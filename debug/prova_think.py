"""Verifica se l'endpoint /v1 di Ollama accetta di spegnere il ragionamento.

Uso:  ./venv/Scripts/python.exe debug/prova_think.py [modello]
Discriminante: completion_tokens. Con il ragionamento acceso sono centinaia
di token e decine di secondi; spento, poche decine di token.
"""
import sys
import time

from openai import OpenAI

MODELLO = sys.argv[1] if len(sys.argv) > 1 else "qwen3.5:4b"
client = OpenAI(base_url="http://localhost:11434/v1", api_key="ollama", timeout=240)

VARIANTI = [
    ("extra_body think=False", {"extra_body": {"think": False}}),
    ("reasoning_effort=none", {"reasoning_effort": "none"}),
    ("nessun parametro (base)", {}),
]

for nome, extra in VARIANTI:
    t0 = time.time()
    try:
        r = client.chat.completions.create(
            model=MODELLO,
            messages=[{"role": "user", "content": "ciao"}],
            **extra)
    except Exception as e:
        print(f"{nome}: ERRORE {e!r}\n")
        continue
    dt = time.time() - t0
    print(f"{nome}: {dt:.1f}s, token generati = {r.usage.completion_tokens}")
    print("  risposta:", (r.choices[0].message.content or "")[:100].replace("\n", " "), "\n")
