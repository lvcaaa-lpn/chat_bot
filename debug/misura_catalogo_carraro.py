"""
Misura le dimensioni del catalogo Antonio Carraro, sezione per sezione,
per decidere tra download completo e cache su richiesta.

Per ogni sezione scarica SOLO l'indice, le pagine di categoria e le pagine
prodotto: NON scarica le tavole. Conta quante tavole uniche ci sono,
visto che la stessa tavola (es. a01-0050-01) e' condivisa da piu' modelli.

Uso (dalla radice del progetto):
    ./venv/Scripts/python.exe debug/misura_catalogo_carraro.py            # solo serie
    ./venv/Scripts/python.exe debug/misura_catalogo_carraro.py motori accessori ...

Le sezioni sono i nomi nel menu "Catalogo" del sito, come compaiono
nell'URL (/catalogo/<sezione>/): serie, motori, contenitori, rasaerba,
kit-service, kits-aftermarket, accessori, special-tools,
materiali-di-consumo.

Il dettaglio di ogni sezione (prodotti e tavole per prodotto) finisce in
dati/carraro_misura_<sezione>.json: e' riusabile come punto di partenza
per il crawler vero. Ctrl+C interrompe e salva comunque quanto raccolto.
"""

import json
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
from fornitori.carraro import parser  # noqa: E402
from fornitori.carraro.client import CarraroClient  # noqa: E402


def uscita(sezione):
    return config.DATI / f"carraro_misura_{sezione}.json"


def pagine_categoria(client, url):
    """Tutti i prodotti di una categoria, seguendo la paginazione."""
    da_visitare, visitate, prodotti = [url], set(), []
    while da_visitare:
        u = da_visitare.pop(0)
        if u in visitate:
            continue
        visitate.add(u)
        html = client.get(u)
        prodotti += parser.parse_categoria(html)["prodotti"]
        da_visitare += [p for p in parser.link_pagine(html) if p not in visitate]
    # la paginazione puo' rimandare alla prima pagina con un URL diverso
    # (.../page_0/): senza dedup gli stessi prodotti verrebbero contati due volte
    unici = list({p["url"]: p for p in prodotti}.values())
    return unici, len(visitate)


def misura_sezione(c, sezione):
    """Misura una sezione. Solleva KeyboardInterrupt dopo aver salvato,
    cosi' il chiamante interrompe anche le sezioni successive."""
    t0 = time.time()
    radice = f"/catalogo/{sezione}/"
    print(f"\n##### SEZIONE {sezione.upper()} ({radice})")
    indice = parser.parse_categoria(c.get(radice))
    categorie = indice["sottocategorie"]
    if not categorie:
        # sezione piccola: la pagina della sezione elenca direttamente i
        # prodotti, senza pannelli di sottocategorie -> e' lei la categoria
        categorie = [{"nome": indice["titolo"] or sezione, "url": radice,
                      "n_prodotti": None}]
        print("nessuna sottocategoria: la sezione elenca direttamente i prodotti")
    else:
        attesi = sum(x["n_prodotti"] or 0 for x in categorie)
        print(f"{len(categorie)} categorie finali, {attesi} prodotti dichiarati dal sito")

    prodotti, richieste, anomalie = {}, 1, []
    tavole_per_prodotto = {}
    interrotto = False
    try:
        for i, cat in enumerate(categorie, 1):
            trovati, n_pag = pagine_categoria(c, cat["url"])
            richieste += n_pag
            if cat["n_prodotti"] is not None and len(trovati) != cat["n_prodotti"]:
                anomalie.append(f"{cat['url']}: attesi {cat['n_prodotti']}, "
                                f"trovati {len(trovati)} in {n_pag} pagine")
            for p in trovati:
                prodotti.setdefault(p["url"], {**p, "categoria": cat["nome"]})
            print(f"[{i}/{len(categorie)}] {cat['nome']}: {len(trovati)} prodotti "
                  f"({n_pag} pag.)", flush=True)

        for i, (url, p) in enumerate(prodotti.items(), 1):
            m = parser.parse_modello(c.get(url))
            richieste += 1
            tavole_per_prodotto[url] = [
                {"codice": t["codice"], "titolo": t["titolo"], "gruppo": t["gruppo"]}
                for t in m["tavole"]]
            if i % 25 == 0 or i == len(prodotti):
                print(f"  prodotti {i}/{len(prodotti)}", flush=True)
    except KeyboardInterrupt:
        print("\nInterrotto: salvo quanto raccolto finora.")
        interrotto = True

    # --- statistiche ---------------------------------------------------
    # i codici tavola compaiono sia maiuscoli che minuscoli (A00-0100-02 /
    # a01-0050-01): per contarli come unici si confrontano in minuscolo
    uso = Counter(t["codice"].lower()
                  for tav in tavole_per_prodotto.values() for t in tav)
    riferimenti = sum(uso.values())
    tipi = Counter(p["tipo"] for p in prodotti.values())
    # prodotti la cui pagina non ha la lista tavole: probabilmente non sono
    # macchine ma articoli singoli (consumabili, accessori...) -> da guardare
    senza_tavole = [u for u, t in tavole_per_prodotto.items() if not t]
    durata = time.time() - t0

    print(f"\n=== RISULTATO {sezione.upper()} ===")
    print(f"prodotti unici:        {len(prodotti)}  {dict(tipi)}")
    print(f"prodotti analizzati:   {len(tavole_per_prodotto)}")
    print(f"prodotti SENZA tavole: {len(senza_tavole)}"
          + (f"  es. {', '.join(senza_tavole[:3])}" if senza_tavole else ""))
    print(f"riferimenti a tavole:  {riferimenti}")
    print(f"tavole UNICHE:         {len(uso)}")
    if uso:
        print(f"riuso medio:           {riferimenti / len(uso):.1f} prodotti per tavola")
        print("tavole piu' condivise:",
              ", ".join(f"{k} ({v})" for k, v in uso.most_common(5)))
    print(f"richieste fatte:       {richieste} in {durata / 60:.1f} min "
          f"({durata / max(richieste, 1):.2f} s/richiesta)")
    if anomalie:
        print("ATTENZIONE, categorie con conteggio diverso dal dichiarato:")
        for a in anomalie:
            print("  -", a)

    uscita(sezione).write_text(json.dumps({
        "sezione": sezione,
        "prodotti": list(prodotti.values()),
        "tavole_per_prodotto": tavole_per_prodotto,
        "anomalie": anomalie,
        "completa": not interrotto,
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"Dettaglio salvato in {uscita(sezione)}")

    if interrotto:
        raise KeyboardInterrupt
    return set(uso), richieste, durata


def _tavole_da_file(percorso):
    dati = json.loads(percorso.read_text(encoding="utf-8"))
    return {t["codice"].lower()
            for tav in dati["tavole_per_prodotto"].values() for t in tav}


def main(sezioni):
    c = CarraroClient()
    misurate = {}
    tot_richieste, tot_durata = 0, 0.0
    try:
        for sez in sezioni:
            tavole, richieste, durata = misura_sezione(c, sez)
            misurate[sez] = tavole
            tot_richieste += richieste
            tot_durata += durata
    except KeyboardInterrupt:
        print("\nInterrotto: le sezioni gia' completate sono salvate.")

    # --- riepilogo complessivo ----------------------------------------
    # aggiunge le sezioni misurate in passato (file gia' presenti), cosi'
    # l'unione e le sovrapposizioni tengono conto anche di serie
    for f in sorted(config.DATI.glob("carraro_misura_*.json")):
        sez = f.stem.removeprefix("carraro_misura_")
        misurate.setdefault(sez, _tavole_da_file(f))
    if "serie" not in misurate and (config.DATI / "carraro_misura.json").exists():
        # file della prima misura, quando lo script faceva solo serie
        misurate["serie"] = _tavole_da_file(config.DATI / "carraro_misura.json")

    if len(misurate) < 2:
        return
    tutte = set().union(*misurate.values())
    print("\n=== RIEPILOGO TUTTE LE SEZIONI ===")
    for sez, tav in sorted(misurate.items(), key=lambda x: -len(x[1])):
        altre = set().union(*(t for s, t in misurate.items() if s != sez))
        print(f"{sez:22} {len(tav):6} tavole, di cui {len(tav - altre):6} solo qui")
    print(f"{'TOTALE (unione)':22} {len(tutte):6} tavole uniche")
    if tot_richieste:
        ritmo = tot_durata / tot_richieste
        print(f"STIMA download di tutte le tavole: ~{len(tutte) * ritmo / 60:.0f} min "
              f"(a {ritmo:.2f} s/richiesta)")


if __name__ == "__main__":
    main(sys.argv[1:] or ["serie"])
