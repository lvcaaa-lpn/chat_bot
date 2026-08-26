"""Configurazione del logging condivisa tra bot.py e server.py.

Perche' esiste: sotto uvicorn i print() dei moduli spesso non si vedono
(stdout bufferizzato quando non e' un terminale vero, reloader in
sottoprocesso, output mescolato con quello di uvicorn). Il modulo logging
invece passa dagli handler che uvicorn ha gia' configurato, quindi i
messaggi compaiono sempre e con timestamp.

Uso in server.py, PRIMA di importare gli altri moduli:

    from logconf import configura
    configura()

Poi in qualunque modulo:

    import logging
    log = logging.getLogger("sdf")
    log.info("catalogo pronto: %s righe", n)
"""

import logging
import os
import sys
from logging.handlers import RotatingFileHandler

import config

CARTELLA_LOG = config.DATI / "log"


def configura(livello=None):
    livello = livello or os.environ.get("LOG_LEVEL", "INFO").upper()

    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-7s [%(name)s] %(message)s",
        datefmt="%H:%M:%S")

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(fmt)

    # oltre alla console: un file su disco che sopravvive a restart/reload
    # e non scorre via nella scrollback del terminale. E' il log "tecnico"
    # (richieste/risposte ai portali fornitore, timing, errori) - non le
    # conversazioni con i clienti, quelle sono in cronologia.py.
    CARTELLA_LOG.mkdir(exist_ok=True)
    file_handler = RotatingFileHandler(
        CARTELLA_LOG / "tecnico.log", maxBytes=5_000_000, backupCount=3,
        encoding="utf-8")
    file_handler.setFormatter(fmt)

    root = logging.getLogger()
    # evita handler duplicati quando uvicorn --reload ricarica il modulo
    for h in list(root.handlers):
        root.removeHandler(h)
    root.addHandler(handler)
    root.addHandler(file_handler)
    root.setLevel(livello)

    # i nostri logger applicativi
    for nome in ("sdf", "goldoni", "fornitori", "agente", "cronologia"):
        logging.getLogger(nome).setLevel(livello)

    # abbassa il rumore delle librerie
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("openai").setLevel(logging.WARNING)

    return root
