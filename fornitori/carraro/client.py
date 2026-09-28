"""
Client HTTP per il catalogo Antonio Carraro (ricambinet.antoniocarraro.it).

Il portale (piattaforma "tQuadra / Ricambi-NET") serve HTML gia' completo
dal server: nessuna API JSON, nessuna chiamata AJAX per il catalogo.
Questo modulo si occupa solo del trasporto (login, sessione, pausa fra
le richieste, rinnovo su sessione scaduta) e restituisce HTML grezzo.
L'estrazione dei dati dalle pagine sta in parser.py.

Login: un solo form, POST a /auth/website/login con username/password e
qualche campo nascosto (letti dalla pagina, non cablati). Nessun captcha
osservato. La sessione vive nei cookie (tqsesid + cookie "ricordami"),
gestiti in automatico da requests.Session.

Credenziali: CARRARO_USERNAME / CARRARO_PASSWORD (variabili d'ambiente o
.env), in alternativa dati/carraro_username.txt / dati/carraro_password.txt.
"""

import logging
import threading
import time
from urllib.parse import quote, urljoin

import requests
from bs4 import BeautifulSoup

import config

log = logging.getLogger("carraro.client")

BASE = "https://ricambinet.antoniocarraro.it"
URL_LOGIN = BASE + "/auth/website/login"

DELAY = 0.5          # pausa minima fra le richieste: e' il portale del costruttore
TIMEOUT = 30
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

# tipi di ricerca del sito: nome nostro -> (campo, model) nell'URL
#   /ricerca/s/<campo>_<testo>/model_<model>/lingua_It/page_<n>/
# Senza il segmento model_<X> il sito risponde con una pagina vuota.
RICERCHE = {
    "codice":    ("searchspareparts", "SpareParts"),     # codice o descrizione
    "modello":   ("searchrproducts", "RProducts"),
    "tavola":    ("scsearch", "Schemes"),
    "matricola": ("snsearch", "SerialNumbers"),
}


class SessioneScaduta(RuntimeError):
    """Il portale ha risposto con la pagina di login: rifare il login."""


class LoginError(RuntimeError):
    """Login fallito (credenziali errate o form di login cambiato)."""


class CarraroNonRaggiungibile(RuntimeError):
    """Portale non raggiungibile o login impossibile: esito finale, da
    mostrare come 'catalogo non raggiungibile', non come 'nessun risultato'."""


def _e_pagina_login(html):
    """Da loggati il form di login non compare in nessuna pagina: se c'e',
    la sessione non e' (piu') valida."""
    return "/auth/website/login" in html


def _decodifica(r):
    # senza charset nell'header requests assume latin-1: meglio stimarlo
    if "charset" not in r.headers.get("Content-Type", "").lower():
        r.encoding = r.apparent_encoding
    return r.text


def login(sessione, username, password, timeout=TIMEOUT):
    """Esegue il login sulla sessione passata (che conserva i cookie).

    Solleva LoginError se qualcosa non va come atteso, indicando il passo:
    non tenta di indovinare.
    """
    if not username or not password:
        raise LoginError("login Carraro: username o password mancanti")

    # 1. home da non loggati: imposta il cookie di sessione e contiene il form
    r = sessione.get(BASE + "/", timeout=timeout)
    r.raise_for_status()
    soup = BeautifulSoup(_decodifica(r), "lxml")
    form = soup.find("form", action=lambda a: a and "/auth/website/login" in a)
    if form is None:
        raise LoginError("login Carraro passo 1: form di login non trovato "
                         "(il sito potrebbe essere cambiato)")

    # campi nascosti (back, urlok, urlko) e pulsante di invio letti dal form,
    # non cablati: se il sito ne cambia i valori continuiamo a funzionare
    campi = {}
    for inp in form.find_all(["input", "button"]):
        nome = inp.get("name")
        if not nome or inp.get("type") in ("checkbox", "radio"):
            continue
        campi[nome] = inp.get("value", "")
    campi["username"] = username
    campi["password"] = password
    url_ko = campi.get("urlko")

    # 2. invio credenziali
    r = sessione.post(urljoin(BASE + "/", form["action"]), data=campi,
                      headers={"Referer": BASE + "/"},
                      allow_redirects=False, timeout=timeout)
    if r.status_code >= 400:
        raise LoginError(f"login Carraro passo 2: risposta {r.status_code}")
    dest = r.headers.get("Location", "")
    if url_ko and dest.rstrip("/").endswith(url_ko.rstrip("/")):
        raise LoginError("login Carraro passo 2: credenziali rifiutate")

    # 3. verifica: da loggati la home non mostra piu' il form di login
    r = sessione.get(BASE + "/", timeout=timeout)
    r.raise_for_status()
    if _e_pagina_login(_decodifica(r)):
        raise LoginError("login Carraro passo 3: dopo l'invio delle "
                         "credenziali la home mostra ancora il form di login")
    log.info("Carraro: login riuscito per %s", username)


class CarraroClient:
    def __init__(self, username=None, password=None, delay=DELAY):
        self.username = username or config.leggi_credenziale(
            "carraro_username.txt", "CARRARO_USERNAME")
        self.password = password or config.leggi_credenziale(
            "carraro_password.txt", "CARRARO_PASSWORD")
        if not (self.username and self.password):
            raise ValueError(
                "Credenziali Antonio Carraro mancanti: imposta CARRARO_USERNAME "
                "e CARRARO_PASSWORD (nel .env o come variabili d'ambiente).")

        self.delay = delay
        self._ultimo = 0.0
        self._throttle_lk = threading.Lock()
        self._sessione_lk = threading.Lock()
        self._generazione = 0      # incrementa a ogni login riuscito
        self.s = self._nuova_sessione()
        self._login()

    # ------------------------------------------------------------------
    def _nuova_sessione(self):
        s = requests.Session()
        s.headers.update({"User-Agent": _UA,
                          "Accept-Language": "it-IT,it;q=0.9"})
        return s

    def _login(self):
        try:
            login(self.s, self.username, self.password)
        except requests.RequestException as e:
            raise CarraroNonRaggiungibile(f"{type(e).__name__}: {e}") from e
        self._generazione += 1

    def _rinnova_sessione(self, generazione_prima):
        """Rifa' il login con una sessione pulita. Con piu' thread in volo
        la scadenza viene scoperta quasi insieme da tutti: solo il primo
        rifa' davvero il login, gli altri vedono che la generazione e' gia'
        cambiata e riusano la sessione nuova (stesso problema gia' visto su
        SDF, vedi SdfClient._rinnova_sessione)."""
        with self._sessione_lk:
            if self._generazione != generazione_prima:
                return
            log.warning("Carraro: sessione scaduta, rifaccio il login")
            self.s = self._nuova_sessione()
            try:
                self._login()
            except LoginError as e:
                log.error("Carraro: login fallito: %s", e)
                raise CarraroNonRaggiungibile(f"login fallito: {e}") from e

    def _throttle(self):
        with self._throttle_lk:
            attesa = self.delay - (time.time() - self._ultimo)
            if attesa > 0:
                time.sleep(attesa)
            self._ultimo = time.time()

    # ------------------------------------------------------------------
    def get(self, percorso, retries=2):
        """HTML della pagina 'percorso' (relativo, es. '/catalogo/serie/').
        Rinnova la sessione una volta se il portale chiede di nuovo il login."""
        generazione = self._generazione
        try:
            return self._get(percorso, retries)
        except SessioneScaduta:
            self._rinnova_sessione(generazione)
            return self._get(percorso, retries)

    def _get(self, percorso, retries):
        url = urljoin(BASE + "/", percorso.lstrip("/"))
        for tentativo in range(retries + 1):
            self._throttle()
            t0 = time.time()
            try:
                r = self.s.get(url, timeout=TIMEOUT)
            except (requests.Timeout, requests.ConnectionError) as e:
                log.debug("GET %s tentativo=%d -> %s", url, tentativo, type(e).__name__)
                if tentativo == retries:
                    raise CarraroNonRaggiungibile(f"{type(e).__name__}: {e}") from e
                time.sleep(2 ** tentativo)
                continue
            log.debug("GET %s %d %dms", url, r.status_code, (time.time() - t0) * 1000)
            if r.status_code in (401, 403):
                raise SessioneScaduta(f"{r.status_code} su {url}")
            r.raise_for_status()
            html = _decodifica(r)
            if _e_pagina_login(html):
                raise SessioneScaduta(f"pagina di login su {url}")
            return html

    # ------------------------------------------------------------------
    def ricerca(self, tipo, testo, pagina=0):
        """HTML di una pagina di risultati della ricerca del sito.
        tipo: 'codice' (codice o descrizione), 'modello', 'tavola', 'matricola'."""
        campo, model = RICERCHE[tipo]
        testo = quote(str(testo).strip(), safe="")
        return self.get(f"/ricerca/s/{campo}_{testo}/model_{model}/"
                        f"lingua_It/page_{int(pagina)}/")

    def pagina_codice(self, codice):
        """Scheda di un codice: codici precedenti + modelli/tavole che lo usano."""
        return self.get("/" + quote(str(codice).strip(), safe=""))


if __name__ == "__main__":
    # prova rapida: ./venv/Scripts/python.exe -m fornitori.carraro.client
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    c = CarraroClient()
    html = c.ricerca("codice", "47921309")
    print("ricerca codice 47921309:",
          "OK, trovato 'Radiatore acqua'" if "Radiatore acqua" in html
          else "risultato inatteso (nessun 'Radiatore acqua' nella pagina)")
    html = c.ricerca("matricola", "10800906")
    print("ricerca matricola 10800906:",
          "OK, trovato MACH4" if "MACH4" in html.upper().replace(" ", "")
          else "risultato inatteso (nessun MACH4 nella pagina)")
