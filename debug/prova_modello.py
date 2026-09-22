import config
from openai import OpenAI

client = OpenAI(base_url=config.LLM[config.PROVIDER]["base_url"], api_key=config.LLM[config.PROVIDER]["api_key"])
modello = config.LLM[config.PROVIDER]["model"]

messaggi = [{"role": "system", "content": "Rispondi sempre in italiano."}]

while True:
    msg = input("> ")
    if msg.strip().lower() in ("exit", "quit"):
        break
    messaggi.append({"role": "user", "content": msg})

    stream = client.chat.completions.create(
        model=modello, messages=messaggi, stream=True, **config.LLM[config.PROVIDER].get("params", {})
    )
    risposta = ""
    stava_ragionando = False
    for chunk in stream:
        if not chunk.choices:
            continue
        delta = chunk.choices[0].delta

        # Ollama usa "reasoning", altri provider "reasoning_content"
        pensiero = getattr(delta, "reasoning", None) or getattr(delta, "reasoning_content", None)
        if pensiero:
            print(f"\033[90m{pensiero}\033[0m", end="", flush=True)
            stava_ragionando = True
            continue

        testo = delta.content or ""
        if testo and stava_ragionando:
            print("\n")
            stava_ragionando = False
        print(testo, end="", flush=True)
        risposta += testo
    print()
    messaggi.append({"role": "assistant", "content": risposta})