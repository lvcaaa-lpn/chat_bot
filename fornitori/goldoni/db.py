"""
Archivio locale del catalogo Goldoni (SQLite + ricerca full-text).

Perche' un database locale: le pagine del catalogo sono HTML statico che
cambia raramente (le tavole portano date dal 2002 al 2014). Scaricandole
una volta, ogni ricerca successiva e' istantanea e non tocca il portale.
"""

import json
import re
import sqlite3
from pathlib import Path

import config
DB = str(config.DATI / "goldoni.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS cataloghi (
    codice        TEXT PRIMARY KEY,
    serie         TEXT,
    pubblicazione TEXT,
    validita_da   TEXT,
    validita_a    TEXT,
    modelli       TEXT,          -- JSON
    descrizione   TEXT,
    url           TEXT,
    scaricato_il  TEXT           -- NULL = non ancora scaricato
);

CREATE TABLE IF NOT EXISTS tavole (
    id             INTEGER PRIMARY KEY,
    catalogo       TEXT NOT NULL,
    tavola         TEXT NOT NULL,
    gruppo         TEXT,
    numero         TEXT,
    titolo         TEXT,
    macchina       TEXT,
    modelli_tavola TEXT,
    data           TEXT,
    immagine       TEXT,
    validita_da    TEXT,
    validita_a     TEXT,
    note           TEXT,         -- JSON {"1": "MOD. 65 - 75", ...}
    url            TEXT,
    UNIQUE (catalogo, tavola)
);

CREATE TABLE IF NOT EXISTS righe (
    id          INTEGER PRIMARY KEY,
    tavola_id   INTEGER NOT NULL REFERENCES tavole(id) ON DELETE CASCADE,
    posizione   TEXT,
    codice      TEXT NOT NULL,
    descrizione TEXT,
    note        TEXT             -- JSON ["2","4"]
);

CREATE INDEX IF NOT EXISTS idx_righe_codice ON righe(codice);
CREATE INDEX IF NOT EXISTS idx_tavole_cat   ON tavole(catalogo);

-- indice full-text sulle descrizioni: e' cio' che rende inutile
-- "navigare" gruppo per gruppo. Si cerca il testo, la tavola arriva
-- nel risultato invece di doverla indovinare prima.
CREATE VIRTUAL TABLE IF NOT EXISTS righe_fts USING fts5(
    descrizione,
    content='righe',
    content_rowid='id',
    tokenize='unicode61'
);

CREATE TRIGGER IF NOT EXISTS righe_ai AFTER INSERT ON righe BEGIN
    INSERT INTO righe_fts(rowid, descrizione) VALUES (new.id, new.descrizione);
END;
CREATE TRIGGER IF NOT EXISTS righe_ad AFTER DELETE ON righe BEGIN
    INSERT INTO righe_fts(righe_fts, rowid, descrizione)
    VALUES ('delete', old.id, old.descrizione);
END;
"""


def apri(path=DB):
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    # WAL: ogni sessione apre una propria connessione (vedi server.py); in
    # modalita' rollback-journal predefinita una scrittura (es. il crawl che
    # salva una tavola) blocca temporaneamente le altre connessioni in lettura.
    con.execute("PRAGMA journal_mode = WAL")
    con.executescript(SCHEMA)
    return con


# -------------------------------------------------------------------
# Matricole: "C556210" -> ordinabile come ("C", 556210)
# -------------------------------------------------------------------
RE_MAT = re.compile(r'^\s*([A-Z])\s*(\d{4,7})\s*$', re.I)


def chiave_matricola(m):
    """Restituisce una chiave confrontabile, o None se non riconosciuta."""
    if not m:
        return None
    x = RE_MAT.match(str(m).replace(" ", ""))
    return (x.group(1).upper(), int(x.group(2))) if x else None


def dentro_intervallo(matricola, da, a):
    """
    True se la matricola cade nell'intervallo [da, a].
    Estremi vuoti = aperti. Matricola non riconoscibile = non filtra
    (meglio mostrare tutto che nascondere il pezzo giusto).
    """
    k = chiave_matricola(matricola)
    if k is None:
        return True
    kda, ka = chiave_matricola(da), chiave_matricola(a)
    if kda and k < kda:
        return False
    if ka and k > ka:
        return False
    return True

def chiave_tavola(t):
    """
    Ordina le tavole in entrambi i formati Goldoni.
    'gruppo' e 'numero' sono testo: '21'/'620' (vecchio),
    '40'/'08_E_01' (nuovo). Confronto pezzo per pezzo, numeri
    come numeri e lettere come lettere, cosi' 02_C viene dopo 02_B
    e 10 dopo 9 invece che dopo 1.
    """
    def pezzi(s):
        out = []
        for p in re.split(r'[_\-.\s]+', str(s or "")):
            if not p:
                continue
            out.append((0, int(p), "") if p.isdigit() else (1, 0, p.upper()))
        return out
    return pezzi(t["gruppo"]) + pezzi(t["numero"])

# -------------------------------------------------------------------
# Scrittura
# -------------------------------------------------------------------
def salva_cataloghi(con, cataloghi):
    for c in cataloghi:
        con.execute("""
            INSERT INTO cataloghi (codice, serie, pubblicazione, validita_da,
                                   validita_a, modelli, descrizione, url)
            VALUES (?,?,?,?,?,?,?,?)
            ON CONFLICT(codice) DO UPDATE SET
                serie=excluded.serie, pubblicazione=excluded.pubblicazione,
                validita_da=excluded.validita_da, validita_a=excluded.validita_a,
                modelli=excluded.modelli, descrizione=excluded.descrizione,
                url=excluded.url
        """, (c["codice"], c["serie"], c["pubblicazione"], c["validita_da"],
              c["validita_a"], json.dumps(c["modelli"], ensure_ascii=False),
              c["descrizione"], c["url"]))
    con.commit()


def salva_tavola(con, catalogo, t, url=""):
    """Inserisce o rimpiazza una tavola con tutte le sue righe."""
    cur = con.execute("SELECT id FROM tavole WHERE catalogo=? AND tavola=?",
                      (catalogo, t.tavola))
    vecchia = cur.fetchone()
    if vecchia:
        con.execute("DELETE FROM righe WHERE tavola_id=?", (vecchia["id"],))
        con.execute("DELETE FROM tavole WHERE id=?", (vecchia["id"],))

    #print(f"[DEBUG] Salvo tavola {t.titolo} in {catalogo["codice"]}")

    cur = con.execute("""
        INSERT INTO tavole (catalogo, tavola, gruppo, numero, titolo, macchina,
                            modelli_tavola, data, immagine, validita_da,
                            validita_a, note, url)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (catalogo, t.tavola, t.gruppo, t.numero, t.titolo, t.macchina,
          t.modelli_tavola, t.data, t.immagine, t.validita_da, t.validita_a,
          json.dumps(t.note, ensure_ascii=False), url))
    tid = cur.lastrowid

    con.executemany("""
        INSERT INTO righe (tavola_id, posizione, codice, descrizione, note)
        VALUES (?,?,?,?,?)
    """, [(tid, r.posizione, r.codice, r.descrizione,
           json.dumps(r.note)) for r in t.righe])
    return tid


def segna_scaricato(con, codice, quando):
    con.execute("UPDATE cataloghi SET scaricato_il=? WHERE codice=?",
                (quando, codice))
    con.commit()


# -------------------------------------------------------------------
# Lettura
# -------------------------------------------------------------------
def trova_cataloghi(con, modello=None, matricola=None, testo=None):
    """
    Ricerca incrociata: modello + matricola + testo libero.
    E' il primo passo: individuare QUALE catalogo riguarda la macchina.
    """
    righe = con.execute("SELECT * FROM cataloghi ORDER BY serie").fetchall()
    out = []
    for r in righe:
        modelli = json.loads(r["modelli"] or "[]")
        punteggio = 0

        if modello:
            import re
            token = [t for t in re.split(r'\W+', modello.upper()) if t]
            desc = (r["descrizione"] or "").upper()
            mod_up = [x.upper() for x in modelli]

            for t in token:
                if t in mod_up:
                    punteggio += 10      # match su un modello: segnale forte
                elif t in desc:
                    punteggio += 1       # match sulla descrizione: debole
            if punteggio == 0:
                continue

        if matricola and not dentro_intervallo(matricola,
                                               r["validita_da"], r["validita_a"]):
            continue

        if testo and testo.lower() not in (r["descrizione"] or "").lower():
            continue

        d = dict(r)
        d["modelli"] = modelli
        d["punteggio"] = punteggio
        out.append(d)

    out.sort(key=lambda d: -d["punteggio"])
    return out


def _fts_query(testo):
    """Ogni parola diventa un prefisso: 'tubo mand' -> 'tubo* mand*'."""
    parole = re.findall(r'\w+', testo, re.UNICODE)
    return " ".join(p + "*" for p in parole if p)


def cerca_ricambi(con, testo, catalogo=None, matricola=None,
                  modello=None, limite=40):
    """
    Cerca nelle descrizioni di tutte le tavole scaricate.
    Non serve sapere in quale gruppo sta il pezzo: il gruppo e la
    tavola compaiono NEL RISULTATO.
    """
    sql = """
        SELECT r.posizione, r.codice, r.descrizione, r.note,
               t.catalogo, t.tavola, t.gruppo, t.numero, t.titolo,
               t.note AS legenda, t.validita_da, t.validita_a,
               t.immagine, t.data
        FROM righe_fts f
        JOIN righe  r ON r.id = f.rowid
        JOIN tavole t ON t.id = r.tavola_id
        WHERE righe_fts MATCH ?
    """
    par = [_fts_query(testo)]
    if catalogo:
        sql += " AND t.catalogo = ?"
        par.append(catalogo)
    sql += " ORDER BY rank LIMIT ?"
    par.append(limite * 4)          # margine per i filtri applicati dopo

    out = []
    for r in con.execute(sql, par):
        d = dict(r)
        d["note"] = json.loads(d["note"] or "[]")
        d["legenda"] = json.loads(d["legenda"] or "{}")

        # filtro per validita' della tavola
        if matricola and not dentro_intervallo(matricola,
                                               d["validita_da"], d["validita_a"]):
            continue

        # filtro per modello, leggendo le note: "(2)" -> "MOD. 85 - 85F"
        d["applicabile_a"] = _modelli_riga(d)
        if modello and d["applicabile_a"]:
            if not _modello_compatibile(modello, d["applicabile_a"]):
                continue

        out.append(d)
        if len(out) >= limite:
            break
    return out


RE_MOD_NOTA = re.compile(r'MOD\.\s*(.+)', re.I)


def _modelli_riga(d):
    """Dai marcatori (n) della riga ricava i modelli citati nella legenda."""
    modelli = []
    for n in d["note"]:
        testo = d["legenda"].get(n, "")
        m = RE_MOD_NOTA.search(testo)
        if m:
            modelli += [x.strip() for x in re.split(r'[-,/]', m.group(1))
                        if x.strip()]
    # a volte il modello e' scritto direttamente nella descrizione
    m = RE_MOD_NOTA.search(d["descrizione"] or "")
    if m:
        modelli += [x.strip() for x in re.split(r'[-,/]', m.group(1))
                    if x.strip()]
    return sorted(set(modelli))


def _modello_compatibile(modello, elenco):
    m = modello.strip().upper()
    return any(m == x.upper() for x in elenco)


def url_esploso(catalogo, tavola):
    """
    L'immagine dell'esploso segue uno schema fisso: sq{tavola}.gif
    (es. tavola 21340 -> sq21340.gif). Non serve estrarla dall'HTML.
    """
    from urllib.parse import quote
    return (f"https://goldoni-ricambi.studioilgranello.com/cat/"
            f"{quote(catalogo)}/dis/sq{tavola}.gif")


def raggruppa(risultati):
    """
    Riorganizza i risultati per tavola e segnala l'ambiguita'.

    L'ambiguita' e' l'informazione piu' importante da restituire: quando
    piu' tavole con lo STESSO TITOLO sopravvivono ai filtri, significa
    che esistono varianti che i dati testuali non sanno distinguere
    (allestimento, misura, cabina...). Va detto, non nascosto.
    """
    per_tavola = {}
    for r in risultati:
        k = (r["catalogo"], r["tavola"])
        if k not in per_tavola:
            per_tavola[k] = {
                "catalogo": r["catalogo"], "tavola": r["tavola"],
                "gruppo": r["gruppo"], "numero": r["numero"],
                "titolo": (r["titolo"] or "").split("/")[0].strip(),
                "validita_da": r["validita_da"], "validita_a": r["validita_a"],
                #"esploso": url_esploso(r["catalogo"], r["tavola"]),
                "righe": [],
            }
        per_tavola[k]["righe"].append({
            "posizione": r["posizione"], "codice": r["codice"],
            "descrizione": r["descrizione"],
            "applicabile_a": r.get("applicabile_a") or [],
        })

    tavole = sorted(per_tavola.values(),
                    key=chiave_tavola)

    # titoli che compaiono in piu' tavole = varianti in competizione
    conteggio = {}
    for t in tavole:
        conteggio[t["titolo"]] = conteggio.get(t["titolo"], 0) + 1
    varianti = {k: v for k, v in conteggio.items() if v > 1}

    codici = {r["codice"] for t in tavole for r in t["righe"]}

    return {
        "tavole": tavole,
        "totale_codici": len(codici),
        "ambiguo": bool(varianti),
        "varianti": [{"titolo": k, "quante": v} for k, v in varianti.items()],
    }


def statistiche(con):
    q = lambda s: con.execute(s).fetchone()[0]
    return {
        "cataloghi": q("SELECT COUNT(*) FROM cataloghi"),
        "scaricati": q("SELECT COUNT(*) FROM cataloghi WHERE scaricato_il IS NOT NULL"),
        "tavole": q("SELECT COUNT(*) FROM tavole"),
        "righe": q("SELECT COUNT(*) FROM righe"),
    }
