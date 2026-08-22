"""
Assistente ricambi — interfaccia da terminale.

Sottile: tutta la logica sta in agente.py, condivisa con server.py.

    set LLM_PROVIDER=gemini
    set GEMINI_API_KEY=...
    python bot.py
"""

import config
from logconf import configura
configura()

from agente import Conversazione, crea_llm
from fornitori import carica_tutti


def main():
    print("Carico i fornitori:")
    registro = carica_tutti(verbose=True)
    if not registro.fornitori:
        raise SystemExit("Nessun fornitore caricato.")

    if not any(f.disponibile() for f in registro.fornitori.values()):
        print("\nATTENZIONE: nessun fornitore utilizzabile in questo momento.")

    llm, modello = crea_llm()
    conv = Conversazione(registro)

    print(f"\nProvider: {config.PROVIDER} ({modello})")
    print("Scrivi 'esci' per terminare.\n")

    log = (lambda n, a: print(f"  [{n}({a})]")) if config.DEBUG else None

    while True:
        try:
            testo = input("Cliente: ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if testo.lower() in ("esci", "exit", "quit"):
            break
        if not testo:
            continue

        esito = conv.turno(llm, modello, testo, su_strumento=log)
        if esito["errore"]:
            print(f"\n[errore] {esito.get('dettaglio', '')}")
        print(f"\nAssistente: {esito['risposta']}\n")


if __name__ == "__main__":
    main()
