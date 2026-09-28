"""
Estrazione del testo dei manuali operatore LANCER (LANCER/Manuali Operatore)
nella tabella `manuali_sezioni` di lancer.db, piu' un indice full-text
(`manuali_fts`, SQLite FTS5) per la ricerca per parole.

I manuali sono quasi tutti scansioni: per ogni pagina si usa il testo
nativo se c'e', altrimenti OCR Tesseract in italiano (tessdata in
dati/tessdata, come debug/estrai_pbt00135.py). Qui l'OCR e' accettabile
perche' e' testo discorsivo (istruzioni, sicurezza, manutenzione), non
codici ricambio: un carattere sbagliato non produce un codice sbagliato.

Il testo viene diviso in sezioni sui titoli numerati ("3.6 ALBERO
CARDANICO"); le sezioni lunghe sono spezzate in blocchi, cosi' ogni
riga e' un pezzo di testo che il bot puo' citare ("vedi manuale, pag. X").

Uso: ./venv/Scripts/python.exe lancer_db/estrai_manuali.py
Rilanciabile: le pagine OCR sono in cache in lancer_db/cache_ocr/.
"""
import os
import re
import sys

import pymupdf

from db import CARTELLA, PDF_MANUALI, connetti

RADICE = CARTELLA.parent
os.environ.setdefault("TESSDATA_PREFIX", str(RADICE / "dati" / "tessdata"))
import pytesseract  # noqa: E402
from PIL import Image  # noqa: E402

pytesseract.pytesseract.tesseract_cmd = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
sys.stdout.reconfigure(encoding="utf-8")

CACHE = CARTELLA / "cache_ocr"
MAX_CARATTERI = 2500

# "3.6 ALBERO CARDANICO", "1.0  IDENTIFICAZIONE" (manuale trinciatrici)
RE_TITOLO = re.compile(
    r"^\s*(\d{1,2}(?:\.\d{1,2})?)\s*[.)]?\s+([A-ZÀÈÉÌÒÙ][A-ZÀÈÉÌÒÙ'’ /\-]{3,}[A-ZÀÈÉÌÒÙ])\s*$")
# "PROFONDITÀ DI LAVORO", "SEGNALI DI PERICOLO" (manuale fresa: titoli senza numero)
RE_TITOLO_MAIUSC = re.compile(r"^\s*([A-ZÀÈÉÌÒÙ][A-ZÀÈÉÌÒÙ'’]+(?: [A-ZÀÈÉÌÒÙ'’/\-]+){1,9})\s*$")


def titolo_di(riga):
    m = RE_TITOLO.match(riga)
    if m:
        return m.group(1), m.group(2)
    m = RE_TITOLO_MAIUSC.match(riga)
    if m and 8 <= len(m.group(1)) <= 80 and "LANCER" not in m.group(1):
        parole = m.group(1).split()
        if sum(map(len, parole)) / len(parole) >= 3.5:     # scarta rumore OCR tipo "SE RA IIBT"
            return None, m.group(1)
    return None


def testo_pagina(doc, nome, i):
    nativo = doc[i].get_text().strip()
    if len(re.findall(r"[A-Za-zà-ù]{3,}", nativo)) > 40:
        return nativo, "nativo"
    cache = CACHE / f"{nome}_{i + 1:03d}.txt"
    if cache.exists():
        return cache.read_text(encoding="utf-8"), "ocr"
    pix = doc[i].get_pixmap(dpi=300)
    img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    t = pytesseract.image_to_string(img, lang="ita")
    CACHE.mkdir(exist_ok=True)
    cache.write_text(t, encoding="utf-8")
    return t, "ocr"


def pulisci(t):
    t = re.sub(r"(\w)-\n(\w)", r"\1\2", t)          # parole spezzate a capo
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()


def spezza(righe):
    """righe = [(pagina, testo)] di una sezione -> blocchi (testo, pag_da, pag_a)
    di al massimo MAX_CARATTERI, tagliando tra paragrafi (righe vuote)."""
    blocchi, cur, pagine = [], [], []
    for pag, riga in righe + [(None, "")]:
        fine_par = not riga.strip()
        if riga.strip():
            cur.append(riga)
            pagine.append(pag)
        if (fine_par and sum(len(x) for x in cur) > MAX_CARATTERI * 0.8) or pag is None:
            if cur:
                blocchi.append(("\n".join(cur).strip(), min(pagine), max(pagine)))
            cur, pagine = [], []
        elif fine_par and cur:
            cur.append("")
    return blocchi


def estrai(con, percorso):
    nome = percorso.stem
    doc = pymupdf.open(percorso)
    sezioni = []                       # [sezione, titolo, [(pagina, riga)]]
    cur = [None, "(inizio)", []]
    n_ocr = 0
    for i in range(len(doc)):
        t, fonte = testo_pagina(doc, nome, i)
        n_ocr += fonte == "ocr"
        for riga in pulisci(t).splitlines():
            tit = titolo_di(riga)
            if tit:
                if any(r.strip() for _, r in cur[2]):
                    sezioni.append(cur)
                cur = [tit[0], tit[1].strip().capitalize(), []]
            else:
                cur[2].append((i + 1, riga))
        print(f"  {nome} pag {i + 1}/{len(doc)} ({fonte})", end="\r", flush=True)
    if any(r.strip() for _, r in cur[2]):
        sezioni.append(cur)

    n = 0
    for sez, titolo, righe in sezioni:
        for blocco, da, a in spezza(righe):
            if len(blocco) < 40:
                continue
            con.execute("INSERT INTO manuali_sezioni(manuale, sezione, titolo, pagina_da, pagina_a, testo) "
                        "VALUES (?,?,?,?,?,?)", (nome, sez, titolo, da, a, blocco))
            n += 1
    print(f"{nome}: {len(doc)} pagine ({n_ocr} OCR), {len(sezioni)} sezioni, {n} blocchi" + " " * 20)


def main():
    con = connetti()
    con.execute("DELETE FROM manuali_sezioni")
    con.execute("DROP TABLE IF EXISTS manuali_fts")
    for p in sorted(PDF_MANUALI.glob("*.pdf")):
        estrai(con, p)
    # indice full-text (parole, con rimozione accenti)
    con.execute("CREATE VIRTUAL TABLE manuali_fts USING fts5("
                "titolo, testo, content='manuali_sezioni', content_rowid='id', "
                "tokenize='unicode61 remove_diacritics 2')")
    con.execute("INSERT INTO manuali_fts(rowid, titolo, testo) "
                "SELECT id, titolo, testo FROM manuali_sezioni")
    con.commit()
    for r in con.execute("SELECT manuale, sezione, titolo, pagina_da, pagina_a, length(testo) "
                         "FROM manuali_sezioni ORDER BY id"):
        print("  ", tuple(r))


if __name__ == "__main__":
    main()
