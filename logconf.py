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


def configura(livello=None):
    livello = livello or os.environ.get("LOG_LEVEL", "INFO").upper()

    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-7s [%(name)s] %(message)s",
        datefmt="%H:%M:%S")

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(fmt)

    root = logging.getLogger()
    # evita handler duplicati quando uvicorn --reload ricarica il modulo
    for h in list(root.handlers):
        root.removeHandler(h)
    root.addHandler(handler)
    root.setLevel(livello)

    # i nostri logger applicativi
    for nome in ("sdf", "fornitori", "agente"):
        logging.getLogger(nome).setLevel(livello)

    # abbassa il rumore delle librerie
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("openai").setLevel(logging.WARNING)

    return root
