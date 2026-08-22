"""
Estrae dal file HAR la traccia del login SDF (Auth0 -> store -> JWT -> eparts),
saltando gli asset statici (css/js/immagini) che non c'entrano con
l'autenticazione. Serve solo per ispezionare a occhio cosa succede: non fa
parte del bot, e non va importato da nessun altro file.

Uso:
    py debug/analizza_har_sdf.py <file.har> [--tutto]

    --tutto   mostra anche le richieste verso domini/estensioni statiche
              che di norma vengono saltate.
"""
import json
import sys
from urllib.parse import urlparse

ESTENSIONI_STATICHE = (
    ".css", ".js", ".svg", ".png", ".jpg", ".jpeg", ".gif", ".ico",
    ".woff", ".woff2", ".ttf", ".map",
)

DOMINI_IGNORATI = (
    "www.googletagmanager.com", "region1.google-analytics.com",
    "fonts.googleapis.com", "fonts.gstatic.com",
    "s3.eu-de.cloud-object-storage.appdomain.cloud",
)


def redigi(nome, valore):
    """Non stampa mai il valore intero di campi sensibili: solo la lunghezza
    e i primi caratteri, quanto basta per riconoscere il formato (es. un JWT
    comincia sempre con 'eyJ...')."""
    basso = nome.lower()
    if any(s in basso for s in ("password", "pwd", "pass")):
        return f"[REDATTO, {len(valore)} caratteri]"
    if len(valore) > 40:
        return f"{valore[:24]}...[{len(valore)} caratteri totali]"
    return valore


def interessante(url):
    path = urlparse(url).path.lower()
    host = urlparse(url).netloc
    if host in DOMINI_IGNORATI:
        return False
    if path.endswith(ESTENSIONI_STATICHE):
        return False
    return True


def stampa_richiesta(e, indice):
    req, res = e["request"], e["response"]
    url = req["url"]
    print(f"\n[{indice}] {req['method']} {res['status']}  {url}")

    origin = next((h["value"] for h in req["headers"]
                   if h["name"].lower() == "origin"), None)
    referer = next((h["value"] for h in req["headers"]
                    if h["name"].lower() == "referer"), None)
    if origin:
        print(f"      origin:  {origin}")
    if referer:
        print(f"      referer: {referer}")

    pd = req.get("postData")
    if pd:
        params = pd.get("params")
        if params:
            for p in params:
                print(f"      form: {p['name']} = {redigi(p['name'], p.get('value', ''))}")
        elif pd.get("text"):
            print(f"      body (raw): {redigi('body', pd['text'])}")

    loc = next((h["value"] for h in res["headers"]
                if h["name"].lower() == "location"), None)
    if loc:
        print(f"      -> redirect Location: {loc}")

    # Chrome espone i nomi dei cookie impostati/inviati solo in alcuni casi
    # (di norma redige i valori di Cookie/Set-Cookie dagli export HAR):
    for c in res.get("cookies", []):
        print(f"      <- Set-Cookie: {c['name']} (domain={c.get('domain')})")
    for c in req.get("cookies", []):
        print(f"      -> cookie inviato: {c['name']}")


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        raise SystemExit(1)
    path = sys.argv[1]
    mostra_tutto = "--tutto" in sys.argv[2:]

    with open(path, encoding="utf-8") as f:
        har = json.load(f)

    entries = har.get("log", {}).get("entries", [])
    print(f"file: {path}")
    print(f"richieste totali nel HAR: {len(entries)}")

    mostrate = 0
    for i, e in enumerate(entries):
        url = e["request"]["url"]
        if not mostra_tutto and not interessante(url):
            continue
        stampa_richiesta(e, i)
        mostrate += 1

    print(f"\n({mostrate} richieste mostrate su {len(entries)}; "
          f"usa --tutto per vederle tutte)")


if __name__ == "__main__":
    main()
