"""Client HTTP per l'API CRIC di SDF (eparts.sdfgroup.com)."""
import contextlib
import json
import logging
import os
import random
import re
import threading
import time
from urllib.parse import urljoin, urlparse

import ssl
from pathlib import Path
import certifi
from requests.adapters import HTTPAdapter

import requests
import urllib3.connectionpool

import config

try:
    from bs4 import BeautifulSoup
except ImportError:  # bs4 e' gia' una dipendenza del progetto (requirements.txt)
    BeautifulSoup = None

log = logging.getLogger("sdf.client")

BASE = "https://eparts.sdfgroup.com/explorer/rest/"

# label utente -> codice richiesto dall'API
BRANDS = {
    "SAME": "SAME",
    "DEUTZ-FAHR": "DEUT",
    "HURLIMANN": "HURL",
    "LAMBORGHINI": "LAMBORGHINI",
    # visibili solo con l'account extranet (dal 2026-11-10)
    "GREGOIRE": "GREG",
    "VITIBOT": "GPRO",
}

# valori ammessi per il parametro searchType
SEARCH_CODE = "PART_REF"      # ricerca per codice ricambio
SEARCH_DESCR = "DESCRIPTION"  # ricerca per descrizione


class SessionExpired(RuntimeError):
    """Il cookie di sessione non e' piu' valido: rifare il login."""


class LoginError(RuntimeError):
    """Il login automatico e' fallito (credenziali errate o flusso cambiato)."""

class SdfNonRaggiungibile(RuntimeError):
    """Portale SDF non raggiungibile o login fallito: esito finale,
    da mostrare come 'SDF non raggiungibile', non come 'nessun risultato'."""

INTERMEDI = Path(__file__).resolve().parent / "intermedi.pem"

def _contesto_ssl():
    ctx = ssl.create_default_context(cafile=certifi.where())
    if INTERMEDI.exists():
        ctx.load_verify_locations(cafile=str(INTERMEDI))
    return ctx

class _AdapterSdf(HTTPAdapter):
    def init_poolmanager(self, *args, **kwargs):
        kwargs["ssl_context"] = _contesto_ssl()
        return super().init_poolmanager(*args, **kwargs)

def _sessione_http():
    s = requests.Session()
    s.mount("https://", _AdapterSdf())
    return s

# ----------------------------------------------------------------------
# Login automatico (extranet SDF: ADFS -> www.extranetsdf.com -> store ->
# ita.store -> eparts). Ricostruito da HAR di login reali (newlogin*.har)
# e collaudato con debug/prova_login_extranet.py. Dal 2026-11-10 sostituisce
# il vecchio flusso Auth0 (account parziale, senza Gregoire/Vitibot): il
# vecchio flusso e' descritto in CLAUDE.md come riferimento storico.
#
# E' un meccanismo AGGIUNTIVO: il login manuale via cookie (SDF_COOKIE /
# dati/cookie.txt) resta il percorso primario quando un cookie e' gia'
# presente (vedi SdfClient.__init__) e funziona da fallback immediato se
# questo flusso dovesse rompersi con un cambiamento del sito SDF.
# ----------------------------------------------------------------------

EXTRANET = "https://www.extranetsdf.com"
ADFS_ORIGIN = "https://logon.extranetsdf.com"
PONTE_STORE = f"{EXTRANET}/Dis/Disstart.nsf/redirectAgent?OpenAgent&TIPO=STORE"
STORE = "https://store.sdfgroup.com"
ITA_STORE = "https://ita.store.sdfgroup.com"
EPARTS_LOGIN_URL = "https://eparts.sdfgroup.com/explorer/jwt/cric.html"

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36")
_HEADER_BROWSER = {
    "User-Agent": _UA,
    "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,"
               "image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7"),
    "Accept-Language": "it-IT,it;q=0.9,en-US;q=0.8,en;q=0.7",
    "Upgrade-Insecure-Requests": "1",
    "sec-ch-ua": '"Not:A-Brand";v="8", "Chromium";v="156", "Google Chrome";v="156"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-User": "?1",
}

# piccole pause tra i passi, come un browser: il login avviene di rado
# (all'avvio e quando la sessione scade), quindi costano pochi secondi
_PAUSA_DIGITAZIONE = (2, 4)
_PAUSA_AUTOINVIO = (0.3, 1.0)

_LINK_STORE = re.compile(r"https://store\.sdfgroup\.com/extranet\?[^\"'\s<>]+")


def _pausa(intervallo):
    time.sleep(random.uniform(*intervallo))


def _senza_porta_default(url):
    """'https://host:443/...' -> 'https://host/...', come fa il browser
    (l'ADFS dopo il login rimanda a un URL con ':443' esplicito)."""
    p = urlparse(url)
    if p.scheme == "https" and p.netloc.endswith(":443"):
        return url.replace(p.netloc, p.netloc[:-4], 1)
    return url


@contextlib.contextmanager
def _url_intatti():
    """Disattiva la ri-codifica degli URL di urllib3 per una richiesta.

    urllib3 (connectionpool._encode_target) mette in maiuscolo le codifiche
    '%xx' ('%2f' -> '%2F'). L'URL dell'ADFS (?SAMLRequest=...&Signature=...)
    e' firmato sulla stringa esatta: alterato cosi', l'ADFS accetta le
    credenziali ma poi mostra una pagina "Errore" generica invece di
    emettere la SAMLResponse. Gli URL qui arrivano dal sito stesso, quindi
    sono gia' validi.

    Nota: e' una sostituzione temporanea a livello di modulo. Le richieste
    REST di altri thread che partono in quei pochi istanti la attraversano
    senza danni (i loro URL sono gia' codificati da requests); i login sono
    serializzati da SdfClient._sessione_lk."""
    originale = urllib3.connectionpool._encode_target
    urllib3.connectionpool._encode_target = lambda target: target
    try:
        yield
    finally:
        urllib3.connectionpool._encode_target = originale


def _vai(s, metodo, url, *, data=None, headers=None, segui=True,
         timeout=30, max_redirect=10):
    """Come s.get/s.post, ma invia gli URL ESATTAMENTE come li da' il sito
    (requests li normalizzerebbe, vedi _url_intatti) e segue i redirect a
    mano, usando la Location cosi' com'e'."""
    storia = []
    for _ in range(max_redirect + 1):
        req = s.prepare_request(requests.Request(metodo, url, data=data, headers=headers))
        req.url = url
        with _url_intatti():
            r = s.send(req, allow_redirects=False, timeout=timeout)
        if r.status_code == 429 or r.status_code >= 500:
            p = urlparse(r.url)
            raise LoginError(f"login SDF: il portale ha risposto {r.status_code} "
                             f"su {p.netloc}{p.path}")
        if not segui or r.status_code not in (301, 302, 303, 307, 308):
            r.history = storia
            return r
        storia.append(r)
        url = _senza_porta_default(urljoin(r.url, r.headers["Location"]))
        if r.status_code in (301, 302, 303):     # come il browser: POST -> GET
            metodo, data = "GET", None
        headers = {"Referer": r.url}
    raise LoginError("login SDF: troppi redirect")


def _form_con(html, base_url, *, campo=None, password=False):
    """Trova il <form> che contiene un certo campo (o un input password) e
    ritorna (action assoluta, dict dei campi con i valori di default);
    (None, None) se non c'e'."""
    if BeautifulSoup is None:
        raise LoginError("login SDF: beautifulsoup4 non installato")
    for form in BeautifulSoup(html, "lxml").find_all("form"):
        inputs = form.find_all("input")
        if campo and not any(i.get("name") == campo for i in inputs):
            continue
        if password and not any((i.get("type") or "").lower() == "password" for i in inputs):
            continue
        campi = {}
        for i in inputs:
            nome = i.get("name")
            if not nome:
                continue
            tipo = (i.get("type") or "text").lower()
            if tipo in ("checkbox", "radio") and not i.has_attr("checked"):
                continue
            campi[nome] = i.get("value", "")
        return urljoin(base_url, form.get("action") or base_url), campi
    return None, None


def login_automatico(username, password, timeout=30):
    """Esegue l'intero login extranet (7 passi, vedi CLAUDE.md) e ritorna
    il cookie 'JSESSIONID=...' pronto da passare a SdfClient(cookie=...).

    Solleva LoginError se un passo qualsiasi non si comporta come atteso
    (credenziali sbagliate, sito cambiato, ecc.): non tenta di indovinare,
    si ferma con un messaggio che indica il passo. Per diagnosticare un
    errore: debug/prova_login_extranet.py --solo-login --dettaglio.
    """
    if not username or not password:
        raise LoginError("login SDF: username o password mancanti")

    s = _sessione_http()
    s.headers.update(_HEADER_BROWSER)

    def vai(*a, **k):
        return _vai(s, *a, timeout=timeout, **k)

    # 1. extranet -> redirect fino al form di login ADFS (URL firmato)
    r = vai("GET", EXTRANET + "/")
    action, campi = _form_con(r.text, r.url, password=True)
    if not action:
        raise LoginError("login SDF passo 1 (extranet): form di login ADFS non trovato")
    nome_user = next((k for k in campi if "user" in k.lower()), None)
    nome_pwd = next((k for k in campi if "password" in k.lower()), None)
    if not nome_user or not nome_pwd:
        raise LoginError("login SDF passo 1: campi username/password non "
                         f"riconosciuti: {list(campi)}")

    # 2. credenziali -> (redirect) pagina auto-inviante con la SAMLResponse
    _pausa(_PAUSA_DIGITAZIONE)
    campi[nome_user] = username
    campi[nome_pwd] = password
    r = vai("POST", action, data=campi, headers={"Referer": r.url, "Origin": ADFS_ORIGIN})
    action, campi = _form_con(r.text, r.url, campo="SAMLResponse")
    if not action:
        if _form_con(r.text, r.url, password=True)[0]:
            raise LoginError("login SDF passo 2 (ADFS): credenziali rifiutate")
        raise LoginError("login SDF passo 2 (ADFS): SAMLResponse non ricevuta")

    # 3. SAMLResponse all'extranet (names.nsf?SAMLLogin) -> LtpaToken
    _pausa(_PAUSA_AUTOINVIO)
    r = vai("POST", action, data=campi, headers={"Referer": r.url})

    # 4. ponte extranet -> store: risponde 200 con un redirect JavaScript
    #    verso store.sdfgroup.com/extranet?KEY=..., che ha il form jwt
    _pausa(_PAUSA_AUTOINVIO)
    r = vai("GET", PONTE_STORE, headers={"Referer": EXTRANET + "/"})
    action, campi = _form_con(r.text, r.url, campo="jwt")
    if not action:
        m = _LINK_STORE.search(r.text)
        if not m:
            raise LoginError("login SDF passo 4 (redirectAgent STORE): link allo "
                             "store non trovato (sessione extranet non valida?)")
        r = vai("GET", m.group(0).replace("&amp;", "&"), headers={"Referer": EXTRANET + "/"})
        action, campi = _form_con(r.text, r.url, campo="jwt")
        if not action:
            raise LoginError("login SDF passo 4 (store/extranet): JWT non trovato")

    # 5. autentica il sito nopCommerce (ita.store.sdfgroup.com)
    _pausa(_PAUSA_AUTOINVIO)
    r = vai("POST", action, data=campi, headers={"Referer": r.url}, segui=False)
    if r.status_code != 302:
        raise LoginError("login SDF passo 5 (ita.store/login): atteso 302, "
                         f"ricevuto {r.status_code}")

    # 6. catalogo ricambi -> pagina con un nuovo JWT per eparts
    _pausa(_PAUSA_AUTOINVIO)
    r = vai("GET", f"{ITA_STORE}/eparts/spareparts")
    action, campi = _form_con(r.text, r.url, campo="jwt")
    if not action:
        raise LoginError("login SDF passo 6 (eparts/spareparts): JWT non trovato")

    # 7. JWT a eparts.sdfgroup.com: la risposta imposta JSESSIONID
    _pausa(_PAUSA_AUTOINVIO)
    vai("POST", EPARTS_LOGIN_URL, data=campi, headers={"Referer": f"{ITA_STORE}/"})
    jsessionid = next((c.value for c in s.cookies
                       if c.name == "JSESSIONID" and "eparts" in c.domain), None)
    if not jsessionid:
        raise LoginError("login SDF passo 7 (eparts jwt/cric.html): "
                         "JSESSIONID non impostato dopo il login")

    log.info("SDF: login automatico riuscito per %s", username)
    return f"JSESSIONID={jsessionid}"


class SdfClient:
    def __init__(self, cookie=None, username=None, password=None,
                 brand="SAME", lang="it-IT", delay=0.4):
        # credenziali: il cookie manuale, se presente, ha SEMPRE priorita'.
        # E' il fallback immediato in caso il login automatico si rompa
        # (basta impostare/aggiornare SDF_COOKIE a mano, senza toccare
        # codice, e questo client torna a funzionare come prima).
        self.cookie = cookie or os.environ.get("SDF_COOKIE", "")
        self.username = username or os.environ.get("SDF_USERNAME", "")
        self.password = password or os.environ.get("SDF_PASSWORD", "")

        if not self.cookie:
            if self.username and self.password:
                self.cookie = login_automatico(self.username, self.password)
                self._salva_cookie()
            else:
                raise ValueError(
                    "Credenziali SDF mancanti. Imposta SDF_COOKIE (login manuale, "
                    "vedi dati/cookie.txt) oppure SDF_USERNAME e SDF_PASSWORD "
                    "(login automatico)."
                )

        self.brand = BRANDS.get(brand, brand)
        self.lang = lang
        self.delay = delay
        self._last = 0.0
        self._throttle_lk = threading.Lock()
        self._sessione_lk = threading.Lock()
        self.s = _sessione_http()
        self.s.headers.update({
            "Cookie": self.cookie,
            "Accept-Language": f"{lang},it;q=0.9",
            "User-Agent": _UA,
        })

    # ------------------------------------------------------------------
    def set_brand(self, brand):
        self.brand = BRANDS.get(brand, brand)
        return self.brand

    def _throttle(self):
        # con piu' thread concorrenti, lettura+scrittura di _last deve
        # essere atomica: altrimenti due thread possono leggere lo stesso
        # _last, dormire in parallelo e violare il delay minimo tra
        # richieste effettive al portale
        with self._throttle_lk:
            wait = self.delay - (time.time() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.time()

    def _handle(self, r):
        if r.status_code in (401, 403):
            raise SessionExpired(f"{r.status_code} su {r.url}")
        # alcune installazioni rispondono 200 con la pagina di login
        if "text/html" in r.headers.get("Content-Type", "") and "login" in r.text.lower():
            raise SessionExpired(f"redirect a login su {r.url}")
        r.raise_for_status()
        if not r.text.strip():
            return None          # 200 con body vuoto = nessun risultato
        try:
            return r.json()
        except json.JSONDecodeError:
            return None

    def _rinnova_sessione(self, cookie_prima=None):
        """Chiamato quando il cookie corrente non e' piu' valido. Se abbiamo
        username/password, rifa' il login automatico e aggiorna il cookie
        in uso; altrimenti propaga l'errore (serve un nuovo cookie manuale).

        'cookie_prima' e' il cookie che il chiamante stava usando quando ha
        preso il 401: con piu' thread paralleli, se la sessione scade una
        volta tutti i thread in volo lo scoprono quasi insieme. Senza
        coordinamento ognuno rifarebbe un login automatico per conto suo
        e, se il portale ammette una sola sessione attiva per utente, ogni
        nuovo login invaliderebbe quello appena ottenuto da un altro thread
        (osservato: valanga di "sessione scaduta" in loop finche' non si
        esauriscono i retry). Con il lock, solo il primo thread che arriva
        rifa' davvero il login; gli altri, trovando che self.cookie e'
        gia' cambiato rispetto a quello con cui hanno fallito, riusano
        quello nuovo invece di rilogarsi a loro volta."""
        if not (self.username and self.password):
            raise SessionExpired(
                "Sessione SDF scaduta e nessuna credenziale per il login "
                "automatico: aggiorna SDF_COOKIE / dati/cookie.txt a mano.")
        with self._sessione_lk:
            if cookie_prima is not None and self.cookie != cookie_prima:
                log.info("SDF: sessione gia' rinnovata da un altro thread, riuso quella")
                return
            log.warning("SDF: sessione scaduta, rifaccio il login automatico")
            try:
                self.cookie = login_automatico(self.username, self.password)
            except (LoginError, requests.RequestException) as e:
                log.error("SDF: login automatico fallito: %s", e)
                raise SdfNonRaggiungibile(f"login automatico fallito: {e}") from e
            self.s.headers["Cookie"] = self.cookie
            self._salva_cookie()

    def _salva_cookie(self):
        """Persiste il cookie appena ottenuto in dati/cookie.txt, cosi' il
        prossimo avvio del processo lo riusa (SdfClient da' sempre priorita'
        al cookie salvato, vedi __init__) invece di rifare il login ad ogni
        riavvio. Se scade nel frattempo, il meccanismo di rinnovo su 401
        (_rinnova_sessione) lo rifa' automaticamente e lo salva di nuovo."""
        try:
            config.scrivi_credenziale("cookie.txt", self.cookie)
        except Exception:
            log.exception("SDF: impossibile salvare il cookie su disco")

    def get(self, path, brand=None, retries=2, **params):
        cookie_usato = self.cookie
        try:
            return self._get(path, brand, retries, **params)
        except SessionExpired:
            self._rinnova_sessione(cookie_usato)
            return self._get(path, brand, retries, **params)

    def post(self, path, brand=None, retries=2, **body):
        cookie_usato = self.cookie
        try:
            return self._post(path, brand, retries, **body)
        except SessionExpired:
            self._rinnova_sessione(cookie_usato)
            return self._post(path, brand, retries, **body)

    def _get(self, path, brand, retries, **params):
        for attempt in range(retries + 1):
            self._throttle()
            t0 = time.time()
            try:
                r = self.s.get(
                    BASE + path,
                    params={"brand": brand or self.brand, "lang": self.lang, **params},
                    timeout=30,
                )
                out = self._handle(r)
                log.debug("GET %s brand=%s tentativo=%d %dms",
                         path, brand or self.brand, attempt, (time.time() - t0) * 1000)
                return out
            except SessionExpired:
                log.debug("GET %s brand=%s tentativo=%d %dms -> sessione scaduta",
                         path, brand or self.brand, attempt, (time.time() - t0) * 1000)
                raise
            except (requests.Timeout, requests.ConnectionError) as e:
                log.debug("GET %s brand=%s tentativo=%d %dms -> %s",
                         path, brand or self.brand, attempt, (time.time() - t0) * 1000,
                         type(e).__name__)
                if attempt == retries:
                    raise SdfNonRaggiungibile(f"{type(e).__name__}: {e}") from e
                time.sleep(2 ** attempt)

    def _post(self, path, brand, retries, **body):
        for attempt in range(retries + 1):
            self._throttle()
            t0 = time.time()
            try:
                r = self.s.post(
                    BASE + path,
                    json={"brand": brand or self.brand, "lang": self.lang, **body},
                    timeout=30,
                )
                out = self._handle(r)
                log.debug("POST %s brand=%s tentativo=%d %dms",
                         path, brand or self.brand, attempt, (time.time() - t0) * 1000)
                return out
            except SessionExpired:
                log.debug("POST %s brand=%s tentativo=%d %dms -> sessione scaduta",
                         path, brand or self.brand, attempt, (time.time() - t0) * 1000)
                raise
            except (requests.Timeout, requests.ConnectionError) as e:
                log.debug("POST %s brand=%s tentativo=%d %dms -> %s",
                         path, brand or self.brand, attempt, (time.time() - t0) * 1000,
                         type(e).__name__)
                if attempt == retries:
                    raise SdfNonRaggiungibile(f"{type(e).__name__}: {e}") from e
                time.sleep(2 ** attempt)
