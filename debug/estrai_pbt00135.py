"""
Estrazione una tantum dei dati ricambi dal catalogo Goldoni PBT00135
(Autocarro 3500 RTS) tramite OCR (Tesseract) con ricostruzione delle
colonne per posizione (non semplice OCR a blocco di testo).

Il PDF e' uno scan (nessun testo estraibile nativamente), ma le pagine
tabella sono a stampa tipografica pulita, quindi l'OCR e' affidabile su
codice/descrizione; il numero di posizione resta il dato piu' debole
(spesso assente o sostituito da un simbolo di "idem" nel documento
originale) e va preso con piu' cautela.

Output: dati/estrazioni_pdf/pbt00135.json (NON caricato in goldoni.db,
solo per revisione umana).
"""
import fitz
import json
import os
import re
import sqlite3
from datetime import datetime, timezone

os.environ["TESSDATA_PREFIX"] = r"C:\Users\Luca\Ricambi\dati\tessdata"
import pytesseract  # noqa: E402
from PIL import Image  # noqa: E402

pytesseract.pytesseract.tesseract_cmd = r"C:\Program Files\Tesseract-OCR\tesseract.exe"

PDF = r"C:\Users\Luca\Ricambi\pbt00135.pdf"
DB = r"C:\Users\Luca\Ricambi\dati\goldoni.db"
OUT = r"C:\Users\Luca\Ricambi\dati\estrazioni_pdf\pbt00135.json"

# pagina PDF (1-based) di ciascuna tabella dati, gia' identificata:
# una tabella per tavola, tavole 1-12 -> pagine 9,11,13,...,31
PAGINE_TABELLA = {t: 9 + (t - 1) * 2 for t in range(1, 13)}

# titoli presi dall'indice del catalogo (pagina 8), non dall'OCR:
TITOLI = {
    1: "Cabina di guida",
    2: "Cassone",
    3: "Carter cambio e coperchio - Frizione - Presa di forza anteriore - Cambio - Differenziale anteriore",
    4: "Mozzi ruote anteriori - Sospensioni e ammortizzatori - Freni anteriori",
    5: "Mozzi ruote posteriori - Ammortizzatori - Freni posteriori - Giunti di trasmissione - Ruote",
    6: "Telaio - Cofano motore - Sedili",
    7: "Sterzo idraulico - Tubi di mandata e di aspirazione per impianto di sollevamento - Cilindro",
    8: "Comandi acceleratore - Differenziale - Frizione - Presa di forza e arresto motore",
    9: "Comando freni meccanico e idraulico",
    10: "Carter differenziale - Presa di forza posteriore - Differenziale posteriore",
    11: "Pompa freni - Distributore e pompa sollevatore",
    12: "Impianto elettrico",
}

HALF_BOUNDARY = 1250  # px a 300dpi: separa colonna sinistra/destra della pagina
ROW_TOL_DENOM = 20  # tolleranza (px) per abbinare parole descrizione alla riga
ROW_TOL_POS = 15  # tolleranza (px) per abbinare il numero di posizione alla riga

CODICE_RE = re.compile(r"^[0-9]{6,8}$")
NON_FORNITO_RE = re.compile(r"^-{4,}$")


def carica_codici_noti(path_db):
    con = sqlite3.connect(path_db)
    cur = con.cursor()
    cur.execute("SELECT DISTINCT codice FROM righe")
    codici = {r[0] for r in cur.fetchall()}
    con.close()
    return codici


def vicino_a_codice_noto(codice, codici_noti):
    if codice in codici_noti:
        return None
    for i in range(len(codice)):
        for d in "0123456789":
            if d == codice[i]:
                continue
            variante = codice[:i] + d + codice[i + 1 :]
            if variante in codici_noti:
                return variante
    return None


def normalizza_diametro(testo):
    # OCR legge spesso il simbolo di diametro "O" come "Q" o "@" isolato
    return re.sub(r"(?<!\S)[Q@](?!\S)", "\u00d8", testo)


def estrai_parole(img):
    data = pytesseract.image_to_data(img, lang="ita", output_type=pytesseract.Output.DICT)
    parole = []
    n = len(data["text"])
    for i in range(n):
        t = data["text"][i].strip()
        if not t:
            continue
        parole.append(
            {
                "text": t,
                "left": data["left"][i],
                "top": data["top"][i],
                "width": data["width"][i],
                "height": data["height"][i],
                "conf": float(data["conf"][i]),
            }
        )
    return parole


def costruisci_righe_meta(parole_meta):
    """parole_meta: lista di dict con text/left/top/width/conf, un solo lato pagina."""
    matricola_words = [
        w for w in parole_meta if CODICE_RE.match(w["text"]) or NON_FORNITO_RE.match(w["text"])
    ]
    if not matricola_words:
        return []

    mat_x_min = min(w["left"] for w in matricola_words)
    mat_x_max = max(w["left"] + w["width"] for w in matricola_words)

    pos_words = [w for w in parole_meta if w["left"] < mat_x_min - 15 and len(w["text"]) <= 3]
    denom_words = [w for w in parole_meta if w["left"] > mat_x_max + 15]

    righe = []
    for mw in sorted(matricola_words, key=lambda w: w["top"]):
        y = mw["top"]

        # parole descrizione piu' vicine a questa riga (per top)
        vicine_grezze = [w for w in denom_words if abs(w["top"] - y) <= ROW_TOL_DENOM]
        # scarta token di 1-2 caratteri a bassissima confidenza: quasi sempre
        # rumore OCR (es. simbolo di diametro "Ø" letto come "�"/em-dash),
        # non parole vere della descrizione
        scartate = [w for w in vicine_grezze if w["conf"] < 30 and len(w["text"]) <= 2]
        vicine = [w for w in vicine_grezze if w not in scartate]
        # ordinare per posizione orizzontale, non per 'top' della singola parola:
        # il top OCR di ogni parola oscilla di qualche pixel (ascendenti/discendenti
        # delle lettere) anche quando le parole sono sulla stessa riga stampata,
        # e ordinare per top scrambla l'ordine delle parole nella descrizione.
        vicine.sort(key=lambda w: w["left"])
        descrizione = normalizza_diametro(" ".join(w["text"] for w in vicine))
        conf_media = (
            sum(w["conf"] for w in vicine + [mw]) / (len(vicine) + 1) if vicine else mw["conf"]
        )
        if scartate:
            descrizione = (descrizione + " [nota: scartato/i token illeggibili]").strip()

        # numero di posizione piu' vicino (puo' mancare: idem/vuoto nel documento)
        pos_candidati = [w for w in pos_words if abs(w["top"] - y) <= ROW_TOL_POS]
        posizione = pos_candidati[0]["text"] if pos_candidati else None

        # riquadro che copre posizione+codice+descrizione di questa riga sulla
        # pagina scansionata (px alla stessa DPI usata per l'OCR), per poter
        # ritagliare l'immagine della riga nello strumento di revisione
        parole_riga = vicine + [mw] + pos_candidati
        bbox = {
            "left": min(w["left"] for w in parole_riga),
            "top": min(w["top"] for w in parole_riga),
            "right": max(w["left"] + w["width"] for w in parole_riga),
            "bottom": max(w["top"] + w["height"] for w in parole_riga),
        }

        righe.append(
            {
                "posizione": posizione,
                "codice": None if NON_FORNITO_RE.match(mw["text"]) else mw["text"],
                "descrizione": descrizione if descrizione else None,
                "_conf_media": round(conf_media, 1),
                "_non_fornito_singolarmente": bool(NON_FORNITO_RE.match(mw["text"])),
                "_bbox": bbox,
            }
        )
    return righe


def confidenza_riga(riga, codici_noti, pagina_pdf, dpi):
    note = []
    if riga["posizione"] is None:
        note.append("posizione non individuata dall'OCR (probabile 'idem' o numero non rilevato)")
    if riga["_conf_media"] < 60:
        note.append(f"bassa confidenza OCR (media {riga['_conf_media']})")
    if not riga["descrizione"]:
        note.append("descrizione non trovata vicino a questa riga")

    if riga["_non_fornito_singolarmente"]:
        stato_codice = "non_fornito_singolarmente"
    elif riga["codice"] is None:
        stato_codice = "codice_assente"
    elif riga["codice"] in codici_noti:
        stato_codice = "nota"
    else:
        vicino = vicino_a_codice_noto(riga["codice"], codici_noti)
        stato_codice = f"vicino_a_codice_noto:{vicino}" if vicino else "nuovo_da_verificare"

    riga_out = {
        "posizione": riga["posizione"],
        "codice": riga["codice"],
        "descrizione": riga["descrizione"],
        "confidenza": stato_codice + (("; " + "; ".join(note)) if note else ""),
        "revisione": {
            "pagina_pdf": pagina_pdf,
            "dpi": dpi,
            "bbox": riga["_bbox"],
        },
    }
    return riga_out


def main():
    codici_noti = carica_codici_noti(DB)
    doc = fitz.open(PDF)
    tavole_out = []

    for tav in range(1, 13):
        pagina = PAGINE_TABELLA[tav]
        pix = doc[pagina - 1].get_pixmap(dpi=300)
        img = Image.frombytes(
            "RGB" if pix.n >= 3 else "L", [pix.width, pix.height], pix.samples
        )
        parole = estrai_parole(img)

        sinistra = [w for w in parole if w["left"] < HALF_BOUNDARY]
        destra = [w for w in parole if w["left"] >= HALF_BOUNDARY]

        righe_meta = costruisci_righe_meta(sinistra) + costruisci_righe_meta(destra)
        righe = [confidenza_riga(r, codici_noti, pagina, 300) for r in righe_meta]

        tavole_out.append(
            {
                "pagina_pdf": pagina,
                "tavola": tav,
                "titolo": TITOLI[tav],
                "righe": righe,
            }
        )
        print(f"Tav. {tav} (pagina {pagina}): {len(righe)} righe estratte")

    risultato = {
        "catalogo": "PBT00135",
        "modello": "AUTOCARRO 3500 RTS",
        "fonte": "pdf",
        "file_origine": "pbt00135.pdf",
        "estratto_il": datetime.now(timezone.utc).isoformat(),
        "metodo": "OCR (Tesseract, lang=ita) con ricostruzione colonne per coordinate, "
        "non lettura visiva manuale",
        "tavole": tavole_out,
    }

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(risultato, f, ensure_ascii=False, indent=2)

    totale_righe = sum(len(t["righe"]) for t in tavole_out)
    print(f"\nFatto: {len(tavole_out)} tavole, {totale_righe} righe -> {OUT}")


if __name__ == "__main__":
    main()
