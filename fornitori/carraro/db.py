"""
Archivio locale del catalogo Antonio Carraro (SQLite).

Struttura ricalcata su fornitori/sdf/db.py (il DB SDF e' quello completo
e di riferimento):

    SDF                         Carraro
    family / model              prodotti (sezione, categoria)
    grp / subgroup              prodotto_tavola.gruppo (un solo livello)
    drawing                     tavole (disegno, dimensioni)
    model_drawing               prodotto_tavola
    part                        righe        <- PER MODELLO, vedi sotto
    hotspot                     punti
    substitution (+ checked)    codici_precedenti (+ anagrafica)
    crawl_state                 stato_download

    prodotti --< prodotto_tavola >-- tavole --< punti
        |               |
        +--------------+--< righe (prodotto, tavola)

Differenza da SDF: in SDF una tavola condivisa fra modelli e' identica
per tutti. In Carraro no: la stessa tavola mostra righe diverse a
seconda del modello da cui la si apre (a01-0050-01: 52 righe dal Tony
10900 SR, 51 dal Tony 8900 SR, 90 dall'indirizzo diretto). Quindi le
righe stanno sul legame prodotto-tavola, non sulla tavola. Il disegno e i
punti invece sono della tavola (i punti sono l'unione di quelli visti dai
vari modelli: ogni modello mostra solo le sue posizioni).

anagrafica: tutti i codici del sito (ricerca '*'), con descrizione; da
qui si ricavano i codici precedenti (vecchio -> nuovo), come
substitution in SDF.

Immagini: si salva solo l'URL completo (https://ricambinet.../files/...),
il file non si scarica. tavole.immagine e' il disegno esploso,
prodotti.immagine la foto del modello.

Chiavi:
  - prodotto: il suo percorso sul sito (/catalogo/serie/.../tgf-10900-r);
  - tavola: il codice in minuscolo (il sito scrive A00-0100-02 e
    a01-0050-01: e' la stessa tavola).

Matricole: progressivi a 5 cifre salvati come TESTO ("00906"), per non
perdere gli zeri; il confronto si fa con dentro_intervallo().
L'intervallo di validita' di una tavola sta sul LEGAME prodotto-tavola:
la stessa tavola vale "da 03703" su un modello e "da 00001" su un altro.
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
  immagine       TEXT,               -- URL foto: https://.../files/immagini/modelli/...
  aggiornato_il  TEXT                -- ultima lettura della pagina prodotto
);
CREATE INDEX IF NOT EXISTS ix_prodotti_slug ON prodotti(slug);

CREATE TABLE IF NOT EXISTS tavole (
  codice         TEXT PRIMARY KEY,   -- minuscolo: a01-0050-01
  codice_sito    TEXT,               -- come scritto dal sito
  titolo         TEXT,               -- a01-0050 - radiatore
  descrizione    TEXT,               -- riga lunga, es. "7900 - 10900 - 8900"
  immagine       TEXT,               -- URL disegno: https://.../files/immagini/tavolecarrarosvg/...png
  immagine_larghezza INTEGER,        -- pixel, per leggere i punti
  immagine_altezza   INTEGER
);

CREATE TABLE IF NOT EXISTS prodotto_tavola (
  prodotto       TEXT NOT NULL REFERENCES prodotti(url) ON DELETE CASCADE,
  tavola         TEXT NOT NULL REFERENCES tavole(codice),
  gruppo         TEXT,               -- A - MOTORE, B - AUTOTELAIO ...
  ordine         INTEGER,            -- posizione nella pagina del prodotto
  matricola_da   TEXT,
  matricola_a    TEXT,
  url            TEXT,               -- la tavola vista da QUESTO prodotto
  scaricata_il   TEXT,               -- NULL = righe di questo modello non ancora lette
  PRIMARY KEY (prodotto, tavola)
);
CREATE INDEX IF NOT EXISTS ix_pt_tavola ON prodotto_tavola(tavola);

CREATE TABLE IF NOT EXISTS righe (
  id             INTEGER PRIMARY KEY,
  prodotto       TEXT NOT NULL,      -- il modello da cui e' stata letta la tavola
  tavola         TEXT NOT NULL,
  ordine         INTEGER,            -- ordine nella tavola
  posizione      TEXT,               -- numero sul disegno
  codice         TEXT NOT NULL,
  descrizione    TEXT,
  matricola_da   TEXT,
  matricola_a    TEXT,
  nota           TEXT,               -- sigle in colonna matricola: mt, ar ...
  info           TEXT,               -- colonna Modifiche/Info
  quantita       INTEGER,
  fornibile      INTEGER,            -- 0 per 00000000 "Non fornibile"
  FOREIGN KEY (prodotto, tavola) REFERENCES prodotto_tavola(prodotto, tavola)
    ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS ix_righe_codice ON righe(codice);
CREATE INDEX IF NOT EXISTS ix_righe_legame ON righe(prodotto, tavola);
CREATE INDEX IF NOT EXISTS ix_righe_tavola ON righe(tavola);

-- riquadri cliccabili sul disegno, uno per numero di posizione (in pixel
-- dell'immagine originale, origine in alto a sinistra)
CREATE TABLE IF NOT EXISTS punti (
  tavola         TEXT NOT NULL REFERENCES tavole(codice) ON DELETE CASCADE,
  posizione      TEXT,
  x              REAL,
  y              REAL,
  larghezza      REAL,
  altezza        REAL,
  UNIQUE (tavola, posizione, x, y)
);

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

-- tutti i codici del sito (ricerca '*'), anche quelli che non compaiono
-- in nessuna tavola (vecchi, OLD-...)
CREATE TABLE IF NOT EXISTS anagrafica (
  codice         TEXT PRIMARY KEY,
  descrizione    TEXT,
  letto_il       TEXT
);

-- codici precedenti (vecchio -> nuovo): dalla colonna "CodicePrecedente"
-- dell'anagrafica, e dal vivo dalla scheda di un codice
CREATE TABLE IF NOT EXISTS codici_precedenti (
  vecchio        TEXT NOT NULL,
  nuovo          TEXT NOT NULL,
  PRIMARY KEY (vecchio, nuovo)
);
CREATE INDEX IF NOT EXISTS ix_prec_nuovo ON codici_precedenti(nuovo);

-- codici gia' chiesti dal vivo alla scheda codice (anche senza risultato)
CREATE TABLE IF NOT EXISTS codici_verificati (
  codice         TEXT PRIMARY KEY,
  verificato_il  TEXT
);

-- avanzamento del download (come crawl_state in SDF):
--   sez:<sezione>   elenco della sezione letto
--   prod:<url>      modello completo (tutte le sue tavole tentate)
--   ana:<pagina>    pagina dell'anagrafica letta
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
    lo scaricamento e il server usano la stessa istanza da thread diversi,
    quindi check_same_thread=False piu' un lock esplicito."""

    def __init__(self, path=DEFAULT_PATH):
        self.path = path
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self._lk = threading.RLock()
        self.con = sqlite3.connect(path, check_same_thread=False, timeout=30)
        self.con.row_factory = sqlite3.Row
        with self._lk:
            self.con.execute("PRAGMA busy_timeout=30000")
            self.con.execute("PRAGMA foreign_keys=ON")
            colonne = {r["name"] for r in self.con.execute("PRAGMA table_info(righe)")}
            if colonne and "prodotto" not in colonne:
                raise RuntimeError(
                    f"{path} ha la struttura vecchia (righe senza prodotto): "
                    "rinominalo (es. carraro_v1.db) e riscarica da capo")
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

    def chiavi_fatte(self, prefisso):
        return {r["chiave"] for r in self.query(
            "SELECT chiave FROM stato_download WHERE chiave LIKE ?", (prefisso + "%",))}

    # ---------------------------------------------------------- scritture
    def salva_prodotti(self, sezione, prodotti):
        """Prodotti di una sezione, da parser.parse_categoria()['prodotti']
        (con il campo 'categoria' aggiunto dal chiamante).
        Non tocca aggiornato_il: lo imposta salva_tavole_prodotto."""
        righe = []
        for p in prodotti:
            da, a = _estremi(p.get("matricole"))
            righe.append((p["url"], sezione, p.get("categoria"), p["nome"],
                          p.get("nome_completo"), p.get("slug"), p.get("tipo"),
                          da, a, p.get("immagine") or None))
        self._write([("""
            INSERT INTO prodotti (url, sezione, categoria, nome, nome_completo,
                                  slug, tipo, matricola_da, matricola_a, immagine)
            VALUES (?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(url) DO UPDATE SET
              sezione=excluded.sezione, categoria=excluded.categoria,
              nome=excluded.nome, nome_completo=excluded.nome_completo,
              slug=excluded.slug, tipo=excluded.tipo,
              matricola_da=excluded.matricola_da, matricola_a=excluded.matricola_a,
              immagine=COALESCE(excluded.immagine, prodotti.immagine)
        """, righe, True)])

    def salva_tavole_prodotto(self, url_prodotto, tavole):
        """Elenco tavole di un prodotto, da parser.parse_modello()['tavole'].
        I legami spariti dal sito si tolgono (con le loro righe); quelli
        rimasti conservano le righe gia' scaricate. Le tavole nuove si
        registrano senza toccare quelle gia' note."""
        legami, nuove, codici = [], [], []
        for i, t in enumerate(tavole):
            cod = chiave_tavola(t["codice"])
            if not cod or cod in codici:
                continue
            codici.append(cod)
            da, a = _estremi(t.get("matricole"))
            legami.append((url_prodotto, cod, t.get("gruppo"), i, da, a, t.get("url")))
            nuove.append((cod, t["codice"], t.get("titolo"), t.get("descrizione")))
        tolte = [(url_prodotto, r["tavola"]) for r in self.query(
            "SELECT tavola FROM prodotto_tavola WHERE prodotto=?", (url_prodotto,))
            if r["tavola"] not in codici]
        self._write([
            ("DELETE FROM righe WHERE prodotto=? AND tavola=?", tolte, True),
            ("DELETE FROM prodotto_tavola WHERE prodotto=? AND tavola=?", tolte, True),
            ("INSERT OR IGNORE INTO tavole (codice, codice_sito, titolo, descrizione) "
             "VALUES (?,?,?,?)", nuove, True),
            ("""INSERT INTO prodotto_tavola
                  (prodotto, tavola, gruppo, ordine, matricola_da, matricola_a, url)
                VALUES (?,?,?,?,?,?,?)
                ON CONFLICT(prodotto, tavola) DO UPDATE SET
                  gruppo=excluded.gruppo, ordine=excluded.ordine,
                  matricola_da=excluded.matricola_da, matricola_a=excluded.matricola_a,
                  url=excluded.url""", legami, True),
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

    def salva_righe(self, url_prodotto, codice, tavola):
        """Una tavola vista da un modello, da parser.parse_tavola(): righe
        del legame sostituite in blocco, disegno della tavola (se non
        ancora noto) e punti aggiunti, legame segnato come scaricato. Tutto
        in una transazione."""
        cod = chiave_tavola(codice)
        dis = tavola.get("disegno") or {}
        righe = []
        for i, r in enumerate(tavola["righe"]):
            da, a = _estremi(r.get("matricole"))
            righe.append((url_prodotto, cod, i, r["posizione"], r["codice"],
                          r["descrizione"], da, a, r.get("nota") or None,
                          r.get("info") or None, r.get("quantita"),
                          int(bool(r.get("fornibile", True)))))
        punti = [(cod, p["posizione"], p["x"], p["y"], p["larghezza"], p["altezza"])
                 for p in dis.get("punti", [])]
        self._write([
            ("UPDATE tavole SET titolo=COALESCE(NULLIF(?, ''), titolo), "
             "immagine=COALESCE(immagine, ?), "
             "immagine_larghezza=COALESCE(immagine_larghezza, ?), "
             "immagine_altezza=COALESCE(immagine_altezza, ?) WHERE codice=?",
             (tavola.get("titolo"), dis.get("url") or None, dis.get("larghezza"),
              dis.get("altezza"), cod), False),
            ("DELETE FROM righe WHERE prodotto=? AND tavola=?", (url_prodotto, cod), False),
            ("INSERT INTO righe (prodotto, tavola, ordine, posizione, codice, "
             "descrizione, matricola_da, matricola_a, nota, info, quantita, "
             "fornibile) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", righe, True),
            ("INSERT OR IGNORE INTO punti VALUES (?,?,?,?,?,?)", punti, True),
            ("UPDATE prodotto_tavola SET scaricata_il=? WHERE prodotto=? AND tavola=?",
             (_adesso(), url_prodotto, cod), False),
        ])

    def salva_anagrafica(self, risultati):
        """Una pagina della ricerca '*', da parser.parse_ricerca_codici():
        codici con descrizione e i loro codici precedenti."""
        ora = _adesso()
        self._write([
            ("INSERT OR REPLACE INTO anagrafica VALUES (?,?,?)",
             [(r["codice"], r["descrizione"], ora) for r in risultati if r["codice"]],
             True),
            ("INSERT OR IGNORE INTO codici_precedenti VALUES (?,?)",
             [(v, r["codice"]) for r in risultati if r["codice"]
              for v in r["codici_precedenti"] if v and v != r["codice"]], True),
        ])

    def salva_codici_precedenti(self, codice, precedenti):
        """Dal vivo, dalla scheda codice del sito: 'codice' ha sostituito
        'precedenti'. Registra comunque il codice come verificato."""
        st = [("INSERT OR REPLACE INTO codici_verificati VALUES (?,?)",
               (codice, _adesso()), False)]
        if precedenti:
            st.append(("INSERT OR IGNORE INTO codici_precedenti VALUES (?,?)",
                       [(v, codice) for v in precedenti if v and v != codice], True))
        self._write(st)

    # -------------------------------------------------- code di lavoro
    def legami_da_scaricare(self, url_prodotto):
        return self.query("""
            SELECT pt.tavola, pt.url, t.codice_sito
            FROM prodotto_tavola pt JOIN tavole t ON t.codice = pt.tavola
            WHERE pt.prodotto = ? AND pt.scaricata_il IS NULL
            ORDER BY pt.ordine""", (url_prodotto,))

    def modelli_da_scaricare(self):
        """Prodotti non completi, piu' quelli completi ma con tavole rimaste
        indietro (errore al giro prima: si ritentano). Prima quelli gia'
        iniziati, come in SDF."""
        fatti = self.chiavi_fatte("prod:")
        con_buchi = {r["prodotto"] for r in self.query(
            "SELECT DISTINCT prodotto FROM prodotto_tavola WHERE scaricata_il IS NULL")}
        iniziati = {r["prodotto"] for r in self.query(
            "SELECT DISTINCT prodotto FROM prodotto_tavola WHERE scaricata_il IS NOT NULL")}
        coda = [dict(p) for p in self.query(
            "SELECT url, nome, sezione, categoria FROM prodotti ORDER BY sezione, url")
            if f"prod:{p['url']}" not in fatti or p["url"] in con_buchi]
        coda.sort(key=lambda p: p["url"] not in iniziati)
        return coda

    # ------------------------------------------------------------- letture
    def prodotti(self, sezione=None):
        if sezione:
            return self.query("SELECT * FROM prodotti WHERE sezione=? ORDER BY nome",
                              (sezione,))
        return self.query("SELECT * FROM prodotti ORDER BY sezione, nome")

    def prodotto(self, url):
        return self.one("SELECT * FROM prodotti WHERE url=?", (url,))

    def modelli_pronti(self):
        """Prodotti scaricati per intero (come modelli_pronti in SDF)."""
        fatti = self.chiavi_fatte("prod:")
        return [p for p in self.prodotti() if f"prod:{p['url']}" in fatti]

    def tavole_prodotto(self, url_prodotto):
        """Tavole di un prodotto, nell'ordine del sito, con l'intervallo di
        matricola valido PER QUEL prodotto."""
        return self.query("""
            SELECT t.*, pt.gruppo, pt.matricola_da, pt.matricola_a, pt.ordine,
                   pt.scaricata_il
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

    def righe_tavola(self, codice, url_prodotto):
        """Righe di una tavola come le vede quel prodotto."""
        return self.query("SELECT * FROM righe WHERE prodotto=? AND tavola=? "
                          "ORDER BY ordine", (url_prodotto, chiave_tavola(codice)))

    def punti_tavola(self, codice, posizione=None):
        """Riquadri sul disegno (tutti, o solo quelli di una posizione)."""
        sql, args = "SELECT * FROM punti WHERE tavola=?", [chiave_tavola(codice)]
        if posizione is not None:
            sql += " AND posizione=?"
            args.append(str(posizione))
        return self.query(sql, args)

    _COLONNE_RIGA = """
        r.tavola, r.posizione, r.codice, r.descrizione, r.matricola_da,
        r.matricola_a, r.nota, r.info, r.quantita, r.fornibile,
        t.titolo AS titolo_tavola, t.codice_sito AS tavola_sito,
        t.immagine AS immagine_tavola"""

    def cerca_codice(self, codice, url_prodotto=None):
        """Righe con quel codice: nelle tavole del prodotto, oppure (senza
        prodotto) in tutto il catalogo, una volta per tavola e posizione."""
        if url_prodotto:
            return self.query(f"""
                SELECT r.ordine, {self._COLONNE_RIGA}
                FROM righe r JOIN tavole t ON t.codice = r.tavola
                WHERE r.prodotto = ? AND r.codice = ? ORDER BY r.tavola, r.ordine
            """, (url_prodotto, codice.strip()))
        return self.query(f"""
            SELECT DISTINCT {self._COLONNE_RIGA}
            FROM righe r JOIN tavole t ON t.codice = r.tavola
            WHERE r.codice = ? ORDER BY r.tavola, r.posizione
        """, (codice.strip(),))

    def prodotti_con_codice(self, codice):
        """'Su quali macchine monta': prodotti che hanno il codice nelle
        righe delle loro tavole."""
        return self.query("""
            SELECT DISTINCT p.url, p.nome, p.sezione, p.tipo,
                   pt.tavola, pt.matricola_da, pt.matricola_a
            FROM righe r
            JOIN prodotto_tavola pt ON pt.prodotto = r.prodotto AND pt.tavola = r.tavola
            JOIN prodotti p ON p.url = r.prodotto
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
        if url_prodotto:
            sql = f"""SELECT r.ordine, {self._COLONNE_RIGA}
                      FROM righe r JOIN tavole t ON t.codice = r.tavola
                      WHERE r.prodotto = ? AND {where}
                      ORDER BY r.tavola, r.ordine LIMIT ?"""
            args.insert(0, url_prodotto)
        else:
            sql = f"""SELECT DISTINCT {self._COLONNE_RIGA}
                      FROM righe r JOIN tavole t ON t.codice = r.tavola
                      WHERE {where} ORDER BY r.tavola, r.posizione LIMIT ?"""
        args.append(int(limite))
        return self.query(sql, args)

    def descrizione_codice(self, codice):
        """Descrizione dall'anagrafica (anche per codici fuori dalle tavole)."""
        r = self.one("SELECT descrizione FROM anagrafica WHERE codice=?", (codice.strip(),))
        return r["descrizione"] if r else None

    def nuovo_codice(self, vecchio):
        """Codici che hanno sostituito 'vecchio'."""
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
               for t in ("prodotti", "tavole", "prodotto_tavola", "righe", "punti",
                         "allegati", "anagrafica", "codici_precedenti")}
        n = lambda sql: self.one(sql)["n"]
        out["prodotti_pronti"] = len(self.chiavi_fatte("prod:"))
        out["prodotti_con_foto"] = n("SELECT COUNT(*) AS n FROM prodotti WHERE immagine IS NOT NULL")
        out["prodotti_solo_pdf"] = n("""
            SELECT COUNT(*) AS n FROM prodotti p
            WHERE NOT EXISTS (SELECT 1 FROM prodotto_tavola pt WHERE pt.prodotto = p.url)
              AND EXISTS (SELECT 1 FROM allegati a WHERE a.prodotto = p.url)""")
        out["legami_scaricati"] = n(
            "SELECT COUNT(*) AS n FROM prodotto_tavola WHERE scaricata_il IS NOT NULL")
        out["tavole_con_disegno"] = n("SELECT COUNT(*) AS n FROM tavole WHERE immagine IS NOT NULL")
        out["codici_in_tavole"] = n("SELECT COUNT(DISTINCT codice) AS n FROM righe")
        out["codici_fuori_anagrafica"] = n("""
            SELECT COUNT(DISTINCT r.codice) AS n FROM righe r
            WHERE NOT EXISTS (SELECT 1 FROM anagrafica a WHERE a.codice = r.codice)""")
        out["pagine_anagrafica"] = len(self.chiavi_fatte("ana:"))
        out["prodotti_per_sezione"] = {
            r["sezione"]: r["n"] for r in self.query(
                "SELECT sezione, COUNT(*) AS n FROM prodotti GROUP BY sezione")}
        return out
