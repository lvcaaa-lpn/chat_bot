"""
Archivio locale del catalogo Maschio Gaspardo (SQLite), riempito da
scarica_tutto.py.

Struttura:

    linee/categorie/modelli --< release_modello >-- release --< tavole --< posizioni
                                                                               |
                                                     ricambi (anagrafica) >----+  (per codice)

  - release: un'edizione di un catalogo ricambi (codice documento + data,
    es. B0D5P0093 2011-02 / 2015-05 / 2016-01). Una release puo' valere
    per piu' modelli, da qui release_modello. Le release che non stanno
    sotto nessun modello (es. libretti) restano senza legame.
  - tavole: il disegno esploso; url_disegno e' l'SVG del portale, che
    richiede login (401 senza): si salva solo l'indirizzo.
  - posizioni: numero sul disegno -> codice. Il portale non da' quantita'
    ne' descrizione per posizione: la descrizione sta in ricambi.
  - ricambi: anagrafica completa (Archivi -> Ricerca da tabella), con
    sostituto, peso e URL della foto (pubblica, si apre senza login).
"""

import os
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_PATH = str(Path(__file__).resolve().parent / "maschio.db")

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS linee (
  id     TEXT PRIMARY KEY,          -- FIE, GAS, MAS, UNG
  nome   TEXT
);

CREATE TABLE IF NOT EXISTS categorie (
  linea  TEXT NOT NULL,
  id     TEXT NOT NULL,
  nome   TEXT,
  PRIMARY KEY (linea, id)
);

CREATE TABLE IF NOT EXISTS modelli (
  linea     TEXT NOT NULL,
  categoria TEXT NOT NULL,
  id        TEXT NOT NULL,          -- C2B
  nome      TEXT,                   -- ENTRY
  PRIMARY KEY (linea, categoria, id)
);

CREATE TABLE IF NOT EXISTS release (
  id             INTEGER PRIMARY KEY,  -- releaseId del portale
  descrizione    TEXT,                 -- Cat. ricambi ENTRY 120
  codice_doc     TEXT,                 -- B0D5P0093
  data           TEXT,                 -- 2011-02
  cartella       TEXT,                 -- B0D5P0093-2011-02 (per i disegni)
  tavole_lette_il TEXT                 -- NULL = elenco tavole da leggere
);
CREATE INDEX IF NOT EXISTS ix_release_doc ON release(codice_doc);

CREATE TABLE IF NOT EXISTS release_modello (
  release   INTEGER NOT NULL,
  linea     TEXT NOT NULL,
  categoria TEXT NOT NULL,
  modello   TEXT NOT NULL,
  PRIMARY KEY (release, linea, categoria, modello)
);
CREATE INDEX IF NOT EXISTS ix_rm_modello ON release_modello(linea, categoria, modello);

CREATE TABLE IF NOT EXISTS tavole (
  id           INTEGER PRIMARY KEY,   -- tableId del portale
  release      INTEGER NOT NULL REFERENCES release(id) ON DELETE CASCADE,
  numero       TEXT,                  -- 009
  titolo       TEXT,                  -- Telaio inferiore + timone ...
  ordine       INTEGER,
  url_disegno  TEXT,                  -- SVG, richiede login
  n_posizioni  INTEGER,
  scaricata_il TEXT                   -- NULL = posizioni da scaricare
);
CREATE INDEX IF NOT EXISTS ix_tavole_release ON tavole(release);

CREATE TABLE IF NOT EXISTS posizioni (
  tavola     INTEGER NOT NULL REFERENCES tavole(id) ON DELETE CASCADE,
  riga       INTEGER NOT NULL,        -- rowID del portale
  etichetta  TEXT,                    -- numero mostrato sul disegno
  codice     TEXT,                    -- NULL = posizione senza codice
  PRIMARY KEY (tavola, riga)
);
CREATE INDEX IF NOT EXISTS ix_posizioni_codice ON posizioni(codice);

CREATE TABLE IF NOT EXISTS ricambi (
  codice         TEXT PRIMARY KEY,
  azienda        TEXT,                -- FE, MA, UG ... (sigla del portale)
  descrizione    TEXT,                -- italiano; il '>' finale e' del portale
  sostituito_da  TEXT,
  dismesso       INTEGER,
  qta_minima     REAL,
  peso           REAL,
  url_foto       TEXT,                -- pubblica; NULL = nessuna foto
  aggiornato_il  TEXT
);
CREATE INDEX IF NOT EXISTS ix_ricambi_sost ON ricambi(sostituito_da);

CREATE TABLE IF NOT EXISTS stato_download (
  chiave   TEXT PRIMARY KEY,
  fatto_il TEXT
);
"""


def _adesso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Db:
    """Connessione condivisibile tra thread (come carraro/db.py)."""

    def __init__(self, path=DEFAULT_PATH):
        self.path = path
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self._lk = threading.RLock()
        self.con = sqlite3.connect(path, check_same_thread=False, timeout=60)
        self.con.row_factory = sqlite3.Row
        with self._lk:
            self.con.execute("PRAGMA busy_timeout=60000")
            self.con.execute("PRAGMA foreign_keys=ON")
            self.con.executescript(SCHEMA)
            self.con.commit()

    def close(self):
        with self._lk:
            self.con.close()

    # ---------------------------------------------------------- primitive
    def query(self, sql, args=()):
        with self._lk:
            return self.con.execute(sql, args).fetchall()

    def one(self, sql, args=()):
        with self._lk:
            return self.con.execute(sql, args).fetchone()

    def _write(self, statements):
        """Piu' istruzioni in UNA transazione: o tutte o nessuna."""
        with self._lk:
            try:
                for sql, args, many in statements:
                    if many:
                        self.con.executemany(sql, args)
                    else:
                        self.con.execute(sql, args)
                self.con.commit()
            except Exception:
                self.con.rollback()
                raise

    # ------------------------------------------------------- avanzamento
    def fatto(self, chiave):
        return self.one("SELECT 1 FROM stato_download WHERE chiave=?",
                        (chiave,)) is not None

    def segna_fatto(self, chiave):
        self._write([("INSERT OR REPLACE INTO stato_download VALUES (?,?)",
                      (chiave, _adesso()), False)])

    # ---------------------------------------------------------- elenco
    def salva_linee(self, linee):
        self._write([("INSERT OR REPLACE INTO linee VALUES (?,?)",
                      [(l["id"], l["nome"]) for l in linee], True)])

    def salva_categorie(self, linea, categorie):
        self._write([("INSERT OR REPLACE INTO categorie VALUES (?,?,?)",
                      [(linea, c["id"], c["nome"]) for c in categorie], True)])

    def salva_modelli(self, linea, categoria, modelli):
        self._write([("INSERT OR REPLACE INTO modelli VALUES (?,?,?,?)",
                      [(linea, categoria, m["id"], m["nome"]) for m in modelli],
                      True)])

    def salva_release(self, releases):
        """Nuove release in coda; quelle gia' note aggiornano solo i dati
        descrittivi (non si perde lo stato delle tavole)."""
        self._write([("""
            INSERT INTO release (id, descrizione, codice_doc, data)
            VALUES (?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET descrizione=excluded.descrizione,
              codice_doc=excluded.codice_doc, data=excluded.data
        """, [(r["id"], r["descrizione"], r["codice_doc"], r["data"])
              for r in releases], True)])

    def lega_release_modello(self, linea, categoria, modello, releases):
        self.salva_release(releases)
        self._write([
            ("DELETE FROM release_modello WHERE linea=? AND categoria=? AND modello=?",
             (linea, categoria, modello), False),
            ("INSERT OR IGNORE INTO release_modello VALUES (?,?,?,?)",
             [(r["id"], linea, categoria, modello) for r in releases], True),
        ])

    # ---------------------------------------------------------- tavole
    def release_da_leggere(self, limite=None):
        sql = "SELECT * FROM release WHERE tavole_lette_il IS NULL ORDER BY id"
        if limite:
            sql += f" LIMIT {int(limite)}"
        return self.query(sql)

    def salva_tavole_release(self, release_id, cartella, tavole, url_disegno):
        """url_disegno: funzione (cartella, table_id) -> URL dell'SVG.
        Le tavole gia' scaricate non vengono toccate."""
        self._write([
            ("""INSERT INTO tavole (id, release, numero, titolo, ordine, url_disegno)
                VALUES (?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET release=excluded.release,
                  numero=excluded.numero, titolo=excluded.titolo,
                  ordine=excluded.ordine, url_disegno=excluded.url_disegno""",
             [(t["id"], release_id, t["numero"], t["titolo"], t["ordine"],
               url_disegno(cartella, t["id"])) for t in tavole], True),
            ("UPDATE release SET cartella=?, tavole_lette_il=? WHERE id=?",
             (cartella, _adesso(), release_id), False),
        ])

    def tavole_da_scaricare(self, limite=None):
        sql = """SELECT t.*, r.cartella FROM tavole t JOIN release r ON r.id=t.release
                 WHERE t.scaricata_il IS NULL ORDER BY t.release, t.ordine"""
        if limite:
            sql += f" LIMIT {int(limite)}"
        return self.query(sql)

    def salva_posizioni(self, tavola_id, posizioni):
        """posizioni: [(riga, etichetta, codice)]. Sostituite in blocco e
        tavola segnata come scaricata nella stessa transazione."""
        self._write([
            ("DELETE FROM posizioni WHERE tavola=?", (tavola_id,), False),
            ("INSERT INTO posizioni VALUES (?,?,?,?)",
             [(tavola_id, r, e, c) for r, e, c in posizioni], True),
            ("UPDATE tavole SET scaricata_il=?, n_posizioni=? WHERE id=?",
             (_adesso(), len(posizioni), tavola_id), False),
        ])

    # ---------------------------------------------------------- anagrafica
    def salva_ricambi(self, ricambi):
        ora = _adesso()
        self._write([("""
            INSERT OR REPLACE INTO ricambi
              (codice, azienda, descrizione, sostituito_da, dismesso,
               qta_minima, peso, url_foto, aggiornato_il)
            VALUES (?,?,?,?,?,?,?,?,?)""",
            [(r["codice"], r["azienda"], r["descrizione"], r["sostituito_da"],
              r["dismesso"], r["qta_minima"], r["peso"], r["url_foto"], ora)
             for r in ricambi], True)])

    # ---------------------------------------------------------- stato
    def statistiche(self):
        n = lambda sql: self.one(sql)[0]
        return {
            "linee": n("SELECT COUNT(*) FROM linee"),
            "modelli": n("SELECT COUNT(*) FROM modelli"),
            "release": n("SELECT COUNT(*) FROM release"),
            "release_lette": n("SELECT COUNT(*) FROM release WHERE tavole_lette_il IS NOT NULL"),
            "release_senza_modello": n("""SELECT COUNT(*) FROM release r WHERE NOT EXISTS
                                          (SELECT 1 FROM release_modello m WHERE m.release=r.id)"""),
            "tavole": n("SELECT COUNT(*) FROM tavole"),
            "tavole_scaricate": n("SELECT COUNT(*) FROM tavole WHERE scaricata_il IS NOT NULL"),
            "posizioni": n("SELECT COUNT(*) FROM posizioni"),
            "posizioni_senza_codice": n("SELECT COUNT(*) FROM posizioni WHERE codice IS NULL"),
            "ricambi": n("SELECT COUNT(*) FROM ricambi"),
            "ricambi_con_foto": n("SELECT COUNT(*) FROM ricambi WHERE url_foto IS NOT NULL"),
        }
