"""
Scarica in locale l'intero catalogo Antonio Carraro in
fornitori/carraro/carraro.db.

Due fasi:
  1. elenco: per ogni sezione legge categorie e pagine prodotto, e salva
     prodotti e legami prodotto-tavola. Le tavole nuove finiscono nel DB
     come "da scaricare". ~600 richieste, ~6 minuti.
  2. tavole: scarica una per una le tavole ancora da scaricare
     (~9.400 la prima volta, ~80 minuti). Una tavola condivisa da piu'
     prodotti si scarica una volta sola.

Interruzione e ripresa: Ctrl+C ferma dopo la richiesta in corso. Al
riavvio riparte da dove era: i prodotti gia' letti e le tavole gia'
scaricate sono segnati nel DB e non si riscaricano. Ogni tavola si salva
in una sola transazione, quindi non ne restano di salvate a meta'.

Aggiornamento periodico: --aggiorna-elenco rilegge tutti i prodotti (le
tavole nuove comparse sul sito vengono messe in coda); le tavole gia'
scaricate restano quelle che sono, salvo --riscarica-tavole.

Uso (dalla radice del progetto):
  ./venv/Scripts/python.exe -m fornitori.carraro.scarica_tutto                  # tutto
  ./venv/Scripts/python.exe -m fornitori.carraro.scarica_tutto --limite 20      # prova: solo 20 tavole
  ./venv/Scripts/python.exe -m fornitori.carraro.scarica_tutto --solo-elenco    # solo fase 1
  ./venv/Scripts/python.exe -m fornitori.carraro.scarica_tutto --sezioni serie,motori
  ./venv/Scripts/python.exe -m fornitori.carraro.scarica_tutto --aggiorna-elenco
  ./venv/Scripts/python.exe -m fornitori.carraro.scarica_tutto --solo-allegati  # solo i PDF allegati ai prodotti
  ./venv/Scripts/python.exe -m fornitori.carraro.scarica_tutto --stato          # solo il punto della situazione
"""

import argparse
import logging
import time

import requests

from . import parser
from .client import CarraroClient, CarraroNonRaggiungibile
from .db import Db, chiave_tavola

log = logging.getLogger("carraro.scarica_tutto")

# sezioni del menu "Catalogo" con contenuto (misurate con
# debug/misura_catalogo_carraro.py; kit-service risultava vuota)
SEZIONI = ["serie", "motori", "kits-aftermarket", "rasaerba", "contenitori",
           "accessori", "special-tools", "materiali-di-consumo"]


# ----------------------------------------------------------------------
# Fase 1: elenco prodotti e tavole
# ----------------------------------------------------------------------
def _prodotti_categoria(client, url):
    """Prodotti di una categoria, seguendo la paginazione del sito."""
    da_visitare, visitate, prodotti = [url], set(), []
    while da_visitare:
        u = da_visitare.pop(0)
        if u in visitate:
            continue
        visitate.add(u)
        html = client.get(u)
        prodotti += parser.parse_categoria(html)["prodotti"]
        da_visitare += [p for p in parser.link_pagine(html) if p not in visitate]
    # la paginazione puo' rimandare alla prima pagina con un altro URL
    return list({p["url"]: p for p in prodotti}.values())


def elenco_sezione(client, db, sezione, aggiorna=False):
    chiave = f"sez:{sezione}"
    if db.fatto(chiave) and not aggiorna:
        print(f"  {sezione}: elenco gia' fatto")
        return
    radice = f"/catalogo/{sezione}/"
    indice = parser.parse_categoria(client.get(radice))
    categorie = indice["sottocategorie"] or [
        # sezione piccola: la pagina elenca direttamente i prodotti
        {"nome": indice["titolo"] or sezione, "url": radice}]

    coda = []
    for cat in categorie:
        trovati = _prodotti_categoria(client, cat["url"])
        trovati = [{**p, "categoria": cat["nome"]} for p in trovati]
        db.salva_prodotti(sezione, trovati)
        coda += trovati
    print(f"  {sezione}: {len(categorie)} categorie, {len(coda)} prodotti", flush=True)

    visti = set()
    while coda:
        p = coda.pop(0)
        if p["url"] in visti:
            continue
        visti.add(p["url"])
        chiave_p = f"prod:{p['url']}"
        if db.fatto(chiave_p) and not aggiorna:
            continue
        html = client.get(p["url"])
        tavole = parser.parse_modello(html)["tavole"]
        allegati = parser.parse_allegati(html)
        if not tavole:
            # alcuni "prodotti" sono in realta' pagine che raccolgono altri
            # prodotti: si aggiungono in coda
            figli = parser.parse_categoria(html)["prodotti"]
            figli = [{**f, "categoria": p.get("categoria")} for f in figli
                     if f["url"] not in visti]
            if figli:
                db.salva_prodotti(sezione, figli)
                coda += figli
                log.info("%s: nessuna tavola, %d prodotti dentro", p["url"], len(figli))
            elif allegati:
                # solo catalogo PDF (Agritalia, alcuni motori): caso noto
                log.info("%s: solo allegati (%s)", p["url"],
                         ", ".join(a["nome"] for a in allegati))
            else:
                log.warning("%s: nessuna tavola, nessun prodotto dentro e "
                            "nessun allegato: pagina da controllare", p["url"])
        db.salva_tavole_prodotto(p["url"], tavole)
        db.salva_allegati(p["url"], allegati)
        db.segna_fatto(chiave_p)
        if len(visti) % 25 == 0:
            print(f"    {sezione}: {len(visti)} prodotti letti", flush=True)
    db.segna_fatto(chiave)


def aggiorna_allegati(client, db):
    """Rilegge le pagine di tutti i prodotti gia' in DB e salva solo gli
    allegati. Serve per i DB creati prima che gli allegati venissero
    salvati: non tocca tavole ne' legami."""
    prodotti = db.prodotti()
    print(f"Allegati: rileggo {len(prodotti)} pagine prodotto", flush=True)
    con_allegati = 0
    for i, p in enumerate(prodotti, 1):
        allegati = parser.parse_allegati(client.get(p["url"]))
        db.salva_allegati(p["url"], allegati)
        con_allegati += bool(allegati)
        if i % 50 == 0 or i == len(prodotti):
            print(f"  {i}/{len(prodotti)} prodotti, {con_allegati} con allegati",
                  flush=True)


# ----------------------------------------------------------------------
# Fase 2: tavole
# ----------------------------------------------------------------------
def scarica_tavole(client, db, limite=None):
    da_fare = db.tavole_da_scaricare(limite)
    totale = len(da_fare)
    if not totale:
        print("Nessuna tavola da scaricare.")
        return 0, []
    print(f"Tavole da scaricare: {totale}", flush=True)
    t0, fatte, errori = time.time(), 0, []
    for t in da_fare:
        # il link preso dalla pagina prodotto; in alternativa l'indirizzo
        # diretto /<codice>, valido per ogni tavola. Il secondo serve anche
        # quando il sito mette un link incompleto (visto su 0512-4741:
        # ".../tcd2-9l4-ht-8700/" senza il codice, che apre il motore)
        diretto = "/" + t["codice_sito"]
        tentativi = [u for u in (t["url"], diretto) if u]
        tentativi = list(dict.fromkeys(tentativi))
        # un errore HTTP (es. 500 su un link) non ferma i tentativi: si
        # prova comunque anche l'indirizzo diretto
        dati, url, errore_http = None, None, None
        for url in tentativi:
            try:
                dati = parser.parse_tavola(client.get(url))
            except requests.HTTPError as e:
                errore_http = e
                continue
            if chiave_tavola(dati["codice"]) == t["codice"]:
                break
        if dati is None:
            # resta "da scaricare": verra' ritentata al prossimo avvio
            errori.append(f"{t['codice']}: {errore_http}")
            log.warning("tavola %s (%s): %s", t["codice"], url, errore_http)
            continue
        if chiave_tavola(dati["codice"]) != t["codice"]:
            # il sito ha risposto con un'altra pagina (home, modello, ...):
            # NON si salva, altrimenti risulterebbe scaricata con 0 righe e
            # non verrebbe piu' ritentata
            errori.append(f"{t['codice']}: la pagina ricevuta e' "
                          f"'{dati['codice'] or 'senza codice tavola'}'")
            log.warning("tavola %s (%s): pagina inattesa (%s), resta in coda",
                        t["codice"], url, dati["codice"] or "senza codice tavola")
            continue
        if not dati["righe"]:
            log.warning("tavola %s: nessuna riga", t["codice"])
        db.salva_tavola(t["codice"], dati)
        fatte += 1
        if fatte % 50 == 0 or fatte == totale:
            trascorso = time.time() - t0
            restano = (totale - fatte) * trascorso / fatte
            print(f"  {fatte}/{totale} tavole  ({fatte / trascorso:.1f}/s, "
                  f"mancano ~{restano / 60:.0f} min)", flush=True)
    return fatte, errori


# ----------------------------------------------------------------------
def stampa_stato(db):
    s = db.statistiche()
    print("--- stato carraro.db ---")
    print(f"prodotti:          {s['prodotti']}  {s['prodotti_per_sezione']}")
    print(f"tavole scaricate:  {s['tavole_scaricate']} / {s['tavole']}")
    print(f"righe:             {s['righe']}")
    print(f"allegati:          {s['allegati']}  "
          f"(prodotti solo PDF: {s['prodotti_solo_pdf']})")
    print(f"codici precedenti: {s['codici_precedenti']}")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--sezioni", default=",".join(SEZIONI),
                    help="sezioni separate da virgola (default: tutte)")
    ap.add_argument("--limite", type=int, help="scarica al massimo N tavole")
    ap.add_argument("--solo-elenco", action="store_true", help="solo fase 1")
    ap.add_argument("--aggiorna-elenco", action="store_true",
                    help="rilegge anche sezioni e prodotti gia' letti")
    ap.add_argument("--riscarica-tavole", action="store_true",
                    help="rimette in coda TUTTE le tavole (aggiornamento completo)")
    ap.add_argument("--ritardo", type=float, default=0.5,
                    help="pausa minima fra le richieste in secondi (default 0.5)")
    ap.add_argument("--solo-allegati", action="store_true",
                    help="rilegge le pagine prodotto e salva solo gli allegati")
    ap.add_argument("--stato", action="store_true", help="mostra lo stato e esce")
    args = ap.parse_args()
    logging.basicConfig(level=logging.WARNING,
                        format="%(levelname)s %(name)s: %(message)s")

    db = Db()
    if args.stato:
        stampa_stato(db)
        return
    if args.riscarica_tavole:
        db._write([("UPDATE tavole SET scaricata_il = NULL", (), False)])

    t0 = time.time()
    errori = []
    try:
        client = CarraroClient(delay=args.ritardo)
        if args.solo_allegati:
            aggiorna_allegati(client, db)
        else:
            print("Fase 1: elenco prodotti")
            for sez in [s.strip() for s in args.sezioni.split(",") if s.strip()]:
                elenco_sezione(client, db, sez, aggiorna=args.aggiorna_elenco)
        if not (args.solo_elenco or args.solo_allegati):
            print("Fase 2: tavole")
            _, errori = scarica_tavole(client, db, args.limite)
    except KeyboardInterrupt:
        print("\nInterrotto: al prossimo avvio riparte da qui.")
    except CarraroNonRaggiungibile as e:
        print(f"\nPortale non raggiungibile, mi fermo: {e}")

    print(f"\nTempo: {(time.time() - t0) / 60:.1f} min")
    if errori:
        print(f"Tavole con errore ({len(errori)}), verranno ritentate al prossimo avvio:")
        for e in errori[:20]:
            print("  -", e)
    stampa_stato(db)


if __name__ == "__main__":
    main()
