"""
Ricerca ricambi SDF: solo la parte specifica di SDF (come leggere il
catalogo di un modello da sdf.db). Il motore (punteggio, sinonimi, misure,
codici) e il vocabolario sono comuni a tutte le marche: vedi
fornitori/ricerca.py e fornitori/arricchimento.py.

Prova da riga di comando:
  ./venv/Scripts/python.exe -m fornitori.sdf.ricerca LAMBORGHINI 11572 "paraolio albero frizione"
  ./venv/Scripts/python.exe -m fornitori.sdf.ricerca --senza-regola
"""
import sys

from .. import ricerca as motore
from ..ricerca import _VUOTE  # noqa: F401  (usato da debug/valuta_ricerca_sdf.py)

# SDF non ha regole proprie: oggi tutto il vocabolario e' quello comune.
# Se servissero regole solo-SDF: Vocabolario(regole=[...], contesti=[...]).
VOCABOLARIO = motore.COMUNE

# Catalogo di un modello nel formato standard del motore (vedi
# fornitori/ricerca.py); price/sellable/replaced/abolished/preview_url
# restano con il nome SDF: li usa solo FornitoreSdf._articolo.
SQL_MODELLO = """
    SELECT p.code AS codice, p.description AS descrizione,
           p.position AS posizione, p.quantity AS quantita,
           p.price, p.sellable, p.replaced, p.abolished,
           d.revision_id AS tavola, d.name AS nome_tavola,
           d.notes AS nota_tavola, d.tractor_sn_range AS validita,
           d.preview_url,
           g.name AS gruppo, s.name AS sottogruppo
    FROM part p
    JOIN drawing d        ON d.revision_id = p.revision_id
    JOIN model_drawing md ON md.revision_id = p.revision_id
    LEFT JOIN grp g       ON g.row_id = md.group_id
    LEFT JOIN subgroup s  ON s.row_id = md.subgroup_id
    WHERE md.brand = ? AND md.model_id = ?
    ORDER BY d.revision_id, p.position
"""


def cerca(db, brand_code, model_id, testo, limit=60):
    """Righe del modello ordinate per pertinenza alla richiesta (formato
    standard del motore, + 'rilevanza', 'pertinenza_tavola', 'trovato_come').
    Si chiama solo a catalogo completo (cerca_ricambio risponde
    "in_preparazione" prima), quindi la cache del catalogo preparato e'
    sicura."""
    preparate = motore.CATALOGHI.prepara(
        ("sdf", brand_code, model_id),
        lambda: db.query(SQL_MODELLO, (brand_code, model_id)),
        VOCABOLARIO)
    return motore.cerca(preparate, testo, VOCABOLARIO, limit=limit)


def tipi_senza_regola(db):
    """Tipi del catalogo SDF in cache che nessuna regola copre, dal piu' frequente."""
    return VOCABOLARIO.tipi_senza_regola(
        d for (d,) in db.query("SELECT description FROM part"))


if __name__ == "__main__":
    from .client import BRANDS
    from .db import Db

    sys.stdout.reconfigure(encoding="utf-8")
    db = Db()
    if sys.argv[1:2] == ["--senza-regola"]:
        for t, n in tipi_senza_regola(db):
            print(f"{n:5d}  {t}")
    else:
        brand, model_id, testo = sys.argv[1], int(sys.argv[2]), " ".join(sys.argv[3:])
        for r in cerca(db, BRANDS[brand.upper()], model_id, testo, limit=15):
            print(f"{r['rilevanza']:5.1f}  {r['codice']:18} {r['descrizione'][:40]:40} "
                  f"{(r['trovato_come'] or '')[:25]:25} | {r['sottogruppo']} / {r['nome_tavola']}")
