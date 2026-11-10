"""Completa gli URL delle immagini degli esplosi (drawing.preview_url) in sdf.db.

Perche' alcune sono NULL (analisi 2026-11-10 sul DB completo, 247.490 tavole):

  * 5.584 tavole hanno image_name ma preview_url NULL: sono tutte state
    scaricate fra l'8 agosto e il 18 settembre, PRIMA che api.py imparasse
    a costruire l'URL da imageName (_url_immagine, prima tavola con URL il
    22 settembre). L'URL si ricava dal nome dell'immagine: nessuna
    richiesta al portale.
  * 4 tavole hanno image_name NULL: e' il portale che non ha restituito
    imageName. Con --ricontrolla vengono richieste di nuovo (4 richieste)
    per capire se era un errore momentaneo o se la tavola non ha immagine.

Uso (dalla radice del progetto):
  ./venv/Scripts/python.exe debug/sistema_immagini_sdf.py                 # solo conteggio
  ./venv/Scripts/python.exe debug/sistema_immagini_sdf.py --scrivi        # riempie gli URL ricavabili
  ./venv/Scripts/python.exe debug/sistema_immagini_sdf.py --ricontrolla   # + richiede al portale le tavole senza imageName
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8")

import config  # noqa: E402,F401  (carica .env)
from fornitori.sdf.api import SdfApi, _url_immagine  # noqa: E402
from fornitori.sdf.db import Db  # noqa: E402


def conta(db):
    r = db.one("""SELECT COUNT(*) AS tot,
                         SUM(image_name IS NULL) AS senza_nome,
                         SUM(image_name IS NOT NULL AND preview_url IS NULL) AS ricavabili
                  FROM drawing""")
    print(f"tavole: {r['tot']}   senza imageName: {r['senza_nome']}   "
          f"URL mancante ma ricavabile: {r['ricavabili']}")
    return r


def riempi(db):
    righe = db.query("SELECT revision_id, image_name FROM drawing "
                     "WHERE image_name IS NOT NULL AND preview_url IS NULL")
    valori = [(_url_immagine(r["image_name"]), r["revision_id"]) for r in righe]
    db._write([("UPDATE drawing SET preview_url=? WHERE revision_id=?", valori, True)])
    print(f"URL scritti: {len(valori)}")


def ricontrolla(db):
    from fornitori.sdf.client import SdfClient
    client = SdfClient(
        cookie=config.leggi_credenziale("cookie.txt", "SDF_COOKIE"),
        username=config.leggi_credenziale("sdf_username.txt", "SDF_USERNAME"),
        password=config.leggi_credenziale("sdf_password.txt", "SDF_PASSWORD"),
        delay=1.0)
    api = SdfApi(client)
    righe = db.query("""SELECT d.revision_id, d.name, md.brand, md.family_id, md.model_id,
                               md.group_id, md.subgroup_id
                        FROM drawing d JOIN model_drawing md USING (revision_id)
                        WHERE d.image_name IS NULL
                        GROUP BY d.revision_id""")
    for r in righe:
        d = api.drawing(r["family_id"], r["model_id"], r["group_id"], r["subgroup_id"],
                        r["revision_id"], brand=r["brand"])
        meta = d["meta"]
        print(f"[{r['revision_id']}] {r['name']} ({r['brand']}): "
              f"ricevuta {d['got_revision']}, imageName={meta['image_name']!r}, "
              f"emapsUrl={'si' if d['emaps_url'] else 'no'}, ricambi={len(d['parts'])}")
        if meta["image_name"] and d["got_revision"] == r["revision_id"]:
            db._write([("UPDATE drawing SET image_name=?, preview_url=? WHERE revision_id=?",
                        (meta["image_name"], meta["preview_url"], r["revision_id"]), False)])
            print("    -> aggiornata")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scrivi", action="store_true", help="riempie gli URL ricavabili")
    ap.add_argument("--ricontrolla", action="store_true",
                    help="richiede al portale le tavole senza imageName (implica --scrivi)")
    a = ap.parse_args()
    db = Db()
    conta(db)
    if a.scrivi or a.ricontrolla:
        riempi(db)
    if a.ricontrolla:
        ricontrolla(db)
    if a.scrivi or a.ricontrolla:
        conta(db)


if __name__ == "__main__":
    main()
