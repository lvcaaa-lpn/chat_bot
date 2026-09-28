"""
Schema e connessione del DB dedicato LANCER (lancer_db/lancer.db).

Tabelle:
  serie            un PDF di esplosi = una serie (HD, JM, PM...)
  tavole           una tabella ricambi del PDF (titolo + pagina tabella/disegno)
  righe            una riga della tabella: posizione, codice, descrizione, qta
  righe_modelli    per quali modelli vale la riga, e con che quantita'
                   (solo dove il PDF lo dice esplicitamente)
  ricambi          un record per codice univoco: descrizione originale +
                   arricchimento (italiano, categoria, sinonimi) -> arricchisci.py
  manuali_sezioni  testo OCR dei manuali operatore, per sezione -> estrai_manuali.py
  embeddings       vettori per la ricerca semantica -> embeddings.py
"""
import sqlite3
from pathlib import Path

CARTELLA = Path(__file__).resolve().parent
DB = CARTELLA / "lancer.db"
PDF_ESPLOSI = CARTELLA.parent / "LANCER" / "Esplosi ricambi"
PDF_MANUALI = CARTELLA.parent / "LANCER" / "Manuali Operatore"

# tipo di attrezzo per serie (dai titoli dei PDF: Rotary Tiller, Mulcher,
# Grooming Mower, Power Harrow, disc harrow; "Kenchua" = coltivatore a denti)
MACCHINE = {
    "HD": "fresa", "HP": "fresa", "HS": "fresa", "MASTER": "fresa",
    "MP": "fresa", "MAXIMO": "fresa", "MAXIMO BOLD": "fresa",
    "JH": "trinciatrice", "JL": "trinciatrice", "JM": "trinciatrice",
    "GM": "trinciaerba / rasaerba",
    "KH": "coltivatore a denti (estirpatore)", "KM": "coltivatore a denti (estirpatore)",
    "PL": "erpice a dischi", "PM": "erpice a dischi", "PMF": "erpice a dischi pieghevole",
    "UM": "erpice rotante",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS serie (
    id INTEGER PRIMARY KEY,
    sigla TEXT NOT NULL,            -- HD, JM, PM... (dal nome file)
    macchina TEXT,                  -- tipo di attrezzo in italiano (vedi MACCHINE)
    nome TEXT,                      -- titolo dalla copertina
    file_pdf TEXT NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS tavole (
    id INTEGER PRIMARY KEY,
    serie_id INTEGER NOT NULL REFERENCES serie(id),
    titolo TEXT,
    pagina_tabella INTEGER NOT NULL,   -- 1-based
    pagina_disegno INTEGER             -- 1-based, pagina con l'esploso (se c'e')
);
CREATE TABLE IF NOT EXISTS righe (
    id INTEGER PRIMARY KEY,
    tavola_id INTEGER NOT NULL REFERENCES tavole(id),
    posizione TEXT,                 -- numero sul disegno (Item No)
    codice TEXT NOT NULL,
    descrizione TEXT,               -- come nel PDF (inglese)
    qta TEXT,                       -- quantita' come scritta (puo' essere "(10-12-14-16)")
    note TEXT,
    ordine INTEGER
);
CREATE TABLE IF NOT EXISTS righe_modelli (
    riga_id INTEGER NOT NULL REFERENCES righe(id),
    modello TEXT NOT NULL,
    qta TEXT,
    fonte TEXT                      -- 'tabella' (colonne/elenco del PDF) o 'descrizione'
);
CREATE TABLE IF NOT EXISTS ricambi (
    codice TEXT PRIMARY KEY,
    descrizione_en TEXT,
    tipo_en TEXT,                   -- descrizione senza misure/modelli ("hex bolt")
    descrizione_it TEXT,
    categoria TEXT,
    sinonimi TEXT                   -- separati da ';' (italiano colloquiale + siciliano)
);
CREATE TABLE IF NOT EXISTS tipi (
    tipo_en TEXT PRIMARY KEY,       -- descrizione senza misure/modelli (vedi arricchisci.py)
    concetto TEXT,                  -- chiave in arricchimento.CONCETTI
    esempio TEXT,
    nome_it TEXT,
    categoria TEXT,
    funzione TEXT,
    sinonimi TEXT,
    siciliano TEXT
);
CREATE TABLE IF NOT EXISTS manuali_sezioni (
    id INTEGER PRIMARY KEY,
    manuale TEXT NOT NULL,
    sezione TEXT,
    titolo TEXT,
    pagina_da INTEGER,
    pagina_a INTEGER,
    testo TEXT
);
CREATE TABLE IF NOT EXISTS embeddings (
    chiave TEXT PRIMARY KEY,        -- 'tipo:<tipo_en>' o 'manuale:<id>'
    testo TEXT NOT NULL,            -- testo che e' stato vettorizzato
    modello TEXT NOT NULL,
    vettore BLOB NOT NULL           -- float32
);
CREATE INDEX IF NOT EXISTS i_righe_codice ON righe(codice);
CREATE INDEX IF NOT EXISTS i_righe_tavola ON righe(tavola_id);
CREATE INDEX IF NOT EXISTS i_rm_riga ON righe_modelli(riga_id);
CREATE INDEX IF NOT EXISTS i_ricambi_tipo ON ricambi(tipo_en);
"""


def connetti():
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode = WAL")
    con.executescript(SCHEMA)
    return con
