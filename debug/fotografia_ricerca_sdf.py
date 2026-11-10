"""
Fotografia COMPLETA dei risultati della ricerca ricambi SDF, per verificare
che un refactoring non cambi nulla.

A differenza di valuta_ricerca_sdf.py (che conta solo se il pezzo atteso e'
nel primo blocco), qui si salva l'intera risposta di cerca_ricambio
(blocchi, ordine, articoli, correlati, note) per ogni frase di test su
OGNI modello in cache, anche dove il pezzo atteso non c'e'. Due fotografie
uguali = la ricerca si comporta in modo identico.

Uso (dalla radice del progetto):
  ./venv/Scripts/python.exe debug/fotografia_ricerca_sdf.py --salva dati/foto_prima.json
  ./venv/Scripts/python.exe debug/fotografia_ricerca_sdf.py --confronta dati/foto_prima.json

Non tocca la rete: solo il DB locale fornitori/sdf/sdf.db.
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8")

from debug.valuta_ricerca_sdf import CASI, fornitore, modelli_in_cache  # noqa: E402
from fornitori.sdf.db import Db  # noqa: E402

# frasi in piu' rispetto ai CASI: codici, misure, richieste vaghe o senza
# risultato, per coprire anche i rami della ricerca che i CASI non toccano
EXTRA = [
    "04411512.4", "2.1529.125.0", "paraolio 25 32 7", "filtro", "filtro olio motore",
    "vite", "il coso", "xyzzy", "pompa acqua", "cuscinetto ruota anteriore",
    "freni posteriori", "kit guarnizioni", "p.t.o.", "s.a.c. sollevatore",
]


def fotografia():
    db = Db()
    frasi = list(dict.fromkeys([c[0] for c in CASI] + EXTRA))
    out = {}
    for label, mid, nome in modelli_in_cache(db):
        f = fornitore(db, label, mid, nome, "arricchita")
        for frase in frasi:
            out[f"{nome} || {frase}"] = f.cerca_ricambio(frase)
    return out


def main():
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--salva")
    g.add_argument("--confronta")
    a = ap.parse_args()

    foto = fotografia()
    if a.salva:
        Path(a.salva).write_text(json.dumps(foto, ensure_ascii=False, indent=1,
                                            sort_keys=True), encoding="utf-8")
        print(f"{len(foto)} ricerche salvate in {a.salva}")
        return

    prima = json.loads(Path(a.confronta).read_text(encoding="utf-8"))
    # passaggio da JSON per confrontare gli stessi tipi (tuple -> liste ecc.)
    dopo = json.loads(json.dumps(foto, ensure_ascii=False, sort_keys=True))
    diverse = [k for k in sorted(set(prima) | set(dopo)) if prima.get(k) != dopo.get(k)]
    print(f"ricerche confrontate: {len(set(prima) | set(dopo))}, diverse: {len(diverse)}")
    for k in diverse[:20]:
        print("  DIVERSA:", k)
    sys.exit(1 if diverse else 0)


if __name__ == "__main__":
    main()
