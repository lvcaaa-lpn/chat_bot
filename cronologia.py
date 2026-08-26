"""
Registro delle conversazioni con i clienti (non il log tecnico: quello e'
in dati/log/tecnico.log, vedi logconf.py).

Perche' esiste: e' il materiale grezzo su cui costruire in futuro la
tabella di apprendimento sui termini imprecisi (vedi CLAUDE.md, sezione
"Idea: tabella di apprendimento sui termini imprecisi") - serve prima
avere le conversazioni reali registrate in modo ordinato, la tabella
curata verra' dopo, come modulo separato.

Vive alla radice del progetto (non dentro fornitori/<marca>/) perche' non
sa nulla di specifico di marca: registra messaggi e chiamate di strumento
per nome/argomenti/esito, che sono gia' generici (l'informazione di marca,
quando c'e', e' dentro gli argomenti/esito stessi, non nel codice qui).

Uso, da agente.py:

    import cronologia
    cronologia.registra_messaggio(sessione_id, "cliente", testo)
    cronologia.registra_strumento(sessione_id, nome, argomenti, esito)
    cronologia.registra_messaggio(sessione_id, "bot", risposta)
"""

import json
import logging
import sqlite3

import config

DB = str(config.DATI / "conversazioni.db")

log = logging.getLogger("cronologia")

SCHEMA = """
CREATE TABLE IF NOT EXISTS messaggi (
    id                 INTEGER PRIMARY KEY,
    sessione_id        TEXT NOT NULL,
    timestamp          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now', 'localtime')),
    ruolo              TEXT NOT NULL,   -- 'cliente' | 'bot' | 'strumento'
    testo              TEXT,
    strumento_nome     TEXT,
    strumento_argomenti TEXT,           -- JSON
    strumento_esito    TEXT             -- JSON
);
CREATE INDEX IF NOT EXISTS idx_messaggi_sessione ON messaggi(sessione_id, id);
"""


def _apri():
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    # connessione aperta e richiusa ad ogni scrittura (vedi sotto): piu'
    # sessioni/thread concorrenti scrivono senza condividere una connessione
    con.execute("PRAGMA journal_mode = WAL")
    con.executescript(SCHEMA)
    return con


def _scrivi(**riga):
    try:
        con = _apri()
        try:
            con.execute(
                "INSERT INTO messaggi (sessione_id, ruolo, testo, strumento_nome, "
                "strumento_argomenti, strumento_esito) VALUES "
                "(:sessione_id, :ruolo, :testo, :strumento_nome, "
                ":strumento_argomenti, :strumento_esito)",
                {"testo": None, "strumento_nome": None,
                 "strumento_argomenti": None, "strumento_esito": None, **riga})
            con.commit()
        finally:
            con.close()
    except Exception:
        # una conversazione non deve mai fallire per colpa del logging
        log.exception("scrittura cronologia fallita")


def registra_messaggio(sessione_id, ruolo, testo):
    """ruolo: 'cliente' o 'bot'."""
    _scrivi(sessione_id=sessione_id, ruolo=ruolo, testo=testo)


def registra_strumento(sessione_id, nome, argomenti, esito):
    _scrivi(sessione_id=sessione_id, ruolo="strumento",
           strumento_nome=nome,
           strumento_argomenti=json.dumps(argomenti, ensure_ascii=False),
           strumento_esito=json.dumps(esito, ensure_ascii=False))


def sessioni(con=None):
    """Elenco sessioni con conteggio messaggi e ultimo timestamp, piu' recenti prima."""
    con = con or _apri()
    return con.execute(
        "SELECT sessione_id, COUNT(*) AS n_messaggi, MAX(timestamp) AS ultimo "
        "FROM messaggi GROUP BY sessione_id ORDER BY ultimo DESC").fetchall()


def leggi_sessione(sessione_id, con=None):
    """Tutti i messaggi di una sessione, in ordine cronologico."""
    con = con or _apri()
    return con.execute(
        "SELECT * FROM messaggi WHERE sessione_id = ? ORDER BY id",
        (sessione_id,)).fetchall()
