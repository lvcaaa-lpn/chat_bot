"""
Prova del NUOVO login SDF (account "completo", via extranet ADFS) e
confronto leggero tra l'elenco modelli del sito e fornitori/sdf/sdf.db.

Flusso ricostruito dagli HAR del 2026-11-10:
  1. GET www.extranetsdf.com, seguendo i redirect fino alla pagina ADFS
     (logon.extranetsdf.com/adfs/ls/?SAMLRequest=...)
  2. POST del form ADFS (campi nascosti ASP.NET + username/password)
     -> pagina con form auto-inviante che contiene SAMLResponse
  3. POST SAMLResponse a www.extranetsdf.com/names.nsf?SAMLLogin
  4. GET /Dis/Disstart.nsf/redirectAgent?OpenAgent&TIPO=STORE
     -> store.sdfgroup.com/extranet?KEY=... -> form con 'jwt'
  5. POST jwt a ita.store.sdfgroup.com/login       (= vecchio passo 8)
  6. GET ita.store/eparts/spareparts -> nuovo jwt   (= vecchio passo 9)
  7. POST jwt a eparts .../jwt/cric.html -> JSESSIONID (= vecchio passo 10)

Poi, con il JSESSIONID ottenuto, POCHE richieste di sola lettura:
  - 1 x cric/models/search per ciascuna delle 6 marche eparts (famiglie +
    modelli), confrontate con le tabelle family/model del DB;
  - cric/groups per i 2 modelli Lamborghini (G.27 H, G.30 H) che nel DB
    non risultano segnati come completati in crawl_state.
In tutto ~10 richieste di login + 8 di dati.

Ritmo "umano", per non pesare sul portale (che deve restare usabile per
l'azienda e per il collega che scarica con lo stesso account):
  - pause casuali dove una persona si fermerebbe: prima di inserire le
    credenziali, prima di cliccare "SDF STORE", prima di aprire il
    catalogo ricambi, e tra un elenco modelli e l'altro (vedi PAUSE_*);
  - i form che il browser invia da solo (SAMLResponse, jwt) partono dopo
    una pausa breve, come farebbe il browser;
  - una richiesta alla volta, nessun tentativo ripetuto: al primo errore
    di rete/server (timeout, 5xx, 429) lo script si ferma invece di
    insistere.

NON scrive nulla: il DB e' aperto in sola lettura, il cookie ottenuto NON
viene salvato in dati/cookie.txt (il bot continua a usare quello di prima).
Non stampa mai password, cookie o JWT.

ATTENZIONE: l'account e' condiviso con un collega dell'azienda. Lanciarlo
quando lui non sta scaricando (o dopo averlo avvisato).

Credenziali: variabili d'ambiente SDF_USERNAME / SDF_PASSWORD,
altrimenti vengono chieste da terminale (la password senza eco). Nessun file.

Uso (dalla radice del progetto):
  ./venv/Scripts/python.exe debug/prova_login_extranet.py
  ./venv/Scripts/python.exe debug/prova_login_extranet.py --solo-login
  ./venv/Scripts/python.exe debug/prova_login_extranet.py --salva dati/sdf_confronto_sito.json
"""
import argparse
import contextlib
import getpass
import json
import os
import random
import sqlite3
import sys
import time
from pathlib import Path
from urllib.parse import urljoin, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8")

import requests  # noqa: E402
import urllib3.connectionpool  # noqa: E402
from bs4 import BeautifulSoup  # noqa: E402

from fornitori.sdf.client import (  # noqa: E402
    EPARTS_LOGIN_URL, ITA_STORE, LoginError, SdfClient, _sessione_http,
)
from fornitori.sdf.db import DEFAULT_PATH  # noqa: E402

EXTRANET = "https://www.extranetsdf.com"
PONTE_STORE = f"{EXTRANET}/Dis/Disstart.nsf/redirectAgent?OpenAgent&TIPO=STORE"

# codici eparts -> etichetta (da app/ALL/js/data.js nell'HAR di eparts)
MARCHE_SITO = {
    "SAME": "SAME",
    "DEUT": "DEUTZ FAHR",
    "HURL": "HURLIMANN",
    "LAMBORGHINI": "LAMBORGHINI",
    "GPRO": "VITIBOT",
    "GREG": "GREGOIRE",
}
# modelli presenti nel DB ma senza riga 'mod:' in crawl_state
DA_RICONTROLLARE = [("LAMBORGHINI", 128271, 67392), ("LAMBORGHINI", 128271, 67402)]

TIMEOUT = 30
DETTAGLIO = False
UA_CHROME = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
             "(KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36")

# pause casuali (secondi, min-max) per imitare una persona al browser
PAUSA_DIGITAZIONE = (6, 12)   # leggere la pagina di login e scrivere le credenziali
PAUSA_CLIC = (3, 7)           # cliccare una voce di menu dopo che la pagina e' caricata
PAUSA_AUTOINVIO = (0.5, 1.5)  # form che il browser invia da solo (SAMLResponse, jwt)
PAUSA_DATI = (5, 12)          # tra una richiesta di dati e la successiva


def pausa(intervallo, motivo=""):
    t = random.uniform(*intervallo)
    if t >= 2:
        print(f"      ... pausa {t:.0f}s{(' (' + motivo + ')') if motivo else ''}")
    time.sleep(t)


class Interrotto(RuntimeError):
    """Il portale ha dato un errore: ci si ferma invece di insistere."""


def breve(url, n=90):
    """URL accorciato e senza query (che contiene SAMLRequest, KEY, ...)."""
    p = urlparse(url)
    s = f"{p.scheme}://{p.netloc}{p.path}"
    if p.query:
        s += "?" + p.query.split("&")[0][:30] + ("..." if len(p.query) > 30 else "")
    return s[:n]


def passo(n, testo):
    print(f"[{n}] {testo}")


def catena(r):
    """Stampa la catena dei redirect seguiti da requests; si ferma se il
    portale segnala sovraccarico o errore (429 / 5xx)."""
    for h in list(r.history) + [r]:
        impostati = [c.name for c in h.cookies]
        print(f"      {h.status_code} {breve(h.url)}"
              + (f"  [imposta cookie: {', '.join(impostati)}]" if impostati else ""))
    if r.status_code == 429 or r.status_code >= 500:
        raise Interrotto(f"il portale ha risposto {r.status_code} su {breve(r.url)}")


def _senza_porta_default(url):
    """'https://host:443/...' -> 'https://host/...', come fa il browser.
    L'ADFS, dopo il login, rimanda a un URL con ':443' esplicito; Chrome lo
    normalizza (nell'HAR la richiesta successiva e' senza porta)."""
    p = urlparse(url)
    if p.scheme == "https" and p.netloc.endswith(":443"):
        return url.replace(p.netloc, p.netloc[:-4], 1)
    return url


@contextlib.contextmanager
def _url_intatti():
    """Disattiva la normalizzazione degli URL di urllib3 per la durata di
    una richiesta.

    Anche passando a requests l'URL esatto, urllib3 (connectionpool.urlopen
    -> util.url._encode_target) mette in maiuscolo le codifiche '%xx'
    ('%2f' -> '%2F'). L'URL dell'ADFS (?SAMLRequest=...&Signature=...) e'
    firmato sulla stringa esatta: alterato cosi', l'ADFS accetta le
    credenziali ma poi rifiuta di emettere la SAMLResponse (pagina
    "Errore"). Verificato con --dettaglio: sul filo partiva '%2F'.
    Gli URL qui arrivano dal sito stesso, quindi sono gia' validi."""
    originale = urllib3.connectionpool._encode_target
    urllib3.connectionpool._encode_target = lambda target: target
    try:
        yield
    finally:
        urllib3.connectionpool._encode_target = originale


def vai(s, metodo, url, *, data=None, headers=None, segui=True, max_redirect=10):
    """Come s.get/s.post, ma invia gli URL ESATTAMENTE come li da' il sito.

    requests normalizza gli URL (es. '%2f' -> '%2F'), sia nella richiesta
    sia seguendo i redirect. Per l'URL firmato dell'ADFS
    (?SAMLRequest=...&Signature=...) questo invalida la firma: le
    credenziali vengono accettate ma poi ADFS risponde con una pagina
    "Errore" invece della SAMLResponse (osservato al primo test). Qui si
    sovrascrive l'URL preparato e i redirect si seguono a mano, con la
    Location usata cosi' com'e'."""
    storia = []
    for _ in range(max_redirect + 1):
        req = s.prepare_request(requests.Request(metodo, url, data=data, headers=headers))
        req.url = url                       # niente ri-codifica
        if DETTAGLIO:
            nomi_cookie = [c.split("=", 1)[0].strip()
                           for c in (req.headers.get("Cookie") or "").split(";") if c.strip()]
            print(f"      >> {metodo} {breve(url)}")
            print(f"         header: {list(req.headers)}")
            print(f"         cookie inviati: {nomi_cookie}")
        with _url_intatti():
            r = s.send(req, allow_redirects=False, timeout=TIMEOUT)
        if DETTAGLIO:
            print(f"      << {r.status_code} Connection={r.headers.get('Connection')} "
                  f"Content-Length={r.headers.get('Content-Length')} "
                  f"letti={len(r.content)} byte")
        if not segui or r.status_code not in (301, 302, 303, 307, 308):
            r.history = storia
            return r
        storia.append(r)
        url = _senza_porta_default(urljoin(r.url, r.headers["Location"]))
        if r.status_code in (301, 302, 303):   # come il browser: POST -> GET
            metodo, data = "GET", None
        headers = {"Referer": r.url}
    raise LoginError(f"troppi redirect partendo da {breve(storia[0].url)}")


def diagnostica(r, s):
    """Descrive una pagina inattesa senza stampare segreti: titolo, form e
    nomi dei campi, testo visibile, nomi dei cookie in sessione. Salva
    anche l'HTML (con i valori lunghi dei campi oscurati) per l'analisi."""
    import re
    soup = BeautifulSoup(r.text, "lxml")
    print("      --- diagnostica pagina inattesa ---")
    print(f"      titolo: {soup.title.get_text(strip=True) if soup.title else None}")
    for f in soup.find_all("form"):
        print(f"      form action={breve(urljoin(r.url, f.get('action') or ''))} "
              f"campi={[i.get('name') for i in f.find_all('input')]}")
    for tag in soup(["script", "style"]):
        tag.decompose()
    testo = " ".join(soup.get_text(" ").split())
    print(f"      testo: {testo[:600]}")
    print(f"      cookie in sessione (solo nomi/dominio): "
          f"{sorted({(c.name, c.domain) for c in s.cookies})}")
    pulito = re.sub(r'value="[^"]{40,}"', 'value="[oscurato]"', r.text)
    pulito = re.sub(r"value='[^']{40,}'", "value='[oscurato]'", pulito)
    out = Path(__file__).resolve().parent.parent / "dati" / "debug_login_extranet.html"
    out.write_text(pulito, encoding="utf-8")
    print(f"      HTML salvato (valori lunghi oscurati) in {out}")


def form_con(html, base_url, *, campo=None, password=False):
    """Trova il <form> che contiene un certo campo (o un input password) e
    ritorna (action assoluta, dict dei campi con i valori di default)."""
    soup = BeautifulSoup(html, "lxml")
    for form in soup.find_all("form"):
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
        action = urljoin(base_url, form.get("action") or base_url)
        return action, campi
    return None, None


def login_extranet(username, password):
    s = _sessione_http()
    # intestazioni identiche a quelle di Chrome nell'HAR (newlogin2.har):
    # IPOTESI (da confermare): l'ADFS accetta le credenziali ma poi non
    # emette la SAMLResponse a un client che non si presenta come il browser
    s.headers.update({
        "User-Agent": UA_CHROME,
        "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,"
                   "image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7"),
        "Accept-Language": "it-IT,it;q=0.9,en-US;q=0.8,en;q=0.7",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "Upgrade-Insecure-Requests": "1",
        "sec-ch-ua": '"Not:A-Brand";v="8", "Chromium";v="156", "Google Chrome";v="156"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-User": "?1",
    })

    passo(1, "apro l'extranet e seguo i redirect fino all'ADFS")
    r = vai(s, "GET", EXTRANET + "/")
    catena(r)
    action, campi = form_con(r.text, r.url, password=True)
    if not action:
        raise LoginError("passo 1: nessun form con password trovato nella pagina "
                         f"finale ({breve(r.url)}). Il punto di partenza va cambiato.")
    nome_user = next((k for k in campi if "user" in k.lower()), None)
    nome_pwd = next((k for k in campi if "password" in k.lower()), None)
    if not nome_user or not nome_pwd:
        raise LoginError(f"passo 1: campi username/password non riconosciuti: {list(campi)}")
    print(f"      form ADFS trovato, campi: {list(campi)}")

    pausa(PAUSA_DIGITAZIONE, "inserimento credenziali")
    passo(2, "invio credenziali al form ADFS")
    print(f"      username usato: {username!r}  (password: {len(password)} caratteri, "
          f"spazi iniziali/finali: {'SI' if password != password.strip() else 'no'})")
    campi[nome_user] = username
    campi[nome_pwd] = password
    r = vai(s, "POST", action, data=campi, headers={
        "Referer": r.url, "Origin": "https://logon.extranetsdf.com"})
    catena(r)
    action, campi = form_con(r.text, r.url, campo="SAMLResponse")
    if not action:
        diagnostica(r, s)
        if form_con(r.text, r.url, password=True)[0]:
            raise LoginError("passo 2: di nuovo la pagina di login -> credenziali rifiutate")
        raise LoginError("passo 2: nessun form con SAMLResponse nella risposta")
    print(f"      SAMLResponse ricevuta, la invio a {breve(action)}")

    pausa(PAUSA_AUTOINVIO)
    passo(3, "consegno la SAMLResponse all'extranet")
    r = vai(s, "POST", action, data=campi, headers={"Referer": r.url})
    catena(r)
    cookie_extranet = sorted({c.name for c in s.cookies if "extranetsdf" in c.domain})
    print(f"      cookie extranet (solo nomi): {cookie_extranet}")

    pausa(PAUSA_CLIC, "clic su SDF STORE")
    passo(4, "ponte extranet -> store (redirectAgent TIPO=STORE)")
    r = vai(s, "GET", PONTE_STORE, headers={"Referer": EXTRANET + "/"})
    catena(r)
    action, campi = form_con(r.text, r.url, campo="jwt")
    if not action:
        # fallback: pagina con redirect via JavaScript invece che via HTTP
        import re
        m = re.search(r"https://store\.sdfgroup\.com/extranet\?[^\"'\s<>]+", r.text)
        if not m:
            raise LoginError("passo 4: ne' form con jwt ne' link a store.sdfgroup.com/extranet "
                             f"nella risposta ({breve(r.url)})")
        print("      redirect via JavaScript, lo seguo")
        pausa(PAUSA_AUTOINVIO)
        r = vai(s, "GET", m.group(0).replace("&amp;", "&"),
                headers={"Referer": EXTRANET + "/"})
        catena(r)
        action, campi = form_con(r.text, r.url, campo="jwt")
        if not action:
            raise LoginError("passo 4: store.sdfgroup.com/extranet senza form jwt")
    print(f"      jwt ricevuto, destinazione {breve(action)}")

    pausa(PAUSA_AUTOINVIO)
    passo(5, "autentico ita.store con il jwt")
    r = vai(s, "POST", action, data=campi, headers={"Referer": r.url}, segui=False)
    catena(r)
    if r.status_code != 302:
        raise LoginError(f"passo 5: atteso 302, ricevuto {r.status_code}")

    pausa(PAUSA_CLIC, "clic sul catalogo ricambi")
    passo(6, "chiedo il catalogo ricambi (eparts/spareparts)")
    r = vai(s, "GET", f"{ITA_STORE}/eparts/spareparts")
    catena(r)
    action, campi = form_con(r.text, r.url, campo="jwt")
    if not action:
        raise LoginError("passo 6: jwt per eparts non trovato")

    pausa(PAUSA_AUTOINVIO)
    passo(7, "consegno il jwt a eparts")
    r = vai(s, "POST", EPARTS_LOGIN_URL, data=campi, headers={"Referer": f"{ITA_STORE}/"})
    catena(r)
    jsid = next((c.value for c in s.cookies
                 if c.name == "JSESSIONID" and "eparts" in c.domain), None)
    if not jsid:
        raise LoginError("passo 7: JSESSIONID non impostato")
    print("      JSESSIONID ottenuto (non salvato)")
    return f"JSESSIONID={jsid}"


# ----------------------------------------------------------------------
# confronto con il DB
# ----------------------------------------------------------------------

def elenco_db(db, brand):
    fam = {str(r[0]): r[1] for r in db.execute(
        "select row_id, name from family where brand=?", (brand,))}
    mod = {str(r[0]): (str(r[1]), r[2]) for r in db.execute(
        "select row_id, family_id, name from model where brand=?", (brand,))}
    return fam, mod


def elenco_sito(client, brand):
    raw = client.post("cric/models/search", brand=brand, retries=0, searchString=None) or []
    fam, mod = {}, {}
    for f in raw:
        fid = str(f.get("rowId"))
        fam[fid] = f.get("description")
        for c in f.get("catalogList") or []:
            mod[str(c.get("rowId"))] = (fid, c.get("description"))
    return fam, mod


def mostra_diff(titolo, solo, nomi, max_righe=15):
    if not solo:
        return
    print(f"      {titolo}: {len(solo)}")
    for k in sorted(solo)[:max_righe]:
        v = nomi[k]
        print(f"        {k}  {v[1] if isinstance(v, tuple) else v}")
    if len(solo) > max_righe:
        print(f"        ... e altri {len(solo) - max_righe}")


def confronta(cookie, salva=None):
    client = SdfClient(cookie=cookie, delay=1.0)   # cookie esplicito: niente salvataggio
    db = sqlite3.connect(f"file:{DEFAULT_PATH}?mode=ro", uri=True)
    rapporto = {}
    try:
        _confronta(client, db, rapporto)
    except Interrotto as e:
        print(f"\nMI FERMO per non insistere sul portale: {e}")
    finally:
        if salva and rapporto:
            Path(salva).write_text(json.dumps(rapporto, ensure_ascii=False, indent=1),
                                   encoding="utf-8")
            print(f"\nrisposte del sito salvate in {salva}")


def _chiama(descrizione, funzione):
    """Una richiesta di dati, preceduta da una pausa 'umana'. Qualunque
    errore (rete, 4xx/5xx, sessione scaduta) interrompe tutto il confronto:
    meglio un risultato parziale che insistere su un portale in difficolta'."""
    pausa(PAUSA_DATI, descrizione)
    try:
        return funzione()
    except Exception as e:
        raise Interrotto(f"{descrizione}: {type(e).__name__}: {e}") from e


def _confronta(client, db, rapporto):
    print("\n=== Famiglie e modelli: sito vs DB ===")
    for brand, etichetta in MARCHE_SITO.items():
        f_sito, m_sito = _chiama(f"elenco modelli {etichetta}",
                                 lambda: elenco_sito(client, brand))
        f_db, m_db = elenco_db(db, brand)
        rapporto[brand] = {"famiglie": f_sito, "modelli": m_sito}
        uguale = set(f_sito) == set(f_db) and set(m_sito) == set(m_db)
        print(f"{etichetta} ({brand}): sito {len(f_sito)} famiglie / {len(m_sito)} modelli, "
              f"DB {len(f_db)} / {len(m_db)}  {'-> IDENTICO' if uguale else '-> DIVERSO'}")
        mostra_diff("famiglie solo sul sito", set(f_sito) - set(f_db), f_sito)
        mostra_diff("famiglie solo nel DB", set(f_db) - set(f_sito), f_db)
        mostra_diff("modelli solo sul sito", set(m_sito) - set(m_db), m_sito)
        mostra_diff("modelli solo nel DB", set(m_db) - set(m_sito), m_db)

    print("\n=== Modelli non segnati come completati nel DB ===")
    for brand, fam, mod in DA_RICONTROLLARE:
        nome = db.execute("select name from model where brand=? and row_id=?",
                          (brand, mod)).fetchone()
        g = _chiama(f"gruppi {brand} {mod}",
                    lambda: client.get("cric/groups", brand=brand, retries=0,
                                       familyRowId=fam, modelRowId=mod)) or []
        sito_g = {str(x.get("ROW_ID") or x.get("rowId")) for x in g}
        sito_s = {str(s.get("ROW_ID") or s.get("rowId"))
                  for x in g for s in (x.get("subGroupList") or [])}
        db_g = {str(r[0]) for r in db.execute(
            "select distinct group_id from model_drawing where brand=? and model_id=?", (brand, mod))}
        db_s = {str(r[0]) for r in db.execute(
            "select distinct subgroup_id from model_drawing where brand=? and model_id=?", (brand, mod))}
        print(f"{brand} {mod} {nome[0] if nome else '?'}: gruppi sito {len(sito_g)} / DB {len(db_g)}, "
              f"sottogruppi sito {len(sito_s)} / DB {len(db_s)}")
        if sito_g - db_g:
            print(f"      gruppi mancanti nel DB: {sorted(sito_g - db_g)}")
        if sito_s - db_s:
            print(f"      sottogruppi mancanti nel DB: {len(sito_s - db_s)} "
                  f"(es. {sorted(sito_s - db_s)[:10]})")
        rapporto[f"groups:{brand}:{mod}"] = g


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--solo-login", action="store_true",
                    help="prova solo il login, nessuna richiesta di dati")
    ap.add_argument("--dettaglio", action="store_true",
                    help="stampa per ogni richiesta header e cookie inviati (solo nomi) "
                         "e le connessioni TCP aperte (log urllib3)")
    ap.add_argument("--salva", help="file JSON dove salvare le risposte del sito")
    ap.add_argument("--cookie-manuale", action="store_true",
                    help="salta il login: chiede il JSESSIONID copiato dal browser "
                         "(DevTools > Application > Cookies > eparts.sdfgroup.com)")
    a = ap.parse_args()
    if a.dettaglio:
        global DETTAGLIO
        DETTAGLIO = True
        import logging
        logging.basicConfig(format="      [urllib3] %(message)s")
        logging.getLogger("urllib3.connectionpool").setLevel(logging.DEBUG)

    if a.cookie_manuale:
        valore = getpass.getpass("JSESSIONID di eparts (non viene mostrato): ").strip()
        if valore.startswith("JSESSIONID="):
            valore = valore[len("JSESSIONID="):]
        if not valore:
            sys.exit("JSESSIONID vuoto")
        confronta(f"JSESSIONID={valore}", a.salva)
        return

    username = os.environ.get("SDF_USERNAME") or input("username extranet: ").strip()
    password = os.environ.get("SDF_PASSWORD") or getpass.getpass("password extranet: ")

    try:
        cookie = login_extranet(username, password)
    except LoginError as e:
        print(f"\nLOGIN FALLITO: {e}")
        sys.exit(1)
    except Interrotto as e:
        print(f"\nMI FERMO per non insistere sul portale: {e}")
        sys.exit(1)
    except requests.RequestException as e:
        print(f"\nERRORE DI RETE durante il login, mi fermo: {type(e).__name__}: {e}")
        sys.exit(1)
    print("\nLOGIN RIUSCITO")

    if not a.solo_login:
        confronta(cookie, a.salva)


if __name__ == "__main__":
    main()
