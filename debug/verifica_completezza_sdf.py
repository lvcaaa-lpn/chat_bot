"""Verifica a campione che sdf.db contenga tutto quello che il portale mostra.

Due controlli, entrambi in sola lettura sul DB e con poche richieste al
portale (una alla volta, --ritardo secondi tra l'una e l'altra):

  A. TAVOLE: riscarica N tavole scelte a caso (ripartite per marca, piu'
     tutte quelle che nel DB hanno 0 ricambi) e confronta le righe ricambio
     (codice + posizione) con quelle salvate. Serve soprattutto perche' le 4
     marche trattori sono state scaricate con il VECCHIO account: se quello
     vedeva meno ricambi per tavola, qui emerge.
  B. CODICI (il metodo del collega, al contrario): per M codici a caso
     chiede al portale "dove e' usato" (searchPartApplicabilities) e
     controlla che ogni tavola indicata dal portale sia nel DB, collegata
     allo stesso modello.

Risultato completo in dati/sdf_verifica_completezza.json.

Uso (dalla radice del progetto):
  ./venv/Scripts/python.exe debug/verifica_completezza_sdf.py                       # 200 tavole, 300 codici
  ./venv/Scripts/python.exe debug/verifica_completezza_sdf.py --tavole 5 --codici 5 # prova veloce
"""
import argparse
import json
import random
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8")

import config  # noqa: E402
from fornitori.sdf.api import SdfApi  # noqa: E402
from fornitori.sdf.client import BRANDS, SdfClient  # noqa: E402
from fornitori.sdf.db import Db  # noqa: E402

OUT = config.DATI / "sdf_verifica_completezza.json"


def client(ritardo):
    return SdfClient(
        cookie=config.leggi_credenziale("cookie.txt", "SDF_COOKIE"),
        username=config.leggi_credenziale("sdf_username.txt", "SDF_USERNAME"),
        password=config.leggi_credenziale("sdf_password.txt", "SDF_PASSWORD"),
        delay=ritardo)


# ------------------------------------------------------------------ A
def campione_tavole(db, n):
    marche = list(BRANDS.values())
    per_marca = max(1, n // len(marche))
    righe = []
    for b in marche:
        revs = [r["revision_id"] for r in db.query(
            "SELECT DISTINCT revision_id FROM model_drawing WHERE brand=?", (b,))]
        righe += [(b, rev) for rev in random.sample(revs, min(per_marca, len(revs)))]
    vuote = db.query("""SELECT d.revision_id,
                               (SELECT brand FROM model_drawing md
                                WHERE md.revision_id=d.revision_id LIMIT 1) AS brand
                        FROM drawing d
                        WHERE NOT EXISTS (SELECT 1 FROM part p WHERE p.revision_id=d.revision_id)""")
    righe += [(r["brand"], r["revision_id"]) for r in vuote]
    return righe


def verifica_tavole(db, api, campione):
    esiti, problemi = Counter(), []
    for i, (brand, rev) in enumerate(campione, 1):
        link = db.one("SELECT * FROM model_drawing WHERE brand=? AND revision_id=? LIMIT 1",
                      (brand, rev))
        d = api.drawing(link["family_id"], link["model_id"], link["group_id"],
                        link["subgroup_id"], rev, brand=brand)
        sito = {(p["position"], p["code"]) for p in d["parts"]}
        nel_db = {(r["position"], r["code"]) for r in db.query(
            "SELECT position, code FROM part WHERE revision_id=?", (rev,))}
        mancano, in_piu = sito - nel_db, nel_db - sito
        if d["got_revision"] != rev:
            esito = "revisione diversa"
        elif not mancano and not in_piu:
            esito = "identica"
        elif mancano and not in_piu:
            esito = "MANCANO ricambi nel DB"
        elif in_piu and not mancano:
            esito = "DB ha ricambi in piu'"
        else:
            esito = "DIVERSA"
        esiti[esito] += 1
        if esito != "identica":
            problemi.append({"brand": brand, "revision_id": rev, "esito": esito,
                             "ricevuta": d["got_revision"],
                             "sito": len(sito), "db": len(nel_db),
                             "mancano": sorted(mancano)[:20], "in_piu": sorted(in_piu)[:20]})
        print(f"\r  A tavole {i}/{len(campione)}  {dict(esiti)}", end="", flush=True)
    print()
    return esiti, problemi


# ------------------------------------------------------------------ B
def campione_codici(db, n):
    marche = list(BRANDS.values())
    per_marca = max(1, n // len(marche))
    out = []
    for b in marche:
        # un codice a caso da ciascuna di N tavole a caso della marca
        # (un campione diretto su 4 milioni di righe part sarebbe lento)
        revs = [r["revision_id"] for r in db.query(
            "SELECT DISTINCT revision_id FROM model_drawing WHERE brand=?", (b,))]
        trovati = set()
        for rev in random.sample(revs, min(len(revs), per_marca * 2)):
            codici = [r["code"] for r in db.query(
                "SELECT code FROM part WHERE revision_id=? AND code <> ''", (rev,))]
            if codici:
                trovati.add(random.choice(codici))
            if len(trovati) >= per_marca:
                break
        out += [(b, c) for c in trovati]
    return out


def verifica_codici(db, api, campione, grezzi):
    esiti, problemi = Counter(), []
    for i, (brand, code) in enumerate(campione, 1):
        nodi = api.applicabilities(code, brand=brand)
        if len(grezzi) < 5:
            grezzi.append({"brand": brand, "code": code, "nodi": nodi[:10]})
        # senza filtri il portale risponde a livello di FAMIGLIA: si
        # confrontano le famiglie che secondo il portale usano il codice con
        # quelle che nel DB lo hanno in almeno una tavola
        sito = {int(n["family_id"]) for n in nodi
                if n["level"] == "family" and n["family_id"] and n["brand"] == brand}
        nel_db = {r["family_id"] for r in db.query(
            """SELECT DISTINCT md.family_id FROM part p
               JOIN model_drawing md ON md.revision_id = p.revision_id AND md.brand = ?
               WHERE p.code = ?""", (brand, code))}
        mancano, in_piu = sito - nel_db, nel_db - sito
        if not sito:
            esito = "portale: nessuna famiglia"
        elif mancano:
            esito = "MANCANO famiglie nel DB"
        elif in_piu:
            esito = "DB ha famiglie in piu'"
        else:
            esito = "identico"
        esiti[esito] += 1
        if esito != "identico":
            nomi = lambda ids: [  # noqa: E731
                (f, (db.one("SELECT name FROM family WHERE brand=? AND row_id=?",
                            (brand, f)) or {"name": None})["name"]) for f in sorted(ids)]
            problemi.append({"brand": brand, "code": code, "esito": esito,
                             "famiglie_sito": len(sito), "famiglie_db": len(nel_db),
                             "mancano": nomi(mancano)[:20], "in_piu": nomi(in_piu)[:20]})
        print(f"\r  B codici {i}/{len(campione)}  {dict(esiti)}", end="", flush=True)
    print()
    return esiti, problemi


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tavole", type=int, default=200)
    ap.add_argument("--codici", type=int, default=300)
    ap.add_argument("--ritardo", type=float, default=0.5)
    ap.add_argument("--seme", type=int, default=1)
    a = ap.parse_args()
    random.seed(a.seme)

    db = Db()
    api = SdfApi(client(a.ritardo))
    t0 = time.time()
    rapporto = {"grezzi_applicabilities": []}

    if a.tavole:
        camp = campione_tavole(db, a.tavole)
        print(f"A. confronto {len(camp)} tavole con il portale")
        esiti, prob = verifica_tavole(db, api, camp)
        rapporto["tavole"] = {"esiti": dict(esiti), "problemi": prob}
    if a.codici:
        camp = campione_codici(db, a.codici)
        print(f"B. 'dove e' usato' per {len(camp)} codici")
        esiti, prob = verifica_codici(db, api, camp, rapporto["grezzi_applicabilities"])
        rapporto["codici"] = {"esiti": dict(esiti), "problemi": prob}

    OUT.write_text(json.dumps(rapporto, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\nfatto in {(time.time() - t0) / 60:.1f} min, dettagli in {OUT}")


if __name__ == "__main__":
    main()
