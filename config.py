"""
Percorsi e impostazioni comuni.

Tutti i percorsi sono calcolati a partire dalla posizione di QUESTO file,
non dalla cartella da cui lanci il comando. Cosi' 'python bot.py' e
'python fornitori/goldoni/cli.py' trovano gli stessi dati.
"""

import os
from dotenv import load_dotenv
from pathlib import Path

load_dotenv()

RADICE = Path(__file__).resolve().parent
DATI = RADICE / "dati"
DATI.mkdir(exist_ok=True)

# --- LLM -------------------------------------------------------------
PROVIDER = os.environ.get("LLM_PROVIDER", "gemini") 
DEBUG = os.environ.get("LLM_DEBUG", "0") == "1"

LLM = {
    "gemini": {"base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
               "api_key": os.environ.get("GEMINI_API_KEY", ""),
               "model": "gemini-3.5-flash-lite"}, # gemini-3.6-flash
    "groq": {"base_url": "https://api.groq.com/openai/v1",
             "api_key": os.environ.get("GROQ_API_KEY", ""),
             "model": "llama-3.3-70b-versatile"},
    "ollama": {"base_url": "http://localhost:11434/v1",
               "api_key": "ollama", "model": "qwen2.5:7b"},
}


def leggi_credenziale(nome_file, variabile_ambiente):
    """Una credenziale di fornitore: prima la variabile d'ambiente, poi il
    file in dati/. Ogni fornitore la richiama con il proprio nome file e
    la propria variabile, cosi' config.py non deve conoscere le marche."""
    v = os.environ.get(variabile_ambiente, "")
    if not v:
        percorso = DATI / nome_file
        if percorso.exists():
            v = percorso.read_text(encoding="utf-8").strip()
    return v


def scrivi_credenziale(nome_file, valore):
    """Salva in dati/ una credenziale ottenuta a runtime (es. il cookie di
    un login automatico), cosi' il prossimo avvio la riusa invece di
    rifare il login da capo. Stesso principio di leggi_credenziale:
    generico, ogni fornitore passa il proprio nome file."""
    (DATI / nome_file).write_text(valore, encoding="utf-8")

# --- server web ------------------------------------------------------
# In produzione elencare i domini reali del sito dell'azienda.
ORIGINI_CONSENTITE = os.environ.get(
    "ORIGINI_CONSENTITE", "*").split(",")