"""
Scarica in locale il catalogo ricambi Maschio Gaspardo in
fornitori/maschio/maschio.db.

Fasi (ognuna riprende da dove si era fermata):
  1. elenco:     linee -> categorie -> modelli, l'elenco di tutte le release
                 (cataloghi) e il legame release-modello. ~500 richieste.
  2. tavole:     per ogni release, l'elenco delle sue tavole. ~2.000 richieste.
  3. posizioni:  per ogni tavola, l'SVG del disegno e poi UNA richiesta per
                 posizione (getPartNumber: e' cosi' che funziona il portale).
                 E' la fase lunga: ~50.000 tavole x ~35 posizioni, ~3,5 s
                 a tavola, circa 2 giorni.
  4. anagrafica: tutti i codici (descrizione, sostituto, peso, URL foto),
                 per prefisso di codice. ~4.500 pagine, in sequenza: la
                 ricerca resta salvata nella sessione del portale, quindi
                 non si parallelizza. Si lancia a parte (--solo-anagrafica),
                 NON mentre gira la fase 3: sarebbe un secondo login
                 contemporaneo sull'account dell'azienda.

Interruzione e ripresa: Ctrl+C ferma dopo le richieste in corso. Ogni
tavola si salva in una sola transazione con tutte le sue posizioni, quindi
non ne restano di salvate a meta'.

Velocita': un solo login (niente sessioni multiple sull'account
dell'azienda); tutte le richieste passano da UN client, che ne lascia
partire al massimo una ogni --ritardo secondi. Il portale esegue comunque
una richiesta alla volta per sessione: vedi _scarica_tavola. In caso di
errori il ritardo aumenta da solo.

Uso (dalla radice del progetto):
  ./venv/Scripts/python.exe -m fornitori.maschio.scarica_tutto                   # fasi 1-3
  ./venv/Scripts/python.exe -m fornitori.maschio.scarica_tutto --limite 20       # prova: solo 20 tavole
  ./venv/Scripts/python.exe -m fornitori.maschio.scarica_tutto --paralleli 5 --ritardo 0.03
  ./venv/Scripts/python.exe -m fornitori.maschio.scarica_tutto --solo-elenco
  ./venv/Scripts/python.exe -m fornitori.maschio.scarica_tutto --solo-anagrafica
  ./venv/Scripts/python.exe -m fornitori.maschio.scarica_tutto --aggiorna-elenco
  ./venv/Scripts/python.exe -m fornitori.maschio.scarica_tutto --stato

Errori dettagliati in dati/log/scarica_maschio.log.
"""

import argparse
import logging
import os
import string
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlencode

import requests

import config
from . import parser
from .client import (AJAX_ESPLOSI, BASE, MaschioClient, MaschioNonRaggiungibile,
                     SessioneScaduta)
from .db import Db

log = logging.getLogger("maschio.scarica_tutto")

RITARDO_MASSIMO = 2.0
ERRORI_DI_FILA_PER_FERMARSI = 20
PREFISSI = list(string.ascii_uppercase + string.digits)

# parametri vuoti che il browser manda sempre a act=table
_FILTRI_VUOTI = dict(showAll=1, selectedLine="", selectedCategory="",
                     selectedModel="", documentCode="", serialNumber="",
                     partNumber="", product="", searchModel="",
                     searchModelType="", offsetRows=15)


class Interrotto(BaseException):
    """Ctrl+C: BaseException, cosi' non viene presa per un errore qualsiasi."""


def url_disegno(cartella, table_id):
    return (BASE + AJAX_ESPLOSI + "?" +
            urlencode({"act": "load", "dir": cartella, "tableId": table_id}))


def _con_freno_interrompibile(client, stop):
    """Dopo Ctrl+C le richieste non ancora partite non partono piu'."""
    originale = client._throttle

    def freno():
        if stop.is_set():
            raise Interrotto()
        originale()
    client._throttle = freno


# ----------------------------------------------------------------------
# Fase 1: elenco
# ----------------------------------------------------------------------
def _tutte_le_pagine(client, **filtri):
    params = dict(_FILTRI_VUOTI, **filtri)
    html = client.esplosi("table", page=1, **params)
    out = parser.releases(html)
    _, pagine, record = parser.paginazione(html)
    for p in range(2, pagine + 1):
        out += parser.releases(client.esplosi("table", page=p, **params))
    # il portale elenca a volte la stessa release due volte: il confronto
    # col totale dichiarato si fa sulle righe, poi si deduplica
    if record and len(out) != record:
        log.warning("table %s: attese %d righe, lette %d", filtri, record, len(out))
    return list({r["id"]: r for r in out}.values())


def fase_elenco(client, db, aggiorna=False):
    if db.fatto("elenco") and not aggiorna:
        print("Elenco: gia' fatto (usa --aggiorna-elenco per rileggerlo)")
        return
    tutte = _tutte_le_pagine(client)
    db.salva_release(tutte)
    print(f"Elenco: {len(tutte)} release in totale", flush=True)

    vuoti = dict(showAll=1, documentCode="", serialNumber="", partNumber="")
    linee = parser.lista_json(client.esplosi("lines", **vuoti))
    db.salva_linee(linee)
    for linea in linee:
        cats = parser.lista_json(client.esplosi(
            "categories", selectedLine=linea["id"], **vuoti))
        db.salva_categorie(linea["id"], cats)
        for cat in cats:
            mods = parser.lista_json(client.esplosi(
                "models", selectedLine=linea["id"], selectedCategory=cat["id"], **vuoti))
            db.salva_modelli(linea["id"], cat["id"], mods)
            for mod in mods:
                chiave = f"mod:{linea['id']}:{cat['id']}:{mod['id']}"
                if db.fatto(chiave) and not aggiorna:
                    continue
                rels = _tutte_le_pagine(client, selectedLine=linea["id"],
                                        selectedCategory=cat["id"],
                                        selectedModel=mod["id"])
                db.lega_release_modello(linea["id"], cat["id"], mod["id"], rels)
                db.segna_fatto(chiave)
            print(f"  {linea['nome']} / {cat['nome']}: {len(mods)} modelli", flush=True)
    db.segna_fatto("elenco")


# ----------------------------------------------------------------------
# Fase 2: tavole di ogni release
# ----------------------------------------------------------------------
def fase_tavole(client, db, stop, limite=None):
    da_fare = db.release_da_leggere(limite)
    if not da_fare:
        print("Tavole: elenco gia' letto per tutte le release")
        return
    print(f"Tavole: leggo l'elenco di {len(da_fare)} release", flush=True)
    for i, r in enumerate(da_fare, 1):
        if stop.is_set():
            raise Interrotto()
        try:
            cartella, tavole = parser.viewer(client.viewer(r["id"]))
        except (requests.HTTPError, MaschioNonRaggiungibile) as e:
            log.warning("release %s: %s", r["id"], e)
            continue
        if not cartella:
            log.warning("release %s (%s %s): cartella disegni non trovata",
                        r["id"], r["codice_doc"], r["data"])
        if not tavole:
            log.warning("release %s (%s %s): nessuna tavola", r["id"],
                        r["codice_doc"], r["data"])
        db.salva_tavole_release(r["id"], cartella, tavole, url_disegno)
        if i % 50 == 0 or i == len(da_fare):
            print(f"  {i}/{len(da_fare)} release", flush=True)


# ----------------------------------------------------------------------
# Fase 3: posizioni di ogni tavola
# ----------------------------------------------------------------------
def _scarica_tavola(client, ex, t):
    """Una tavola: prima il disegno, DA SOLO, poi i codici in parallelo.

    Il portale (classic ASP) esegue una richiesta alla volta per sessione:
    misurato, una getPartNumber in coda dietro a un SVG pesante aspetta
    ~10 s. Quindi i due tipi di richiesta non si mescolano mai: mentre si
    scarica il disegno non parte altro. Le getPartNumber invece sono
    minuscole, e in parallelo (anche se il server le esegue in fila)
    nascondono la latenza di rete: ~25/s contro ~16/s in sequenza.
    Un solo login: niente sessioni multiple sull'account dell'azienda.

    L'SVG si legge solo per sapere quali numeri sono cliccabili e poi si
    scarta: nel DB resta solo il suo URL (tavole.url_disegno)."""
    posizioni = parser.posizioni_svg(client.svg_tavola(t["cartella"], t["id"]))
    codici = ex.map(lambda p: client.codice_posizione(p[0], p[1], p[2]), posizioni)
    return [(riga, etichetta, codice)
            for (_, _, riga, etichetta), codice in zip(posizioni, codici)]


def fase_posizioni(client, db, stop, paralleli, limite=None):
    da_fare = db.tavole_da_scaricare(limite)
    totale = len(da_fare)
    if not totale:
        print("Posizioni: nessuna tavola da scaricare")
        return
    print(f"Posizioni: {totale} tavole da scaricare, una alla volta, "
          f"codici {paralleli} in parallelo", flush=True)
    t0, fatte, posizioni, errori_di_fila = time.time(), 0, 0, 0
    r0 = client.n_richieste
    with ThreadPoolExecutor(paralleli) as ex:
        for t in da_fare:
            if stop.is_set():
                raise Interrotto()
            try:
                pos = _scarica_tavola(client, ex, t)
            except KeyboardInterrupt:
                stop.set()      # i codici in volo si fermano alla prossima richiesta
                raise
            except (requests.HTTPError, MaschioNonRaggiungibile, SessioneScaduta) as e:
                errori_di_fila += 1
                client.delay = min(RITARDO_MASSIMO, max(client.delay, 0.05) * 1.5)
                log.warning("tavola %s (release %s): %s -> ritardo %.2fs",
                            t["id"], t["release"], e, client.delay)
                if errori_di_fila >= ERRORI_DI_FILA_PER_FERMARSI:
                    print(f"\n{errori_di_fila} errori di fila: mi fermo. "
                          "Vedi dati/log/scarica_maschio.log", flush=True)
                    return
                continue
            errori_di_fila = 0
            if not pos:
                log.info("tavola %s (release %s): nessuna posizione",
                         t["id"], t["release"])
            db.salva_posizioni(t["id"], pos)
            fatte += 1
            posizioni += len(pos)
            if fatte % 100 == 0 or fatte == totale:
                dt = time.time() - t0
                rps = (client.n_richieste - r0) / dt
                restano = (totale - fatte) * dt / fatte
                print(f"  {fatte}/{totale} tavole, {posizioni} posizioni  "
                      f"({rps:.1f} rich/s, mancano ~{restano / 3600:.1f} h)",
                      flush=True)


# ----------------------------------------------------------------------
# Fase 4: anagrafica
# ----------------------------------------------------------------------
def _prefisso(client, db, prefisso, stop):
    html = client.anagrafica_cerca(prefisso)
    _, pagine, record = parser.paginazione(html)
    letti = parser.anagrafica(html)
    db.salva_ricambi(letti)
    n, p = len(letti), 2
    while p <= pagine:
        if stop.is_set():
            raise Interrotto()
        try:
            html = client.anagrafica_pagina(p)
        except SessioneScaduta:
            # nuovo login = ricerca persa: la si rifa' e si riprende da p
            client.anagrafica_cerca(prefisso)
            continue
        corrente, _, _ = parser.paginazione(html)
        if corrente != p:
            log.warning("anagrafica %s: chiesta pagina %d, ricevuta %d: rifaccio "
                        "la ricerca", prefisso, p, corrente)
            client.anagrafica_cerca(prefisso)
            continue
        letti = parser.anagrafica(html)
        db.salva_ricambi(letti)
        n += len(letti)
        p += 1
        if p % 50 == 0:
            print(f"    {prefisso}: pagina {p}/{pagine}", flush=True)
    if record and n != record:
        log.warning("anagrafica %s: attesi %d codici, letti %d", prefisso, record, n)
    return n, record


def fase_anagrafica(client, db, stop, aggiorna=False):
    print("Anagrafica ricambi", flush=True)
    for pre in PREFISSI:
        chiave = f"ana:{pre}"
        if db.fatto(chiave) and not aggiorna:
            continue
        n, record = _prefisso(client, db, pre, stop)
        db.segna_fatto(chiave)
        print(f"  {pre}: {n} codici (il portale ne dichiara {record})", flush=True)


# ----------------------------------------------------------------------
def stampa_stato(db):
    s = db.statistiche()
    mb = os.path.getsize(db.path) / 1e6
    print("--- stato maschio.db ---")
    print(f"linee: {s['linee']}   modelli: {s['modelli']}")
    print(f"release: {s['release']} (tavole lette: {s['release_lette']}, "
          f"senza modello: {s['release_senza_modello']})")
    pct = 100 * s["tavole_scaricate"] / s["tavole"] if s["tavole"] else 0
    print(f"tavole: {s['tavole_scaricate']}/{s['tavole']} scaricate ({pct:.1f}%)")
    print(f"posizioni: {s['posizioni']} (senza codice: {s['posizioni_senza_codice']})")
    print(f"ricambi in anagrafica: {s['ricambi']} (con foto: {s['ricambi_con_foto']})")
    fatti = [p for p in PREFISSI if db.fatto(f"ana:{p}")]
    print(f"prefissi anagrafica completati: {len(fatti)}/{len(PREFISSI)}")
    print(f"DB: {mb:.0f} MB")


def _configura_log():
    cartella = config.DATI / "log"
    cartella.mkdir(parents=True, exist_ok=True)
    h = logging.FileHandler(cartella / "scarica_maschio.log", encoding="utf-8")
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    radice = logging.getLogger()
    radice.setLevel(logging.INFO)
    radice.addHandler(h)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--paralleli", type=int, default=5,
                    help="richieste di codici in parallelo dentro una tavola")
    ap.add_argument("--ritardo", type=float, default=0.03)
    ap.add_argument("--limite", type=int,
                    help="prova: legge le tavole di sole N release e scarica solo N tavole")
    ap.add_argument("--solo-elenco", action="store_true")
    ap.add_argument("--solo-anagrafica", action="store_true")
    ap.add_argument("--aggiorna-elenco", action="store_true")
    ap.add_argument("--aggiorna-anagrafica", action="store_true")
    ap.add_argument("--stato", action="store_true")
    a = ap.parse_args()

    db = Db()
    if a.stato:
        stampa_stato(db)
        return
    _configura_log()
    stop = threading.Event()
    client = MaschioClient(delay=a.ritardo, pool=a.paralleli + 4)
    _con_freno_interrompibile(client, stop)
    t0 = time.time()
    try:
        if a.solo_anagrafica:
            fase_anagrafica(client, db, stop, a.aggiorna_anagrafica)
        else:
            fase_elenco(client, db, a.aggiorna_elenco)
            if not a.solo_elenco:
                fase_tavole(client, db, stop, a.limite)
                fase_posizioni(client, db, stop, a.paralleli, a.limite)
    except (KeyboardInterrupt, Interrotto):
        stop.set()
        print("\nInterrotto: riparte da qui al prossimo avvio.", flush=True)
    print(f"\n{client.n_richieste} richieste in {(time.time() - t0) / 60:.1f} min")
    stampa_stato(db)


if __name__ == "__main__":
    main()
