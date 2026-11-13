"""
Client HTTP per il portale Maschio Gaspardo (@work, work.maschiogaspardo.com).

Il portale e' classic ASP su IIS. Solo trasporto: login, sessione, freno
fra le richieste, rinnovo su sessione scaduta. L'estrazione dei dati sta
in parser.py.

Login: form semplice, POST /login_check.asp con usr/pwd (+ remeb_me e tre
campi nascosti vuoti). Nessun token, nessun captcha (ricostruito da
fornitori/maschio/maschio.har). La sessione vive nei cookie ASPSESSIONID*
(+ autosignP/autosignUSv2 del "ricordami"), gestiti da requests.Session.

Endpoint usati (Archivi -> Elenco Ricambi):
  - esplosi:   /applications/books/book_parts_expl-ajax.asp?act=...
               lines / categories / models -> JSON [{id, des}]
               table   -> HTML, elenco release (cataloghi) paginato
               load    -> SVG della tavola (richiede login: 401 senza)
               getPartNumber -> testo nudo: il codice in quella posizione
  - viewer:    /applications/books/book_parts_expl-view.asp?releaseId=N
               -> elenco tavole della release
  - anagrafica: POST /applications/books/book_parts_info-view.asp (ricerca),
               poi GET ?new=no&p=N per le pagine successive. ATTENZIONE: la
               ricerca resta salvata nella sessione ASP, quindi le pagine di
               una ricerca vanno lette in sequenza, con la stessa sessione
               e senza ricerche in mezzo.

Credenziali: MASCHIO_USERNAME / MASCHIO_PASSWORD (variabili d'ambiente o
.env), in alternativa dati/maschio_username.txt / dati/maschio_password.txt.
"""

import logging
import threading
import time
from urllib.parse import urljoin

import requests
from requests.adapters import HTTPAdapter

import config

log = logging.getLogger("maschio.client")

BASE = "https://work.maschiogaspardo.com"
URL_LOGIN_FORM = BASE + "/main_login.asp"
URL_LOGIN = BASE + "/login_check.asp"
URL_LOGOUT = BASE + "/main_logout.asp"
URL_VERIFICA = BASE + "/applications/books/book_parts_idx.asp"

AJAX_ESPLOSI = "/applications/books/book_parts_expl-ajax.asp"
VIEWER = "/applications/books/book_parts_expl-view.asp"
ANAGRAFICA = "/applications/books/book_parts_info-view.asp"

DELAY = 0.05         # pausa minima fra due richieste (su tutti i thread)
TIMEOUT = 60
TIMEOUT_SVG = 60     # secondi SENZA ricevere nulla (non sul totale). Le
                     # tavole "bloccanti" non mandano nemmeno l'intestazione
                     # (misurato: 161501 muta anche dopo 600 s); le altre
                     # arrivano in 2-6 s anche quando pesano MB.
TIMEOUT_LOGOUT_IN_SOSPESO = 3600  # il logout di una sessione bloccata parte
                                  # solo quando il server la libera
SVG_PARALLELI = 1    # SVG scaricati insieme: la sessione ASP li esegue comunque
                     # uno alla volta, e intanto blocca ogni altra richiesta
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")


class SessioneScaduta(RuntimeError):
    """Il portale rimanda al login: rifarlo."""


class LoginError(RuntimeError):
    """Login fallito (credenziali errate o form di login cambiato)."""


class MaschioNonRaggiungibile(RuntimeError):
    """Portale non raggiungibile o login impossibile."""


class RichiestaAppesa(MaschioNonRaggiungibile):
    """Nessuna risposta entro il timeout di lettura. Sul server la richiesta
    CONTINUA a girare anche se chiudiamo la connessione (classic ASP), e
    finche' non finisce tiene bloccata la sessione (session lock: misurato,
    anche il logout resta in coda). Ritentare sulla stessa sessione non
    serve: chi la riceve deve abbandonare la sessione (abbandona_sessione)."""


def portale_risponde(timeout=20):
    """Controllo leggero, SENZA sessione: la pagina di login. Serve a capire
    se il portale e' sano dopo una richiesta appesa (le richieste appese
    occupano processi del server: troppe e il portale smette di rispondere
    a tutti)."""
    try:
        r = requests.get(URL_LOGIN_FORM, timeout=(10, timeout),
                         headers={"User-Agent": _UA})
        return r.status_code == 200
    except requests.RequestException:
        return False


def _decodifica(r):
    # le pagine ASP spesso non dichiarano il charset: requests assumerebbe
    # latin-1. Se manca, si prova utf-8 e si ripiega su cp1252.
    if "charset" in r.headers.get("Content-Type", "").lower():
        return r.text
    try:
        return r.content.decode("utf-8")
    except UnicodeDecodeError:
        return r.content.decode("cp1252", errors="replace")


def _rimanda_al_login(r):
    """Da non loggati il portale risponde 302 verso main_logout.asp /
    main_login.asp / login_index.asp (o 401 sugli AJAX)."""
    if r.status_code in (401, 403):
        return True
    if r.is_redirect:
        dest = r.headers.get("Location", "").lower()
        return any(x in dest for x in ("logout", "login", "/default.asp"))
    return False


def login(sessione, username, password, timeout=TIMEOUT):
    if not username or not password:
        raise LoginError("login Maschio: username o password mancanti")

    # 1. pagina del form: imposta il cookie ASPSESSIONID
    r = sessione.get(URL_LOGIN_FORM, timeout=timeout)
    r.raise_for_status()
    if "login_check.asp" not in _decodifica(r):
        raise LoginError("login Maschio passo 1: form di login non trovato "
                         "(il sito potrebbe essere cambiato)")

    # 2. invio credenziali (stessi campi del browser, vedi maschio.har)
    dati = {"usr": username, "pwd": password, "remeb_me": "on",
            "resScreen": "", "colorDepth": "", "acrobat": ""}
    r = sessione.post(URL_LOGIN, data=dati, allow_redirects=False,
                      headers={"Referer": URL_LOGIN_FORM, "Origin": BASE},
                      timeout=timeout)
    if r.status_code >= 400:
        raise LoginError(f"login Maschio passo 2: risposta {r.status_code}")
    dest = r.headers.get("Location", "")
    if "login_index" not in dest.lower():
        raise LoginError("login Maschio passo 2: credenziali rifiutate "
                         f"(redirect inatteso verso '{dest}')")
    sessione.get(urljoin(BASE + "/", dest), timeout=timeout)

    # 3. verifica: la pagina Elenco Ricambi non deve rimandare al login
    r = sessione.get(URL_VERIFICA, allow_redirects=False, timeout=timeout)
    if _rimanda_al_login(r) or r.status_code != 200:
        raise LoginError("login Maschio passo 3: dopo il login la pagina "
                         f"Elenco Ricambi risponde {r.status_code}")
    log.info("Maschio: login riuscito")


class MaschioClient:
    def __init__(self, username=None, password=None, delay=DELAY, pool=16,
                 svg_paralleli=SVG_PARALLELI):
        self.username = username or config.leggi_credenziale(
            "maschio_username.txt", "MASCHIO_USERNAME")
        self.password = password or config.leggi_credenziale(
            "maschio_password.txt", "MASCHIO_PASSWORD")
        if not (self.username and self.password):
            raise ValueError(
                "Credenziali Maschio Gaspardo mancanti: imposta MASCHIO_USERNAME "
                "e MASCHIO_PASSWORD (nel .env o come variabili d'ambiente).")
        self.delay = delay
        self.pool = pool
        self._ultimo = 0.0
        self._throttle_lk = threading.Lock()
        self._sessione_lk = threading.Lock()
        self._generazione = 0
        self.n_richieste = 0
        self._svg_sem = threading.Semaphore(svg_paralleli)
        self.s = self._nuova_sessione()
        self._login()

    # ------------------------------------------------------------------
    def abbandona_sessione(self):
        """Dopo una RichiestaAppesa: la sessione resta bloccata sul server
        finche' la richiesta appesa non finisce. Le si manda un logout "in
        sospeso" (thread a parte, timeout lungo: parte quando il server la
        libera, cosi' non resta un accesso aperto) e si rifa' il login su una
        sessione nuova. Chi chiama deve limitare quante volte succede."""
        with self._sessione_lk:
            vecchia = self.s
            threading.Thread(target=self._logout, name="logout-in-sospeso",
                             args=(vecchia, TIMEOUT_LOGOUT_IN_SOSPESO),
                             daemon=True).start()
            log.warning("Maschio: sessione abbandonata (logout in sospeso), "
                        "nuovo login")
            self.s = self._nuova_sessione()
            self._login()

    @staticmethod
    def _logout(sessione, timeout):
        t0 = time.time()
        try:
            sessione.get(URL_LOGOUT, timeout=(15, timeout), allow_redirects=False)
            log.info("Maschio: logout eseguito (%.0fs)", time.time() - t0)
        except requests.RequestException as e:
            log.warning("Maschio: logout non riuscito dopo %.0fs: %s",
                        time.time() - t0, type(e).__name__)
        finally:
            sessione.close()

    def chiudi(self, timeout=15):
        """Logout a fine lavoro: non lasciare accessi aperti sull'account."""
        self._logout(self.s, timeout)

    # ------------------------------------------------------------------
    def _nuova_sessione(self):
        s = requests.Session()
        s.headers.update({"User-Agent": _UA,
                          "Accept-Language": "it-IT,it;q=0.9"})
        s.mount("https://", HTTPAdapter(pool_connections=2,
                                        pool_maxsize=self.pool))
        return s

    def _login(self):
        try:
            login(self.s, self.username, self.password)
        except requests.RequestException as e:
            raise MaschioNonRaggiungibile(f"{type(e).__name__}: {e}") from e
        self._generazione += 1

    def _rinnova_sessione(self, generazione_prima):
        """Con piu' thread la scadenza la scoprono quasi tutti insieme: solo
        il primo rifa' il login (come CarraroClient/SdfClient)."""
        with self._sessione_lk:
            if self._generazione != generazione_prima:
                return
            log.warning("Maschio: sessione scaduta, rifaccio il login")
            self.s = self._nuova_sessione()
            try:
                self._login()
            except LoginError as e:
                raise MaschioNonRaggiungibile(f"login fallito: {e}") from e

    def _throttle(self):
        with self._throttle_lk:
            attesa = self.delay - (time.time() - self._ultimo)
            if attesa > 0:
                time.sleep(attesa)
            self._ultimo = time.time()
            self.n_richieste += 1

    # ------------------------------------------------------------------
    def richiesta(self, metodo, percorso, retries=2, rinnova=True, **kw):
        """Testo della risposta. Rinnova la sessione una volta se il portale
        rimanda al login. rinnova=False per le pagine di anagrafica
        successive alla prima: dopo un nuovo login la ricerca salvata in
        sessione e' persa, e chi chiama deve rifarla."""
        generazione = self._generazione
        try:
            return self._richiesta(metodo, percorso, retries, **kw)
        except SessioneScaduta:
            if not rinnova:
                self._rinnova_sessione(generazione)
                raise
            self._rinnova_sessione(generazione)
            return self._richiesta(metodo, percorso, retries, **kw)

    def _richiesta(self, metodo, percorso, retries, timeout=TIMEOUT, **kw):
        url = urljoin(BASE + "/", percorso.lstrip("/"))
        for tentativo in range(retries + 1):
            self._throttle()
            t0 = time.time()
            try:
                r = self.s.request(metodo, url, timeout=timeout,
                                   allow_redirects=False, **kw)
            except requests.ReadTimeout as e:
                # niente nuovi tentativi: la sessione e' ormai bloccata
                log.debug("%s %s APPESA dopo %dms", metodo, url,
                          (time.time() - t0) * 1000)
                raise RichiestaAppesa(f"nessuna risposta in {timeout}s: {url}") from e
            except (requests.Timeout, requests.ConnectionError) as e:
                log.debug("%s %s tentativo=%d -> %s", metodo, url, tentativo,
                          type(e).__name__)
                if tentativo == retries:
                    raise MaschioNonRaggiungibile(f"{type(e).__name__}: {e}") from e
                time.sleep(2 ** tentativo)
                continue
            log.debug("%s %s %d %dms %dKB", metodo, url, r.status_code,
                      (time.time() - t0) * 1000, len(r.content) // 1000)
            if _rimanda_al_login(r):
                raise SessioneScaduta(f"{r.status_code} su {url}")
            if r.status_code >= 500 and tentativo < retries:
                time.sleep(2 ** tentativo)
                continue
            r.raise_for_status()
            return _decodifica(r)

    def get(self, percorso, rinnova=True, **params):
        return self.richiesta("GET", percorso, params=params or None,
                              rinnova=rinnova)

    def post(self, percorso, dati, rinnova=True):
        return self.richiesta("POST", percorso, data=dati, rinnova=rinnova)

    # ------------------------------------------------------------ esplosi
    def esplosi(self, act, **params):
        return self.get(AJAX_ESPLOSI, act=act, **params)

    def viewer(self, release_id):
        return self.get(VIEWER, releaseId=release_id)

    def svg_tavola(self, cartella, table_id):
        with self._svg_sem:
            return self.richiesta("GET", AJAX_ESPLOSI, timeout=(15, TIMEOUT_SVG),
                                  params={"act": "load", "dir": cartella,
                                          "tableId": table_id})

    def codice_posizione(self, prd_id, tab_id, row_id):
        """Codice ricambio in una posizione del disegno; None se vuota."""
        t = self.esplosi("getPartNumber", prdID=prd_id, tabID=tab_id,
                         rowID=row_id).strip()
        return None if (not t or t == "_EMPTY") else t

    # --------------------------------------------------------- anagrafica
    def anagrafica_cerca(self, prefisso, linea="0", sostituiti=True):
        """Prima pagina della ricerca per prefisso di codice. Le successive
        con anagrafica_pagina(), nella stessa sessione."""
        dati = {"pn": prefisso, "de": "", "com": "", "cm": linea}
        if sostituiti:
            dati["chkInclude"] = "on"
        return self.post(ANAGRAFICA, dati)

    def anagrafica_pagina(self, n):
        return self.get(ANAGRAFICA, rinnova=False, new="no", p=n)


if __name__ == "__main__":
    # prova rapida: ./venv/Scripts/python.exe -m fornitori.maschio.client
    logging.basicConfig(level=logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")
    c = MaschioClient()
    print("linee:", c.esplosi("lines", showAll=1)[:200])
    print("codice pos. 1 tavola 009 release 455:",
          c.codice_posizione(455, "009", 1), "(atteso B07S01750R)")
