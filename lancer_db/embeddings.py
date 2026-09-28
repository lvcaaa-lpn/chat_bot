"""
Ricerca semantica sul catalogo LANCER.

Si vettorizza un testo per ogni "tipo" di ricambio (tabella `tipi`,
~480 righe, non ~1760 codici: i codici dello stesso tipo hanno lo
stesso significato e cambiano solo misure/modello, che si filtrano
dopo). Alla ricerca si vettorizza la frase del cliente e si prendono
i tipi piu' vicini (similarita' coseno), poi i codici di quei tipi,
eventualmente filtrati per serie/modello.

Due varianti di testo, per poterle confrontare (vedi valuta.py):
  'base:'   solo la descrizione inglese del PDF (com'e' oggi il bot)
  'ricco:'  descrizione + nome italiano, funzione, sinonimi, siciliano

Il modello di embedding e' solo un "traduttore in vettori": non genera
contenuti. Gira sui server del fornitore (API hosted): niente modelli
locali da installare. Backend intercambiabili (i vettori sono salvati
per backend, chiave "<backend>|ricco:<tipo>"):
  voyage  Voyage AI (VOYAGE_API_KEY in .env o dati/voyage_key.txt)  [default]
          modello in VOYAGE_MODELLO, distingue domanda/documento
  gemini  gemini-embedding-001 (chiave gia' in config.py); piano gratuito
          limitato: 100 testi/minuto e ~1000/giorno

Uso:
  ./venv/Scripts/python.exe lancer_db/embeddings.py [--backend voyage]      # calcola i mancanti
  ./venv/Scripts/python.exe lancer_db/embeddings.py "pistone del rullo" [--serie PM] [--modello "PM 300"]
"""
import argparse
import os
import sys
import time
from pathlib import Path

import numpy as np
import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config  # noqa: E402

from db import connetti  # noqa: E402

sys.stdout.reconfigure(encoding="utf-8")

BACKEND_DEFAULT = "voyage"
VOYAGE_MODELLO = os.environ.get("VOYAGE_MODELLO", "voyage-3.5")
VOYAGE_URL = "https://api.voyageai.com/v1/embeddings"


def _voyage(testi, domanda):
    chiave = config.leggi_credenziale("voyage_key.txt", "VOYAGE_API_KEY")
    if not chiave:
        raise RuntimeError("manca la chiave Voyage: VOYAGE_API_KEY in .env o dati/voyage_key.txt")
    out = []
    for i in range(0, len(testi), 128):
        for tentativo in range(5):
            r = requests.post(VOYAGE_URL, timeout=60,
                              headers={"Authorization": f"Bearer {chiave}"},
                              json={"input": testi[i:i + 128], "model": VOYAGE_MODELLO,
                                    "input_type": "query" if domanda else "document"})
            if r.status_code != 429:
                break
            time.sleep(20 * (tentativo + 1))       # limite di frequenza: aspetta e riprova
        if r.status_code != 200:
            raise RuntimeError(f"Voyage {r.status_code}: {r.text[:300]}")
        dati = sorted(r.json()["data"], key=lambda d: d["index"])
        out.extend(np.array(d["embedding"], dtype=np.float32) for d in dati)
    return out


def _gemini(testi):
    """Piano gratuito: 100 testi/minuto. Lotti da 90; sul 429 si aspetta e si riprova."""
    from openai import OpenAI, RateLimitError
    p = config.LLM["gemini"]
    client = OpenAI(base_url=p["base_url"], api_key=p["api_key"])
    out = []
    for i in range(0, len(testi), 90):
        for _ in range(5):
            try:
                r = client.embeddings.create(model="gemini-embedding-001",
                                             input=testi[i:i + 90], dimensions=768)
                break
            except RateLimitError:
                print("   limite di quota Gemini, attendo 65s", flush=True)
                time.sleep(65)
        else:
            raise RuntimeError("quota embedding Gemini esaurita (limite giornaliero?)")
        out.extend(np.array(d.embedding, dtype=np.float32) for d in r.data)
    return out


def vettorizza(testi, backend=BACKEND_DEFAULT, domanda=False):
    """domanda=True per la frase del cliente (Voyage vettorizza in modo
    diverso domande e documenti, migliora la ricerca)."""
    v = _gemini(testi) if backend == "gemini" else _voyage(testi, domanda)
    return [x / np.linalg.norm(x) for x in v]


def testo_base(t):
    return f"{t['tipo_en']}. {t['esempio']}"


def testo_ricco(t):
    return (f"{t['nome_it']}. {t['funzione']}. Detto anche: {t['sinonimi']}"
            + (f"; {t['siciliano']}" if t["siciliano"] else "")
            + f". Categoria: {t['categoria']}. ({t['tipo_en']}; {t['esempio']})")


def calcola(con, backend=BACKEND_DEFAULT, rifai=False):
    tipi = con.execute("SELECT * FROM tipi WHERE nome_it IS NOT NULL").fetchall()
    da_fare = []
    for t in tipi:
        for pref, f in (("base:", testo_base), ("ricco:", testo_ricco)):
            chiave, testo = f"{backend}|{pref}{t['tipo_en']}", f(t)
            vecchio = con.execute("SELECT testo FROM embeddings WHERE chiave=?", (chiave,)).fetchone()
            if rifai or not vecchio or vecchio[0] != testo:   # ricalcola se l'arricchimento e' cambiato
                da_fare.append((chiave, testo))
    print(f"[{backend}] vettori da calcolare: {len(da_fare)}")
    for i in range(0, len(da_fare), 90):       # salva lotto per lotto: se si interrompe
        lotto = da_fare[i:i + 90]              # il lavoro fatto resta e si riprende da li'
        for (chiave, testo), v in zip(lotto, vettorizza([t for _, t in lotto], backend)):
            con.execute("INSERT OR REPLACE INTO embeddings VALUES (?,?,?,?)",
                        (chiave, testo, backend, v.tobytes()))
        con.commit()
        print(f"   salvati {min(i + 90, len(da_fare))}/{len(da_fare)}", flush=True)


def carica(con, prefisso, backend=BACKEND_DEFAULT):
    """(chiavi, matrice) dei vettori '<backend>|<prefisso>...'"""
    p = f"{backend}|{prefisso}"
    righe = con.execute("SELECT chiave, vettore FROM embeddings WHERE chiave LIKE ?",
                        (p + "%",)).fetchall()
    if not righe:
        return [], np.zeros((0, 1), dtype=np.float32)
    chiavi = [r[0][len(p):] for r in righe]
    matrice = np.vstack([np.frombuffer(r[1], dtype=np.float32) for r in righe])
    return chiavi, matrice


def tipi_simili(con, domanda, k=10, backend=BACKEND_DEFAULT):
    """[(tipo_en, punteggio)] dei k tipi piu' vicini alla domanda."""
    chiavi, m = carica(con, "ricco:", backend)
    q = vettorizza([domanda], backend, domanda=True)[0]
    s = m @ q
    return [(chiavi[i], float(s[i])) for i in np.argsort(-s)[:k]]


def cerca(con, domanda, serie=None, modello=None, k=5, backend=BACKEND_DEFAULT):
    """Codici candidati per la frase del cliente, raggruppati per tipo."""
    out = []
    for tipo, punteggio in tipi_simili(con, domanda, k=k, backend=backend):
        q = ("SELECT DISTINCT k.codice, k.descrizione_it, s.sigla, t.titolo, t.pagina_tabella, r.posizione "
             "FROM ricambi k JOIN righe r ON r.codice = k.codice JOIN tavole t ON t.id = r.tavola_id "
             "JOIN serie s ON s.id = t.serie_id WHERE k.tipo_en = ?")
        par = [tipo]
        if serie:
            q += " AND s.sigla = ?"
            par.append(serie.upper())
        if modello:
            # righe senza modelli = valgono per tutta la serie
            q += (" AND (NOT EXISTS (SELECT 1 FROM righe_modelli m WHERE m.riga_id = r.id) "
                  "OR EXISTS (SELECT 1 FROM righe_modelli m WHERE m.riga_id = r.id AND m.modello = ?))")
            par.append(modello.upper())
        codici = con.execute(q, par).fetchall()
        if codici:
            out.append((tipo, punteggio, codici))
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("domanda", nargs="?")
    ap.add_argument("--serie")
    ap.add_argument("--modello")
    ap.add_argument("--backend", default=BACKEND_DEFAULT, choices=["voyage", "gemini"])
    ap.add_argument("--rifai", action="store_true")
    a = ap.parse_args()
    con = connetti()
    if not a.domanda:
        calcola(con, a.backend, a.rifai)
    else:
        for tipo, p, codici in cerca(con, a.domanda, a.serie, a.modello, backend=a.backend):
            print(f"\n[{p:.3f}] {tipo}")
            for c in codici[:6]:
                print(f"    {c[0]}  {c[1]}   ({c[2]}, '{c[3]}' pag {c[4]}, pos {c[5]})")
            if len(codici) > 6:
                print(f"    ... altri {len(codici) - 6}")
