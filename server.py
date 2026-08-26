"""
Server web dell'assistente ricambi.

    pip install fastapi uvicorn openai
    uvicorn server:app --reload --port 8000

Poi apri http://localhost:8000/demo

Il browser parla SOLO con questo server: chiavi API, cookie dei portali
e cataloghi non escono mai da qui.
"""

import time
import uuid
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import json
from concurrent.futures import ThreadPoolExecutor
 
from fastapi.responses import StreamingResponse
 

from logconf import configura
configura()

import config
from agente import Conversazione, crea_llm
from fornitori import carica_tutti

RADICE = Path(__file__).resolve().parent
WEB = RADICE / "web"

SESSIONE_TTL = 60 * 60          # un'ora di inattivita'
MAX_SESSIONI = 500              # tetto di sicurezza

ATTESA_MASSIMA = 15 * 60     # tetto di attesa per un catalogo, in secondi
PASSO = 1.5                  # ogni quanto interrogare l'avanzamento
 
 
def _evento(tipo, **dati):
    """Un frame Server-Sent Events."""
    return "data: " + json.dumps({"tipo": tipo, **dati}, ensure_ascii=False) + "\n\n"
 
 
def _stato_evento(registro):
    """Traduce lo stato del registro in un frame, o None se non c'e' attesa."""
    try:
        st = registro.stato_preparazione()
    except Exception:
        return None
    if st.get("stato") != "in_corso":
        return None
    return _evento("stato",
                   testo=st.get("etichetta") or "Preparo il catalogo",
                   percentuale=st.get("percentuale", 0),
                   dettaglio=st.get("gruppo") or "",
                   marca=st.get("marca") or "")
 
 
def _turno_con_stati(conv, registro, messaggio, log):
    """Esegue un turno in un thread emettendo stati finche' non finisce.
 
    Restituisce i frame da inviare; l'ultimo elemento e' la tupla
    ('__esito__', risultato) che il chiamante intercetta.
    """
    with ThreadPoolExecutor(max_workers=1) as ex:
        fut = ex.submit(conv.turno, llm, MODELLO, messaggio, log)
        while not fut.done():
            time.sleep(PASSO)
            ev = _stato_evento(registro)
            if ev:
                yield ev
        yield ("__esito__", fut.result())
 
 
def _flusso(sid, sess, messaggio):
    conv, registro = sess["conv"], sess["registro"]
    registro.codici_visti.clear()
 
    log = (lambda n, a: print(f"  [{sid[:6]}] {n}({a})")) if config.DEBUG else None
 
    yield _evento("stato", testo="Sto cercando", percentuale=0)
 
    esito = None
    for x in _turno_con_stati(conv, registro, messaggio, log):
        if isinstance(x, tuple):
            esito = x[1]
        else:
            yield x
 
    # un fornitore sta ancora preparando il catalogo: aspetto la fine e
    # rilancio la domanda, cosi' il cliente riceve la risposta vera invece
    # di un "riprova tra poco" che dovrebbe digitare di nuovo
    if registro.qualcuno_sta_lavorando():
        scadenza = time.time() + ATTESA_MASSIMA
        while registro.qualcuno_sta_lavorando() and time.time() < scadenza:
            ev = _stato_evento(registro)
            if ev:
                yield ev
            time.sleep(PASSO)
 
        if not registro.qualcuno_sta_lavorando():
            yield _evento("stato", testo="Catalogo pronto, cerco il ricambio",
                          percentuale=100)
            registro.codici_visti.clear()
            for x in _turno_con_stati(conv, registro, messaggio, log):
                if isinstance(x, tuple):
                    esito = x[1]
                else:
                    yield x
 
    yield _evento("risposta", sessione=sid,
                  testo=esito["risposta"] if esito else "",
                  errore=bool(esito and esito["errore"]),
                  codici=sorted(registro.codici_visti)) 

app = FastAPI(title="Assistente ricambi")

# In produzione: sostituire con il dominio del sito dell'azienda
app.add_middleware(CORSMiddleware,
                   allow_origins=config.ORIGINI_CONSENTITE,
                   allow_methods=["POST", "GET"], allow_headers=["*"])

llm, MODELLO = crea_llm()

# REGISTRO_BASE serve solo per /api/stato (diagnostica). Ogni sessione ha
# il proprio Registro/Fornitore (vedi conversazione()): NON e' uno spreco
# da evitare, e' necessario. FornitoreSdf/FornitoreGoldoni tengono "quale
# macchina ho scelto" come attributi di istanza (self.model_id, ecc.): se
# due sessioni condividessero lo stesso oggetto, un cliente potrebbe
# ritrovarsi a cercare ricambi sulla macchina scelta da un altro cliente.
# Le risorse davvero costose (crawl dei cataloghi, cache SQLite) sono gia'
# condivise correttamente a livello di modulo negli adapter, non qui.
REGISTRO_BASE = carica_tutti()

SESSIONI = {}


def pulisci():
    ora = time.time()
    scaduti = [k for k, v in SESSIONI.items() if ora - v["ultimo"] > SESSIONE_TTL]
    for k in scaduti:
        SESSIONI.pop(k, None)
    # se ne restano troppe, butto le piu' vecchie
    if len(SESSIONI) > MAX_SESSIONI:
        per_eta = sorted(SESSIONI.items(), key=lambda kv: kv[1]["ultimo"])
        for k, _ in per_eta[:len(SESSIONI) - MAX_SESSIONI]:
            SESSIONI.pop(k, None)


def conversazione(sid):
    pulisci()
    if sid not in SESSIONI:
        registro = carica_tutti()
        SESSIONI[sid] = {"conv": Conversazione(registro, sessione_id=sid),
                         "registro": registro,
                         "ultimo": time.time()}
    SESSIONI[sid]["ultimo"] = time.time()
    return SESSIONI[sid]


class Richiesta(BaseModel):
    messaggio: str
    sessione: str | None = None


@app.post("/api/chat")
def chat(req: Richiesta):
    sid = req.sessione or str(uuid.uuid4())
    sess = conversazione(sid)
    conv, registro = sess["conv"], sess["registro"]

    registro.codici_visti.clear()      # solo i codici di questo turno

    log = (lambda n, a: print(f"  [{sid[:6]}] {n}({a})")) if config.DEBUG else None
    esito = conv.turno(llm, MODELLO, req.messaggio, su_strumento=log)

    risposta = {"sessione": sid, "risposta": esito["risposta"],
                "errore": esito["errore"],
                "codici": sorted(registro.codici_visti)}
    if esito["errore"] and config.DEBUG:
        risposta["dettaglio"] = esito.get("dettaglio", "")
    return risposta


@app.post("/api/chat/stream")
def chat_stream(req: Richiesta):
    sid = req.sessione or str(uuid.uuid4())
    sess = conversazione(sid)
    return StreamingResponse(
        _flusso(sid, sess, req.messaggio),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            # senza questo nginx bufferizza e consegna tutti gli eventi
            # insieme alla fine, vanificando lo streaming
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/api/stato")
def stato():
    """Diagnostica: quali fornitori sono vivi?"""
    return {"provider": config.PROVIDER, "modello": MODELLO,
            "fornitori": REGISTRO_BASE.elenca()["marche"],
            "sessioni_attive": len(SESSIONI)}

@app.get("/api/preparazione")
def preparazione(sessione: str):
    """Avanzamento del download catalogo per la sessione indicata.

    Il widget lo interroga ogni pochi secondi mentre il catalogo si prepara,
    cosi' il cliente vede una barra invece di una pagina ferma. Nessuna marca
    e' cablata qui: il registro interroga tutti i fornitori caricati e
    restituisce quello con lavoro in corso, se c'e'.
    """
    sess = SESSIONI.get(sessione)
    if not sess:
        return {"stato": "sessione_sconosciuta", "percentuale": 0}

    return sess["registro"].stato_preparazione()

if WEB.exists():
    app.mount("/widget", StaticFiles(directory=str(WEB)), name="widget")

    @app.get("/demo")
    def demo():
        return FileResponse(WEB / "demo.html")
