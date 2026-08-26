"""Persistenza SQLite del catalogo ricambi (thread-safe)."""
import os
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

# Calcolato dalla posizione di questo file, non dalla cartella da cui si
# lancia il comando: altrimenti un cwd diverso (es. un servizio avviato
# altrove) creerebbe/cercherebbe il DB nel posto sbagliato.
DEFAULT_PATH = str(Path(__file__).resolve().parent / "sdf.db")

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS family (
  brand TEXT, row_id INTEGER, code TEXT, name TEXT, macro TEXT,
  PRIMARY KEY (brand, row_id)
);

CREATE TABLE IF NOT EXISTS model (
  brand TEXT, row_id INTEGER, family_id INTEGER,
  code TEXT, name TEXT, sort_key TEXT, start_vin TEXT,
  PRIMARY KEY (brand, row_id)
);
CREATE INDEX IF NOT EXISTS ix_model_family ON model(brand, family_id);

CREATE TABLE IF NOT EXISTS grp (
  row_id INTEGER PRIMARY KEY, code TEXT, name TEXT, sort_key TEXT
);

CREATE TABLE IF NOT EXISTS subgroup (
  row_id INTEGER PRIMARY KEY, group_id INTEGER,
  code TEXT, name TEXT, sort_key TEXT
);

CREATE TABLE IF NOT EXISTS drawing (
  revision_id INTEGER PRIMARY KEY,
  name TEXT, rif TEXT, nove_punto TEXT,
  image_name TEXT, preview_url TEXT,
  tractor_sn_range TEXT, engine_sn TEXT, notes TEXT,
  fetched_at TEXT
);

CREATE TABLE IF NOT EXISTS model_drawing (
  brand TEXT, family_id INTEGER, model_id INTEGER,
  group_id INTEGER, subgroup_id INTEGER, revision_id INTEGER,
  PRIMARY KEY (brand, model_id, subgroup_id, revision_id)
);
CREATE INDEX IF NOT EXISTS ix_md_rev ON model_drawing(revision_id);
CREATE INDEX IF NOT EXISTS ix_md_model ON model_drawing(brand, model_id);

CREATE TABLE IF NOT EXISTS part (
  revision_id INTEGER, position TEXT, code TEXT,
  description TEXT, quantity TEXT,
  sellable INTEGER, replaced INTEGER, abolished INTEGER, price REAL,
  PRIMARY KEY (revision_id, position, code)
);
CREATE INDEX IF NOT EXISTS ix_part_code ON part(code);

CREATE TABLE IF NOT EXISTS substitution (
  old_code TEXT, new_code TEXT, description TEXT, quantity TEXT,
  PRIMARY KEY (old_code, new_code)
);
CREATE INDEX IF NOT EXISTS ix_sub_new ON substitution(new_code);

CREATE TABLE IF NOT EXISTS substitution_checked (code TEXT PRIMARY KEY);

CREATE TABLE IF NOT EXISTS hotspot (
  revision_id INTEGER, position TEXT, code TEXT,
  x1 INTEGER, y1 INTEGER, x2 INTEGER, y2 INTEGER
);
CREATE INDEX IF NOT EXISTS ix_hotspot_rev ON hotspot(revision_id);

CREATE TABLE IF NOT EXISTS crawl_state (key TEXT PRIMARY KEY, done_at TEXT);
"""


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Db:
    """Connessione condivisibile tra thread.

    FastAPI esegue gli endpoint sincroni in un threadpool: la stessa istanza
    viene usata da thread diversi tra una richiesta e l'altra. Servono quindi
    check_same_thread=False piu' un lock esplicito, perche' la connessione
    sqlite3 non e' thread-safe da sola.
    """

    def __init__(self, path=DEFAULT_PATH):
        self.path = path
        cartella = os.path.dirname(os.path.abspath(path))
        os.makedirs(cartella, exist_ok=True)
        self._lk = threading.RLock()
        self.con = sqlite3.connect(path, check_same_thread=False, timeout=30)
        self.con.row_factory = sqlite3.Row
        with self._lk:
            self.con.execute("PRAGMA busy_timeout=30000")
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
        with self._lk:
            for sql, args, many in statements:
                if many:
                    self.con.executemany(sql, args)
                else:
                    self.con.execute(sql, args)
            self.con.commit()

    # ------------------------------------------------------------ progressi
    def is_done(self, key):
        return self.one("SELECT 1 FROM crawl_state WHERE key=?", (key,)) is not None

    def mark_done(self, key):
        self._write([("INSERT OR REPLACE INTO crawl_state VALUES (?,?)",
                      (key, _now()), False)])

    def has_drawing(self, revision_id):
        return self.one("SELECT 1 FROM drawing WHERE revision_id=?",
                        (revision_id,)) is not None

    def sub_checked(self, code):
        return self.one("SELECT 1 FROM substitution_checked WHERE code=?",
                        (code,)) is not None

    # -------------------------------------------------------------- scritture
    def save_families(self, brand, families):
        self._write([("INSERT OR REPLACE INTO family VALUES (?,?,?,?,?)",
                      [(brand, f["row_id"], f["code"], f["name"], f["macro"])
                       for f in families], True)])

    def save_models(self, brand, models):
        self._write([("INSERT OR REPLACE INTO model VALUES (?,?,?,?,?,?,?)",
                      [(brand, m["row_id"], m["family_id"], m["code"], m["name"],
                        m["sort_key"], m["start_vin"]) for m in models], True)])

    def save_groups(self, groups):
        subs = [(s["row_id"], g["row_id"], s["code"], s["name"], s["sort_key"])
                for g in groups for s in g["subgroups"]]
        self._write([
            ("INSERT OR REPLACE INTO grp VALUES (?,?,?,?)",
             [(g["row_id"], g["code"], g["name"], g["sort_key"]) for g in groups], True),
            ("INSERT OR REPLACE INTO subgroup VALUES (?,?,?,?,?)", subs, True),
        ])

    def save_drawing(self, meta, parts, hotspots, brand, family_id, model_id):
        rev = meta["revision_id"]
        self._write([
            ("INSERT OR REPLACE INTO drawing VALUES (?,?,?,?,?,?,?,?,?,?)",
             (rev, meta["name"], meta["rif"], meta["nove_punto"], meta["image_name"],
              meta["preview_url"], meta["tractor_sn_range"], meta["engine_sn"],
              meta["notes"], _now()), False),
            ("INSERT OR REPLACE INTO model_drawing VALUES (?,?,?,?,?,?)",
             (brand, family_id, model_id, meta["group_id"], meta["subgroup_id"], rev),
             False),
            ("INSERT OR REPLACE INTO part VALUES (?,?,?,?,?,?,?,?,?)",
             [(rev, p["position"], p["code"], p["description"], p["quantity"],
               int(p["sellable"]), int(p["replaced"]), int(p["abolished"]), p["price"])
              for p in parts], True),
            ("DELETE FROM hotspot WHERE revision_id=?", (rev,), False),
            ("INSERT INTO hotspot VALUES (?,?,?,?,?,?,?)",
             [(rev, h["position"], h["code"], h["x1"], h["y1"], h["x2"], h["y2"])
              for h in hotspots], True),
        ])

    def link_drawing(self, brand, family_id, model_id, group_id, subgroup_id,
                     revision_id):
        self._write([("INSERT OR REPLACE INTO model_drawing VALUES (?,?,?,?,?,?)",
                      (brand, family_id, model_id, group_id, subgroup_id,
                       revision_id), False)])

    def save_substitutions(self, old_code, subs):
        st = [("INSERT OR REPLACE INTO substitution_checked VALUES (?)",
               (old_code,), False)]
        if subs:
            st.append(("INSERT OR REPLACE INTO substitution VALUES (?,?,?,?)",
                       [(old_code, s["code"], s["description"], s["quantity"])
                        for s in subs], True))
        self._write(st)

    # -------------------------------------------------------------- letture
    def find_part(self, code):
        return self.query("""
            SELECT p.*, d.name AS drawing_name, d.rif,
                   md.brand, md.model_id, m.name AS model_name
            FROM part p
            JOIN drawing d ON d.revision_id = p.revision_id
            LEFT JOIN model_drawing md ON md.revision_id = p.revision_id
            LEFT JOIN model m ON m.row_id = md.model_id AND m.brand = md.brand
            WHERE p.code = ?
        """, (code,))

    def search_description(self, text, limit=50):
        words = [w for w in text.split() if w]
        if not words:
            return []
        where = " AND ".join("description LIKE ?" for _ in words)
        args = [f"%{w}%" for w in words] + [limit]
        return self.query(
            f"SELECT DISTINCT code, description FROM part WHERE {where} LIMIT ?",
            args)

    def stats(self):
        return {t: self.one(f"SELECT COUNT(*) AS n FROM {t}")["n"]
                for t in ("family", "model", "grp", "subgroup", "drawing",
                          "model_drawing", "part", "substitution")}

    def modelli_pronti(self):
        """Modelli il cui download e' completo (crawl_state 'mod:...'):
        quelli su cui il bot risponde subito, senza aspettare il crawl."""
        chiavi = self.query("SELECT key FROM crawl_state WHERE key LIKE 'mod:%'")
        out = []
        for r in chiavi:
            try:
                _, brand, model_id = r["key"].split(":")
                model_id = int(model_id)
            except ValueError:
                continue
            fam = self.one(
                "SELECT DISTINCT family_id FROM model_drawing WHERE brand=? AND model_id=?",
                (brand, model_id))
            nome = self.one("SELECT name FROM model WHERE brand=? AND row_id=?",
                            (brand, model_id))
            n_tavole = self.one(
                "SELECT COUNT(DISTINCT revision_id) AS n FROM model_drawing "
                "WHERE brand=? AND model_id=?", (brand, model_id))
            out.append({
                "brand": brand, "model_id": model_id,
                "family_id": fam["family_id"] if fam else None,
                "nome": nome["name"] if nome else None,
                "n_tavole": n_tavole["n"] if n_tavole else 0,
            })
        return out

    def modelli_incompleti(self):
        """Modelli con dati parziali in locale: crawl iniziato (in corso o
        interrotto) ma non finito. Non si perde nulla riprendendolo: le
        tavole gia' qui non si riscaricano (vedi Crawler.crawl_group)."""
        modelli = self.query(
            "SELECT DISTINCT brand, model_id, family_id FROM model_drawing")
        completi = {r["key"] for r in
                   self.query("SELECT key FROM crawl_state WHERE key LIKE 'mod:%'")}
        out = []
        for r in modelli:
            key = f"mod:{r['brand']}:{r['model_id']}"
            if key in completi:
                continue
            n_tavole = self.one(
                "SELECT COUNT(DISTINCT revision_id) AS n FROM model_drawing "
                "WHERE brand=? AND model_id=?", (r["brand"], r["model_id"]))
            n_gruppi = self.one(
                "SELECT COUNT(*) AS n FROM crawl_state WHERE key LIKE ?",
                (f"grp:{r['brand']}:{r['model_id']}:%",))
            nome = self.one("SELECT name FROM model WHERE brand=? AND row_id=?",
                            (r["brand"], r["model_id"]))
            out.append({
                "brand": r["brand"], "model_id": r["model_id"],
                "family_id": r["family_id"],
                "nome": nome["name"] if nome else None,
                "n_tavole": n_tavole["n"] if n_tavole else 0,
                "n_gruppi_completi": n_gruppi["n"] if n_gruppi else 0,
            })
        return out
