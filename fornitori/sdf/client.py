"""Client HTTP per l'API CRIC di SDF (eparts.sdfgroup.com)."""
import json
import logging
import os
import time
from urllib.parse import urljoin

import requests

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
}

# valori ammessi per il parametro searchType
SEARCH_CODE = "PART_REF"      # ricerca per codice ricambio
SEARCH_DESCR = "DESCRIPTION"  # ricerca per descrizione


class SessionExpired(RuntimeError):
    """Il cookie di sessione non e' piu' valido: rifare il login."""


class LoginError(RuntimeError):
    """Il login automatico e' fallito (credenziali errate o flusso cambiato)."""


# ----------------------------------------------------------------------
# Login automatico (Auth0 -> store.sdfgroup.com -> ita.store.sdfgroup.com
# -> eparts.sdfgroup.com). Ricostruito da un HAR di login reale con
# debug/analizza_har_sdf.py: vedi CLAUDE.md, sezione "Guida: implementare
# il login automatico SDF", per la spiegazione passo-passo. Se SDF cambia
# il flusso (nuova cattura HAR mostra una catena diversa), questa e'
# l'unica funzione da aggiornare.
#
# E' un meccanismo AGGIUNTIVO: il login manuale via cookie (SDF_COOKIE /
# dati/cookie.txt) resta il percorso primario quando un cookie e' gia'
# presente (vedi SdfClient.__init__) e funziona da fallback immediato se
# questo flusso dovesse rompersi con un cambiamento del sito SDF.
# ----------------------------------------------------------------------

AUTH0_DOMAIN = "https://sdfdataplatform.eu.auth0.com"
STORE = "https://store.sdfgroup.com"
ITA_STORE = "https://ita.store.sdfgroup.com"
EPARTS_LOGIN_URL = "https://eparts.sdfgroup.com/explorer/jwt/cric.html"

_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"


def _location(r):
    """Location di una risposta di redirect, risolta rispetto all'URL richiesto."""
    loc = r.headers.get("Location")
    if not loc:
        raise LoginError(f"login SDF: atteso redirect da {r.url}, "
                          f"ricevuto {r.status_code} senza Location")
    return urljoin(r.url, loc)


def _campi_form_nascosto(html):
    """Estrae i campi <input> nascosti del form auto-inviante di una
    pagina response_mode=form_post (usato da Auth0 e da store/ita.store
    per passare code/jwt al passo successivo)."""
    if BeautifulSoup is None:
        raise LoginError("login SDF: beautifulsoup4 non installato")
    form = BeautifulSoup(html, "lxml").find("form")
    if not form:
        raise LoginError("login SDF: pagina inattesa, nessun <form> trovato "
                          "(il sito potrebbe aver cambiato il flusso di login)")
    return {inp.get("name"): inp.get("value", "")
            for inp in form.find_all("input") if inp.get("name")}


def login_automatico(username, password, timeout=30):
    """Esegue l'intero login (10 passi, vedi CLAUDE.md) e ritorna il
    cookie 'JSESSIONID=...' pronto da passare a SdfClient(cookie=...).

    Solleva LoginError se un passo qualsiasi non si comporta come atteso
    (credenziali sbagliate, captcha comparso, sito cambiato, ecc.) — non
    tenta di indovinare, si ferma con un messaggio che indica il passo.
    """
    if not username or not password:
        raise LoginError("login SDF: username o password mancanti")

    s = requests.Session()
    s.headers.update({"User-Agent": _UA})

    # 1. store.sdfgroup.com avvia il login OIDC verso Auth0
    r = s.get(f"{STORE}/auth0", allow_redirects=False, timeout=timeout)
    if r.status_code != 302:
        raise LoginError(f"login SDF passo 1 (/auth0): atteso 302, ricevuto {r.status_code}")
    url_authorize = _location(r)

    # 2. Auth0 rimanda alla pagina di login con un proprio 'state' interno
    r = s.get(url_authorize, allow_redirects=False, timeout=timeout)
    if r.status_code != 302:
        raise LoginError(f"login SDF passo 2 (/authorize): atteso 302, ricevuto {r.status_code}")
    url_login = _location(r)

    # 3. pagina di login vera e propria (serve solo a far impostare ad
    #    Auth0 gli eventuali cookie di sessione della transazione)
    s.get(url_login, timeout=timeout)

    # 4. invio credenziali
    r = s.post(f"{AUTH0_DOMAIN}/u/login", data={
        "state": _stato_da_query(url_login),
        "username": username,
        "password": password,
    }, allow_redirects=False, timeout=timeout)
    if r.status_code != 302:
        raise LoginError("login SDF passo 4 (/u/login): credenziali rifiutate o "
                          f"pagina di login cambiata (atteso 302, ricevuto {r.status_code})")
    url_resume = _location(r)

    # 5. /authorize/resume -> pagina auto-inviante con code + state originale
    r = s.get(url_resume, allow_redirects=False, timeout=timeout)
    campi = _campi_form_nascosto(r.text)
    if "code" not in campi or "state" not in campi:
        raise LoginError("login SDF passo 5 (/authorize/resume): campi "
                          "'code'/'state' mancanti nel form di risposta")

    # 6. consegna code+state a store.sdfgroup.com: lo autentica
    r = s.post(f"{STORE}/signin-oidc", data=campi, allow_redirects=False, timeout=timeout)
    if r.status_code != 302:
        raise LoginError(f"login SDF passo 6 (/signin-oidc): atteso 302, ricevuto {r.status_code}")

    # 7. store.sdfgroup.com (ora autenticato) genera un JWT per ita.store
    r = s.get(f"{STORE}/auth0", timeout=timeout)
    campi = _campi_form_nascosto(r.text)
    jwt1 = campi.get("jwt")
    if not jwt1:
        raise LoginError("login SDF passo 7 (/auth0): JWT non trovato nella risposta")

    # 8. autentica il sito nopCommerce vero e proprio (ita.store.sdfgroup.com)
    r = s.post(f"{ITA_STORE}/login", data={"jwt": jwt1},
               headers={"Referer": f"{STORE}/"}, allow_redirects=False, timeout=timeout)
    if r.status_code != 302:
        raise LoginError(f"login SDF passo 8 (ita.store/login): atteso 302, ricevuto {r.status_code}")

    # 9. richiesta del catalogo ricambi -> pagina con un nuovo JWT per eparts
    r = s.get(f"{ITA_STORE}/eparts/spareparts", timeout=timeout)
    campi = _campi_form_nascosto(r.text)
    jwt2 = campi.get("jwt")
    if not jwt2:
        raise LoginError("login SDF passo 9 (eparts/spareparts): JWT non trovato")

    # 10. consegna il JWT a eparts.sdfgroup.com: la risposta imposta JSESSIONID
    s.post(EPARTS_LOGIN_URL, data={"jwt": jwt2},
           headers={"Referer": f"{ITA_STORE}/"}, timeout=timeout)
    jsessionid = s.cookies.get("JSESSIONID")
    if not jsessionid:
        raise LoginError("login SDF passo 10 (eparts jwt/cric.html): "
                          "JSESSIONID non impostato dopo il login")

    log.info("SDF: login automatico riuscito per %s", username)
    return f"JSESSIONID={jsessionid}"


def _stato_da_query(url):
    from urllib.parse import urlparse, parse_qs
    stato = parse_qs(urlparse(url).query).get("state", [None])[0]
    if not stato:
        raise LoginError(f"login SDF: parametro 'state' non trovato in {url}")
    return stato


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
        self.s = requests.Session()
        self.s.headers.update({
            "Cookie": self.cookie,
            "Accept-Language": f"{lang},it;q=0.9",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        })

    # ------------------------------------------------------------------
    def set_brand(self, brand):
        self.brand = BRANDS.get(brand, brand)
        return self.brand

    def _throttle(self):
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

    def _rinnova_sessione(self):
        """Chiamato quando il cookie corrente non e' piu' valido. Se abbiamo
        username/password, rifa' il login automatico e aggiorna il cookie
        in uso; altrimenti propaga l'errore (serve un nuovo cookie manuale)."""
        if not (self.username and self.password):
            raise SessionExpired(
                "Sessione SDF scaduta e nessuna credenziale per il login "
                "automatico: aggiorna SDF_COOKIE / dati/cookie.txt a mano.")
        log.warning("SDF: sessione scaduta, rifaccio il login automatico")
        self.cookie = login_automatico(self.username, self.password)
        self.s.headers["Cookie"] = self.cookie

    def get(self, path, brand=None, retries=2, **params):
        try:
            return self._get(path, brand, retries, **params)
        except SessionExpired:
            self._rinnova_sessione()
            return self._get(path, brand, retries, **params)

    def post(self, path, brand=None, retries=2, **body):
        try:
            return self._post(path, brand, retries, **body)
        except SessionExpired:
            self._rinnova_sessione()
            return self._post(path, brand, retries, **body)

    def _get(self, path, brand, retries, **params):
        for attempt in range(retries + 1):
            self._throttle()
            try:
                r = self.s.get(
                    BASE + path,
                    params={"brand": brand or self.brand, "lang": self.lang, **params},
                    timeout=30,
                )
                return self._handle(r)
            except SessionExpired:
                raise
            except (requests.Timeout, requests.ConnectionError):
                if attempt == retries:
                    raise
                time.sleep(2 ** attempt)

    def _post(self, path, brand, retries, **body):
        for attempt in range(retries + 1):
            self._throttle()
            try:
                r = self.s.post(
                    BASE + path,
                    json={"brand": brand or self.brand, "lang": self.lang, **body},
                    timeout=30,
                )
                return self._handle(r)
            except SessionExpired:
                raise
            except (requests.Timeout, requests.ConnectionError):
                if attempt == retries:
                    raise
                time.sleep(2 ** attempt)
