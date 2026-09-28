"""
Misura le dimensioni del catalogo Antonio Carraro (sezione SERIE), per
decidere tra download completo e cache su richiesta.

Scarica SOLO indice, pagine di categoria e pagine modello (circa 600
richieste, ~5-10 minuti): NON scarica le tavole. Conta quante tavole
uniche ci sono, visto che la stessa tavola (es. a01-0050-01) e' condivisa
da piu' modelli.

Uso (dalla radice del progetto):
    ./venv/Scripts/python.exe debug/misura_catalogo_carraro.py

Il risultato completo (prodotti e tavole per prodotto) finisce in
dati/carraro_misura.json: e' riusabile come punto di partenza per il
crawler vero. Ctrl+C interrompe e salva comunque quanto raccolto.
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

RADICE_CATALOGO = "/catalogo/serie/"
USCITA = config.DATI / "carraro_misura.json"


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


def main():
    t0 = time.time()
    c = CarraroClient()
    indice = parser.parse_categoria(c.get(RADICE_CATALOGO))
    categorie = indice["sottocategorie"]
    attesi = sum(x["n_prodotti"] or 0 for x in categorie)
    print(f"{len(categorie)} categorie finali, {attesi} prodotti dichiarati dal sito")

    prodotti, richieste, anomalie = {}, 1, []
    tavole_per_prodotto = {}
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
                print(f"  modelli {i}/{len(prodotti)}", flush=True)
    except KeyboardInterrupt:
        print("\nInterrotto: salvo quanto raccolto finora.")

    # --- statistiche ---------------------------------------------------
    # i codici tavola compaiono sia maiuscoli che minuscoli (A00-0100-02 /
    # a01-0050-01): per contarli come unici si confrontano in minuscolo
    uso = Counter(t["codice"].lower()
                  for tav in tavole_per_prodotto.values() for t in tav)
    riferimenti = sum(uso.values())
    tipi = Counter(p["tipo"] for p in prodotti.values())
    durata = time.time() - t0

    print("\n=== RISULTATO ===")
    print(f"prodotti unici:        {len(prodotti)}  {dict(tipi)}")
    print(f"prodotti analizzati:   {len(tavole_per_prodotto)}")
    print(f"riferimenti a tavole:  {riferimenti}")
    print(f"tavole UNICHE:         {len(uso)}")
    if uso:
        print(f"riuso medio:           {riferimenti / len(uso):.1f} modelli per tavola")
        print("tavole piu' condivise:",
              ", ".join(f"{k} ({v})" for k, v in uso.most_common(5)))
    print(f"richieste fatte:       {richieste} in {durata / 60:.1f} min "
          f"({durata / max(richieste, 1):.2f} s/richiesta)")
    if uso:
        stima = len(uso) * durata / max(richieste, 1)
        print(f"STIMA download di tutte le tavole: ~{stima / 60:.0f} min "
              f"({len(uso)} richieste allo stesso ritmo)")
    if anomalie:
        print("\nATTENZIONE, categorie con conteggio diverso dal dichiarato:")
        for a in anomalie:
            print("  -", a)

    USCITA.write_text(json.dumps({
        "prodotti": list(prodotti.values()),
        "tavole_per_prodotto": tavole_per_prodotto,
        "anomalie": anomalie,
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\nDettaglio salvato in {USCITA}")


if __name__ == "__main__":
    main()
