"""
Archivio locale del catalogo Antonio Carraro (SQLite).

Tutto il catalogo (circa 470 prodotti e 9.400 tavole, vedi
debug/misura_catalogo_carraro.py) si scarica una volta con
scarica_tutto.py e poi si interroga qui, senza toccare il portale.
Dal vivo restano solo la ricerca per matricola e i codici precedenti,
che si salvano qui man mano che vengono chiesti (codici_precedenti).

Struttura (le tavole sono condivise fra prodotti, da qui il legame
molti-a-molti):

    prodotti --< prodotto_tavola >-- tavole --< righe
                                                  codici_precedenti

Chiavi:
  - prodotto: il suo percorso sul sito (/catalogo/serie/.../tgf-10900-r),
    stabile e gia' univoco;
  - tavola: il codice in minuscolo. Il sito lo scrive a volte maiuscolo
    (A00-0100-02) e a volte minuscolo (a01-0050-01): e' la stessa tavola.

Matricole: progressivi a 5 cifre salvati come TESTO ("00906"), per non
perdere gli zeri; il confronto si fa con dentro_intervallo().
L'intervallo di validita' di una tavola sta sul LEGAME prodotto-tavola,
non sulla tavola: la stessa tavola vale "da 03703" su un modello e
"da 00001" su un altro.
"""

import os
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_PATH = str(Path(__file__).resolve().parent / "carraro.db")

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS prodotti (
  url            TEXT PRIMARY KEY,   -- /catalogo/serie/.../tgf-10900-r
  sezione        TEXT,               -- serie, motori, rasaerba, ...
  categoria      TEXT,
  nome           TEXT,               -- senza la parentesi delle matricole
  nome_completo  TEXT,               -- come lo mostra il sito
  slug           TEXT,
  tipo           TEXT,               -- modello | kit | attrezzi
  matricola_da   TEXT,               -- dall'eventuale "(15-03120 ->)" nel nome
  matricola_a    TEXT,
  aggiornato_il  TEXT                -- ultima lettura della pagina prodotto
);
CREATE INDEX IF NOT EXISTS ix_prodotti_slug ON prodotti(slug);

CREATE TABLE IF NOT EXISTS tavole (
  codice         TEXT PRIMARY KEY,   -- minuscolo: a01-0050-01
  codice_sito    TEXT,               -- come scritto dal sito
  titolo         TEXT,               -- a01-0050 - radiatore
  descrizione    TEXT,               -- riga lunga, es. "7900 - 10900 - 8900"
  url            TEXT,               -- percorso da cui scaricarla
  scaricata_il   TEXT                -- NULL = ancora da scaricare
);

CREATE TABLE IF NOT EXISTS prodotto_tavola (
  prodotto       TEXT NOT NULL REFERENCES prodotti(url) ON DELETE CASCADE,
  tavola         TEXT NOT NULL,      -- tavole.codice (puo' non essere ancora scaricata)
  gruppo         TEXT,               -- A - MOTORE, B - AUTOTELAIO ...
  ordine         INTEGER,            -- posizione nella pagina del prodotto
  matricola_da   TEXT,
  matricola_a    TEXT,
  PRIMARY KEY (prodotto, tavola)
);
CREATE INDEX IF NOT EXISTS ix_pt_tavola ON prodotto_tavola(tavola);

CREATE TABLE IF NOT EXISTS righe (
  id             INTEGER PRIMARY KEY,
  tavola         TEXT NOT NULL REFERENCES tavole(codice) ON DELETE CASCADE,
  ordine         INTEGER,            -- ordine nella tavola
  posizione      TEXT,               -- numero sul disegno
  codice         TEXT NOT NULL,
  descrizione    TEXT,
  matricola_da   TEXT,
  matricola_a    TEXT,
  nota           TEXT,               -- sigle in colonna matricola: mt, ar ...
  info           TEXT,               -- colonna Modifiche/Info
  quantita       INTEGER,
  fornibile      INTEGER             -- 0 per 00000000 "Non fornibile"
);
CREATE INDEX IF NOT EXISTS ix_righe_codice ON righe(codice);
CREATE INDEX IF NOT EXISTS ix_righe_tavola ON righe(tavola);

-- documenti allegati alla pagina prodotto (cataloghi PDF di prodotti
-- senza tavole navigabili). 'nota' contiene le regole per ordinare
-- (es. prefisso WAG/WCP sui codici): va mostrata insieme al documento
CREATE TABLE IF NOT EXISTS allegati (
  prodotto       TEXT NOT NULL REFERENCES prodotti(url) ON DELETE CASCADE,
  url            TEXT NOT NULL,      -- /files/cataloghi-agritalia-pdf/...pdf
  nome           TEXT,
  tipo           TEXT,               -- PDF
  dimensione     TEXT,               -- 15.17 Mb
  nota           TEXT,
  PRIMARY KEY (prodotto, url)
);

-- codici precedenti (vecchio -> nuovo), dalla scheda codice del sito:
-- si riempie dal vivo, solo per i codici chiesti dai clienti
CREATE TABLE IF NOT EXISTS codici_precedenti (
  vecchio        TEXT NOT NULL,
  nuovo          TEXT NOT NULL,
  PRIMARY KEY (vecchio, nuovo)
);
CREATE INDEX IF NOT EXISTS ix_prec_nuovo ON codici_precedenti(nuovo);

-- codici gia' chiesti al sito (anche senza risultato), per non richiederli
CREATE TABLE IF NOT EXISTS codici_verificati (
  codice         TEXT PRIMARY KEY,
  verificato_il  TEXT
);

-- avanzamento del download (sezioni/prodotti completati, ecc.)
CREATE TABLE IF NOT EXISTS stato_download (
  chiave         TEXT PRIMARY KEY,
  fatto_il       TEXT
);
"""


def _adesso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def chiave_tavola(codice):
    return (codice or "").strip().lower()


def _estremi(intervallo):
    intervallo = intervallo or {}
    return intervallo.get("da"), intervallo.get("a")


# ----------------------------------------------------------------------
# Matricole
# ----------------------------------------------------------------------
def _progressivo(m):
    """'00906' -> 906. Accetta anche '08-00216' (prende il numero dopo il
    trattino). None se non e' un numero riconoscibile."""
    if m is None:
        return None
    s = str(m).strip().rsplit("-", 1)[-1]
    return int(s) if s.isdigit() else None


def dentro_intervallo(progressivo, da, a):
    """True se il progressivo cade in [da, a]. Estremi vuoti = aperti.
    Progressivo non riconoscibile = non filtra: meglio mostrare un pezzo
    in piu' che nascondere quello giusto (stessa regola di Goldoni)."""
    p = _progressivo(progressivo)
    if p is None:
        return True
    pda, pa = _progressivo(da), _progressivo(a)
    if pda is not None and p < pda:
        return False
    if pa is not None and p > pa:
        return False
    return True


class Db:
    """Connessione condivisibile tra thread, come fornitori/sdf/db.py:
    il server usa la stessa istanza da thread diversi, quindi
    check_same_thread=False piu' un lock esplicito."""

    def __init__(self, path=DEFAULT_PATH):
        self.path = path
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self._lk = threading.RLock()
        self.con = sqlite3.connect(path, check_same_thread=False, timeout=30)
        self.con.row_factory = sqlite3.Row
        with self._lk:
            self.con.execute("PRAGMA busy_timeout=30000")
            self.con.execute("PRAGMA foreign_keys=ON")
            self.con.executescript(SCHEMA)
            self.con.commit()

    def close(self):
        with self._lk:
            self.con.close()

    # ---------------------------------------------------------- primitive
    def query(self, sql, args=()):
        """Lettura thread-safe: usare sempre questa, mai db.con.execute."""
        with self._lk:
            return self.con.execute(sql, args).fetchall()

    def one(self, sql, args=()):
        with self._lk:
            return self.con.execute(sql, args).fetchone()

    def _write(self, statements):
        """Esegue piu' istruzioni in UNA transazione: o tutte o nessuna
        (una tavola salvata a meta' non deve mai risultare scaricata)."""
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

    # ---------------------------------------------------------- scritture
    def salva_prodotti(self, sezione, prodotti):
        """Prodotti di una sezione, da parser.parse_categoria()['prodotti']
        (con l'eventuale campo 'categoria' aggiunto dal chiamante).
        Non tocca aggiornato_il: lo imposta salva_tavole_prodotto."""
        righe = []
        for p in prodotti:
            da, a = _estremi(p.get("matricole"))
            righe.append((p["url"], sezione, p.get("categoria"), p["nome"],
                          p.get("nome_completo"), p.get("slug"), p.get("tipo"),
                          da, a))
        self._write([("""
            INSERT INTO prodotti (url, sezione, categoria, nome, nome_completo,
                                  slug, tipo, matricola_da, matricola_a)
            VALUES (?,?,?,?,?,?,?,?,?)
            ON CONFLICT(url) DO UPDATE SET
              sezione=excluded.sezione, categoria=excluded.categoria,
              nome=excluded.nome, nome_completo=excluded.nome_completo,
              slug=excluded.slug, tipo=excluded.tipo,
              matricola_da=excluded.matricola_da, matricola_a=excluded.matricola_a
        """, righe, True)])

    def salva_tavole_prodotto(self, url_prodotto, tavole):
        """Elenco tavole di un prodotto, da parser.parse_modello()['tavole'].
        Sostituisce i legami precedenti del prodotto (una tavola tolta dal
        sito sparisce anche qui) e registra le tavole nuove come 'da
        scaricare', senza toccare quelle gia' scaricate."""
        legami, nuove = [], []
        for i, t in enumerate(tavole):
            cod = chiave_tavola(t["codice"])
            if not cod:
                continue
            da, a = _estremi(t.get("matricole"))
            legami.append((url_prodotto, cod, t.get("gruppo"), i, da, a))
            nuove.append((cod, t["codice"], t.get("titolo"), t.get("descrizione"),
                          t.get("url")))
        self._write([
            ("DELETE FROM prodotto_tavola WHERE prodotto=?", (url_prodotto,), False),
            ("INSERT OR REPLACE INTO prodotto_tavola VALUES (?,?,?,?,?,?)", legami, True),
            ("INSERT OR IGNORE INTO tavole (codice, codice_sito, titolo, descrizione, url) "
             "VALUES (?,?,?,?,?)", nuove, True),
            ("UPDATE prodotti SET aggiornato_il=? WHERE url=?",
             (_adesso(), url_prodotto), False),
        ])

    def salva_allegati(self, url_prodotto, allegati):
        """Documenti di un prodotto, da parser.parse_allegati(). Sostituisce
        quelli precedenti (un PDF tolto dal sito sparisce anche qui)."""
        self._write([
            ("DELETE FROM allegati WHERE prodotto=?", (url_prodotto,), False),
            ("INSERT OR REPLACE INTO allegati VALUES (?,?,?,?,?,?)",
             [(url_prodotto, a["url"], a["nome"], a["tipo"], a["dimensione"],
               a["nota"]) for a in allegati if a.get("url")], True),
        ])

    def salva_tavola(self, codice, tavola):
        """Contenuto di una tavola, da parser.parse_tavola(). Righe
        sostituite in blocco e tavola segnata come scaricata, nella stessa
        transazione."""
        cod = chiave_tavola(codice)
        righe = []
        for i, r in enumerate(tavola["righe"]):
            da, a = _estremi(r.get("matricole"))
            righe.append((cod, i, r["posizione"], r["codice"], r["descrizione"],
                          da, a, r.get("nota") or None, r.get("info") or None,
                          r.get("quantita"), int(bool(r.get("fornibile", True)))))
        self._write([
            ("INSERT INTO tavole (codice, codice_sito, titolo) VALUES (?,?,?) "
             "ON CONFLICT(codice) DO UPDATE SET "
             "titolo=COALESCE(NULLIF(excluded.titolo, ''), tavole.titolo)",
             (cod, tavola.get("codice") or codice, tavola.get("titolo")), False),
            ("DELETE FROM righe WHERE tavola=?", (cod,), False),
            ("INSERT INTO righe (tavola, ordine, posizione, codice, descrizione, "
             "matricola_da, matricola_a, nota, info, quantita, fornibile) "
             "VALUES (?,?,?,?,?,?,?,?,?,?,?)", righe, True),
            ("UPDATE tavole SET scaricata_il=? WHERE codice=?", (_adesso(), cod), False),
        ])

    def salva_codici_precedenti(self, codice, precedenti):
        """Dalla scheda codice del sito: 'codice' ha sostituito 'precedenti'.
        Registra comunque il codice come verificato, anche senza precedenti."""
        st = [("INSERT OR REPLACE INTO codici_verificati VALUES (?,?)",
               (codice, _adesso()), False)]
        if precedenti:
            st.append(("INSERT OR IGNORE INTO codici_precedenti VALUES (?,?)",
                       [(v, codice) for v in precedenti if v and v != codice], True))
        self._write(st)

    # ------------------------------------------------------------- letture
    def tavole_da_scaricare(self, limite=None):
        sql = ("SELECT codice, codice_sito, url FROM tavole "
               "WHERE scaricata_il IS NULL ORDER BY codice")
        if limite:
            sql += f" LIMIT {int(limite)}"
        return self.query(sql)

    def prodotti(self, sezione=None):
        if sezione:
            return self.query("SELECT * FROM prodotti WHERE sezione=? ORDER BY nome",
                              (sezione,))
        return self.query("SELECT * FROM prodotti ORDER BY sezione, nome")

    def prodotto(self, url):
        return self.one("SELECT * FROM prodotti WHERE url=?", (url,))

    def tavole_prodotto(self, url_prodotto):
        """Tavole di un prodotto, nell'ordine del sito, con l'intervallo di
        matricola valido PER QUEL prodotto."""
        return self.query("""
            SELECT t.*, pt.gruppo, pt.matricola_da, pt.matricola_a, pt.ordine
            FROM prodotto_tavola pt JOIN tavole t ON t.codice = pt.tavola
            WHERE pt.prodotto = ? ORDER BY pt.ordine
        """, (url_prodotto,))

    def allegati(self, url_prodotto):
        return self.query("SELECT * FROM allegati WHERE prodotto=? ORDER BY nome",
                          (url_prodotto,))

    def solo_pdf(self, url_prodotto):
        """True se il prodotto non ha tavole ma ha documenti allegati: il bot
        non puo' cercarci dentro e deve rimandare al PDF."""
        return (self.one("SELECT 1 FROM prodotto_tavola WHERE prodotto=?",
                         (url_prodotto,)) is None
                and self.one("SELECT 1 FROM allegati WHERE prodotto=?",
                             (url_prodotto,)) is not None)

    def righe_tavola(self, codice):
        return self.query("SELECT * FROM righe WHERE tavola=? ORDER BY ordine",
                          (chiave_tavola(codice),))

    def cerca_codice(self, codice, url_prodotto=None):
        """Righe con quel codice, con la tavola e (se dato) solo nelle
        tavole del prodotto."""
        sql = """
            SELECT r.*, t.titolo AS titolo_tavola, t.codice_sito AS tavola_sito
            FROM righe r JOIN tavole t ON t.codice = r.tavola
        """
        args = [codice.strip()]
        if url_prodotto:
            sql += " JOIN prodotto_tavola pt ON pt.tavola = r.tavola AND pt.prodotto = ?"
            args.insert(0, url_prodotto)
        sql += " WHERE r.codice = ? ORDER BY r.tavola, r.ordine"
        return self.query(sql, args)

    def prodotti_con_codice(self, codice):
        """'Su quali macchine monta': prodotti le cui tavole contengono il codice."""
        return self.query("""
            SELECT DISTINCT p.url, p.nome, p.sezione, p.tipo,
                   pt.tavola, pt.matricola_da, pt.matricola_a
            FROM righe r
            JOIN prodotto_tavola pt ON pt.tavola = r.tavola
            JOIN prodotti p ON p.url = pt.prodotto
            WHERE r.codice = ?
            ORDER BY p.sezione, p.nome
        """, (codice.strip(),))

    def cerca_descrizione(self, termini, url_prodotto=None, limite=200):
        """Righe la cui descrizione contiene TUTTI i termini (LIKE, senza
        distinzione di maiuscole). I termini arrivano gia' normalizzati dal
        chiamante (radici di fornitori/testo.py), come in SDF."""
        termini = [t for t in termini if t]
        if not termini:
            return []
        where = " AND ".join("r.descrizione LIKE ?" for _ in termini)
        args = [f"%{t}%" for t in termini]
        sql = """
            SELECT r.*, t.titolo AS titolo_tavola, t.codice_sito AS tavola_sito
            FROM righe r JOIN tavole t ON t.codice = r.tavola
        """
        if url_prodotto:
            sql += """
                JOIN prodotto_tavola pt ON pt.tavola = r.tavola AND pt.prodotto = ?
            """
            args.insert(0, url_prodotto)
        sql += f" WHERE {where} ORDER BY r.tavola, r.ordine LIMIT ?"
        args.append(int(limite))
        return self.query(sql, args)

    def nuovo_codice(self, vecchio):
        """Codici che hanno sostituito 'vecchio' (gia' noti in locale)."""
        return [r["nuovo"] for r in self.query(
            "SELECT nuovo FROM codici_precedenti WHERE vecchio=?", (vecchio.strip(),))]

    def codici_precedenti(self, nuovo):
        return [r["vecchio"] for r in self.query(
            "SELECT vecchio FROM codici_precedenti WHERE nuovo=?", (nuovo.strip(),))]

    def codice_verificato(self, codice):
        return self.one("SELECT 1 FROM codici_verificati WHERE codice=?",
                        (codice.strip(),)) is not None

    def statistiche(self):
        out = {t: self.one(f"SELECT COUNT(*) AS n FROM {t}")["n"]
               for t in ("prodotti", "tavole", "prodotto_tavola", "righe",
                         "allegati", "codici_precedenti")}
        out["prodotti_solo_pdf"] = self.one("""
            SELECT COUNT(*) AS n FROM prodotti p
            WHERE NOT EXISTS (SELECT 1 FROM prodotto_tavola pt WHERE pt.prodotto = p.url)
              AND EXISTS (SELECT 1 FROM allegati a WHERE a.prodotto = p.url)""")["n"]
        out["tavole_scaricate"] = self.one(
            "SELECT COUNT(*) AS n FROM tavole WHERE scaricata_il IS NOT NULL")["n"]
        out["prodotti_per_sezione"] = {
            r["sezione"]: r["n"] for r in self.query(
                "SELECT sezione, COUNT(*) AS n FROM prodotti GROUP BY sezione")}
        return out
