"""
Legge dati/conversazioni.db (scritto da cronologia.py durante le chat reali)
e la stampa in modo leggibile. Serve per rivedere a occhio le conversazioni
avvenute, in vista della futura tabella di apprendimento sui termini
imprecisi (vedi CLAUDE.md). Non fa parte del bot, non va importato altrove.

Uso:
    py debug/esporta_conversazioni.py                  elenco sessioni
    py debug/esporta_conversazioni.py <sessione_id>     trascrizione completa
    py debug/esporta_conversazioni.py <sessione_id> --csv > out.csv
"""
import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cronologia


def elenco():
    righe = cronologia.sessioni()
    if not righe:
        print("Nessuna conversazione registrata.")
        return
    print(f"{'sessione':<38} {'messaggi':>8}  ultimo")
    for r in righe:
        print(f"{r['sessione_id']:<38} {r['n_messaggi']:>8}  {r['ultimo']}")


def trascrizione(sessione_id, come_csv=False):
    righe = cronologia.leggi_sessione(sessione_id)
    if not righe:
        print(f"Nessun messaggio per la sessione '{sessione_id}'.")
        return

    if come_csv:
        w = csv.writer(sys.stdout)
        w.writerow(["timestamp", "ruolo", "testo", "strumento_nome",
                   "strumento_argomenti", "strumento_esito"])
        for r in righe:
            w.writerow([r["timestamp"], r["ruolo"], r["testo"] or "",
                       r["strumento_nome"] or "", r["strumento_argomenti"] or "",
                       r["strumento_esito"] or ""])
        return

    for r in righe:
        if r["ruolo"] == "strumento":
            print(f"  [{r['timestamp']}] {r['strumento_nome']}"
                  f"({r['strumento_argomenti']}) -> {r['strumento_esito']}")
        else:
            print(f"[{r['timestamp']}] {r['ruolo']}: {r['testo']}")


if __name__ == "__main__":
    args = sys.argv[1:]
    if not args:
        elenco()
    else:
        trascrizione(args[0], come_csv="--csv" in args)
