"""Recupera le sostituzioni mancanti in sdf.db.

Durante il crawl, per ogni ricambio segnato 'replaced' si chiede al portale
la catena di sostituzione (cric/part/substitutions). Se quella singola
richiesta falliva, il Crawler andava avanti senza (le sostituzioni sono un
arricchimento, vedi crawler.py) e il codice restava fuori da
substitution_checked. Questo script riprova solo quei codici.

Uso (dalla radice del progetto):
  ./venv/Scripts/python.exe debug/completa_sostituzioni_sdf.py            # solo conteggio
  ./venv/Scripts/python.exe debug/completa_sostituzioni_sdf.py --scarica
"""
import argparse
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8")

import config  # noqa: E402
from fornitori.sdf.api import SdfApi  # noqa: E402
from fornitori.sdf.client import SdfClient  # noqa: E402
from fornitori.sdf.db import Db  # noqa: E402

SQL_MANCANTI = """
    SELECT m.code,
           (SELECT md.brand FROM model_drawing md
            WHERE md.revision_id = m.rev LIMIT 1) AS brand
    FROM (SELECT p.code, MIN(p.revision_id) AS rev
          FROM part p
          WHERE p.replaced = 1 AND p.code <> ''
            AND NOT EXISTS (SELECT 1 FROM substitution_checked s WHERE s.code = p.code)
          GROUP BY p.code) m
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scarica", action="store_true")
    ap.add_argument("--ritardo", type=float, default=0.5)
    a = ap.parse_args()

    db = Db()
    mancanti = db.query(SQL_MANCANTI)
    print(f"codici 'replaced' senza sostituzione verificata: {len(mancanti)} "
          f"{dict(Counter(r['brand'] for r in mancanti))}")
    if not a.scarica or not mancanti:
        return

    api = SdfApi(SdfClient(
        cookie=config.leggi_credenziale("cookie.txt", "SDF_COOKIE"),
        username=config.leggi_credenziale("sdf_username.txt", "SDF_USERNAME"),
        password=config.leggi_credenziale("sdf_password.txt", "SDF_PASSWORD"),
        delay=a.ritardo))
    esiti = Counter()
    esempi = []
    for i, r in enumerate(mancanti, 1):
        try:
            subs = api.substitutions(r["code"], brand=r["brand"])
        except Exception as e:  # noqa: BLE001 - si conta e si va avanti
            esiti[f"errore {type(e).__name__}"] += 1
            continue
        db.save_substitutions(r["code"], subs)
        esiti["con sostituto" if subs else "nessun sostituto"] += 1
        if subs and len(esempi) < 8:
            esempi.append(f"{r['code']} -> {', '.join(s['code'] for s in subs)}")
        print(f"\r  {i}/{len(mancanti)} {dict(esiti)}", end="", flush=True)
    print()
    for e in esempi:
        print("  " + e)
    print(f"rimasti: {len(db.query(SQL_MANCANTI))}")


if __name__ == "__main__":
    main()
