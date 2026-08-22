# Assistente ricambi multi-marca

## Struttura

    bot.py                    chatbot unificato
    config.py                 percorsi e impostazioni
    fornitori/
      __init__.py             registro + scoperta automatica
      base.py                 interfaccia comune
      goldoni/                catalogo statico scaricato in locale
      sdf/                    portale live (richiede cookie)
    dati/
      goldoni.db              archivio Goldoni
      cookie.txt              cookie sessione SDF

## Preparazione

    pip install requests beautifulsoup4 lxml
    pip install fastapi uvicorn openai
    pip install ollama

    python -m fornitori.goldoni.cli indice
    python -m fornitori.goldoni.cli scarica starqu

Per SDF: incollare il cookie del browser in dati/cookie.txt

## Avvio

    set LLM_PROVIDER=gemini
    set GEMINI_API_KEY=...
    python bot.py

Con set LLM_DEBUG=1 vengono mostrate le chiamate agli strumenti.

## Aggiungere una marca

Creare fornitori/<marca>/ con __init__.py che espone crea(),
e adapter.py con una classe che estende Fornitore.
Nessun altro file va modificato.

## Inserire widget in un sito web
Inserisci una riga prima di </body>:

<script src="https://tuo-server/widget/widget.js" data-api="https://tuo-server"></script>

HANNO DATO ERRORE - DA CONTROLLARE
5000
RONIN MR