"""
Scopre coppie di nomi che il catalogo SDF usa come alternativi per la
stessa posizione dello stesso disegno (es. "guarnizione" / "anello
tenuta" in pos. 3 e 7 della stessa tavola) - candidati per una tabella
di "famiglie di sinonimi" da far rivedere all'azienda.

Solo esplorazione offline: query dirette su sdf.db, nessun collegamento
con agente.py/fornitori (per questo vive in debug/, non rompe
l'astrazione dalla marca).
"""
import csv
import re
import sqlite3
from collections import Counter, defaultdict
from itertools import combinations

from fornitori.sdf.db import DEFAULT_PATH

# pezzi speculari dx/sx: descrizioni diverse ma NON un caso di nome
# improprio, sono fisicamente due pezzi diversi -> vanno esclusi, non
# uniti in una famiglia.
LATO_RE = re.compile(r"dx/rh/re|sx/lh/li", re.IGNORECASE)

# punto in cui si taglia la descrizione per arrivare al "nome base":
# il primo digit, la parola STANDARD, o una virgoletta/Ø (misure,
# varianti +/- mm, taglie).
TAGLIO_RE = re.compile(r"[0-9\"Ø]|standard", re.IGNORECASE)


def normalizza_parentesi(desc):
    """Toglie le annotazioni di revisione tipo '( -> 20001 )' / '( 76 <- )'."""
    return re.sub(r"\(.*?\)", "", desc)


def nome_base(desc):
    """'guarnizione 215.6x222.4x3.4' -> 'guarnizione'
    'semibronzina banco + mm 0.25' -> 'semibronzina banco'
    'semibronzina banco STANDARD' -> 'semibronzina banco'
    """
    d = desc.strip().lower()
    m = TAGLIO_RE.search(d)
    if m:
        d = d[:m.start()]
    d = re.sub(r"[\s+\-/]+$", "", d)
    d = re.sub(r"\bmm\b\s*$", "", d)
    d = re.sub(r"[\s+\-/]+$", "", d)
    return d.strip()


def estrai_gruppi(conn):
    """{(revision_id, position): [(code, description_normalizzata), ...]}
    scartando 'vecchio codice' e le varianti dx/sx."""
    parts = conn.execute(
        "SELECT revision_id, position, code, description FROM part"
    ).fetchall()

    gruppi = defaultdict(list)
    for revision_id, position, code, description in parts:
        if description.lower().strip() == "vecchio codice":
            continue
        if LATO_RE.search(description):
            continue
        pulita = normalizza_parentesi(description).strip()
        gruppi[(revision_id, position)].append((code, pulita))
    return gruppi


def gruppi_con_nomi_diversi(gruppi):
    """Tiene solo i gruppi dove restano >= 2 NOMI BASE diversi dopo la
    normalizzazione delle misure (in fase precedente scartavamo solo
    duplicati esatti, qui uniformiamo 'guarnizione 215...' e
    'guarnizione 14...' allo stesso nome base)."""
    multipli = {}
    for chiave, valori in gruppi.items():
        basi = {nome_base(descrizione) for _, descrizione in valori}
        basi.discard("")
        if len(basi) > 1:
            multipli[chiave] = valori
    return multipli


def conta_coppie(multipli):
    conteggio = Counter()
    esempi = {}
    for chiave, valori in multipli.items():
        basi = sorted({nome_base(d) for _, d in valori if nome_base(d)})
        for a, b in combinations(basi, 2):
            conteggio[(a, b)] += 1
            esempi.setdefault((a, b), chiave)
    return conteggio, esempi


def esporta_csv(conteggio, esempi, percorso, minimo=2):
    righe = [
        (a, b, n, esempi[(a, b)])
        for (a, b), n in conteggio.items()
        if n >= minimo
    ]
    righe.sort(key=lambda r: -r[2])

    with open(percorso, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["termine_1", "termine_2", "occorrenze", "esempio_revision_id_position"])
        for a, b, n, esempio in righe:
            w.writerow([a, b, n, f"{esempio[0]}/{esempio[1]}"])
    return len(righe)


if __name__ == "__main__":
    conn = sqlite3.connect(database=DEFAULT_PATH)

    gruppi = estrai_gruppi(conn)
    print("gruppi totali (revision_id, position):", len(gruppi))

    multipli = gruppi_con_nomi_diversi(gruppi)
    print("gruppi con nomi base diversi:", len(multipli))

    conteggio, esempi = conta_coppie(multipli)
    print("coppie distinte trovate:", len(conteggio))

    percorso_csv = "debug/famiglie_sinonimi_candidate.csv"
    n = esporta_csv(conteggio, esempi, percorso_csv, minimo=1)
    print(f"esportate {n} coppie (occorrenze >= 1) in {percorso_csv}")

    print("\nTop 20 per frequenza:")
    for (a, b), n in conteggio.most_common(20):
        print(f"  {n:4d}  {a!r} <-> {b!r}")
