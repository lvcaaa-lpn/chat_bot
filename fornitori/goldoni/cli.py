"""
Interfaccia a riga di comando per il catalogo Goldoni.

    python cli.py indice
    python cli.py cataloghi --matricola C556300
    python cli.py cataloghi --modello 75
    python cli.py scarica starqu
    python cli.py cerca "tubo mandata" --catalogo starqu
    python cli.py cerca "tubo mandata" --catalogo starqu --matricola C556300
    python cli.py stato
    python cli.py elenco
    python cli.py scarica_tutti
    
"""

import argparse
import sys

from fornitori.goldoni import db
from fornitori.goldoni.client import GoldoniClient


def c_indice(args, con):
    cli = GoldoniClient()
    print("Scarico l'indice dei cataloghi...")
    cat = cli.scarica_indice(con)
    print(f"{len(cat)} cataloghi HTML registrati.")


def c_cataloghi(args, con):
    trovati = db.trova_cataloghi(con, modello=args.modello,
                                 matricola=args.matricola, testo=args.testo)
    if not trovati:
        print("Nessun catalogo corrisponde.")
        print("Se l'indice e' vuoto, lancia prima:  python goldoni.py indice")
        return
    print(f"{len(trovati)} cataloghi:\n")
    for c in trovati:
        stato = "scaricato" if c["scaricato_il"] else "-"
        val = f"{c['validita_da'] or '...'} -> {c['validita_a'] or '...'}"
        print(f"  {c['codice']:<22} {c['serie'][:24]:<24} {val:<20} {stato}")
        if c["modelli"]:
            print(f"      modelli: {', '.join(c['modelli'][:12])}")


def c_scarica(args, con):
    cli = GoldoniClient(delay=args.delay)
    print(f"Scarico il catalogo '{args.codice}' (pausa {args.delay}s fra le pagine)")
    n = cli.scarica_catalogo(con, args.codice, max_pagine=args.max_pagine,
                             debug=args.debug)
    s = db.statistiche(con)
    print(f"\nFatto. In archivio: {s['tavole']} tavole, {s['righe']} righe.")


def c_cerca(args, con):
    ris = db.cerca_ricambi(con, args.testo, catalogo=args.catalogo,
                           matricola=args.matricola, modello=args.modello,
                           limite=args.limite)
    if not ris:
        print("Nessun risultato.")
        print("Verifica di aver scaricato il catalogo:  python goldoni.py stato")
        return

    print(f"{len(ris)} risultati per '{args.testo}'")
    if args.matricola:
        print(f"(filtrati per matricola {args.matricola})")
    print()

    tav_corrente = None
    for r in ris:
        chiave = (r["catalogo"], r["tavola"])
        if chiave != tav_corrente:
            tav_corrente = chiave
            val = ""
            if r["validita_da"] or r["validita_a"]:
                val = f"  [valido {r['validita_da'] or '...'} -> {r['validita_a'] or '...'}]"
            print(f"\n  GRUPPO {r['gruppo']} TAVOLA {r['numero']} — "
                  f"{r['titolo'].split('/')[0].strip()}{val}")

        note = ""
        if r["applicabile_a"]:
            note = "   applicabile a: " + ", ".join(r["applicabile_a"])
        elif r["note"]:
            testi = [r["legenda"].get(n, f"({n})") for n in r["note"]]
            note = "   nota: " + "; ".join(testi)

        print(f"    pos {r['posizione']:>3}  {r['codice']:<12} "
              f"{r['descrizione'][:38]:<38}{note}")


def c_stato(args, con):
    s = db.statistiche(con)
    print(f"cataloghi in indice : {s['cataloghi']}")
    print(f"cataloghi scaricati : {s['scaricati']}")
    print(f"tavole              : {s['tavole']}")
    print(f"righe ricambi       : {s['righe']}")
    if s["scaricati"]:
        print("\nscaricati:")
        for r in con.execute("SELECT codice, serie, scaricato_il FROM cataloghi "
                             "WHERE scaricato_il IS NOT NULL ORDER BY scaricato_il"):
            print(f"  {r['codice']:<22} {r['serie'][:26]:<26} {r['scaricato_il']}")

def c_elenco(args, con):
    righe = con.execute("""
        SELECT codice, serie, validita_da, validita_a, scaricato_il
        FROM cataloghi
        ORDER BY serie, codice
    """).fetchall()

    if not righe:
        print("Indice vuoto.")
        print("Esegui prima: python goldoni.py indice")
        return

    print(f"{len(righe)} cataloghi presenti nell'indice:\n")

    print(f"{'CODICE':<22} {'SERIE':<30} {'VALIDITÀ':<20}{'STATO'}")
    print('-'*85)
    for r in righe:
        stato = "scaricato" if r["scaricato_il"] else "-"
        validita = f"{r['validita_da'] or '...'} -> {r['validita_a'] or '...'}"
        print(f"{r['codice']:<22} {r['serie']:<30} {validita:<20} {stato}")

def c_scarica_tutti(args, con):
    righe = con.execute("""
        SELECT codice, serie
        FROM cataloghi
        WHERE scaricato_il IS NULL
        ORDER BY serie, codice
    """).fetchall()

    if not righe:
        print("Nessun catalogo da scaricare.")
        print("Se l'indice e' vuoto, lancia prima: python goldoni.py indice")
        return

    cli = GoldoniClient(delay=args.delay)
    print(f"{len(righe)} cataloghi da scaricare (pausa {args.delay}s fra le pagine)\n")

    falliti = []
    for i, r in enumerate(righe, 1):
        print(f"[{i}/{len(righe)}] {r['codice']} — {r['serie'][:40]}")
        try:
            cli.scarica_catalogo(con, r["codice"],
                                 max_pagine=args.max_pagine,
                                 debug=args.debug)
        except KeyboardInterrupt:
            print("\nInterrotto dall'utente.")
            break
        except Exception as e:
            print(f"  Errore su {r['codice']}: {e}")
            falliti.append(r["codice"])

    s = db.statistiche(con)
    print(f"\nFatto. In archivio: {s['tavole']} tavole, {s['righe']} righe.")
    if falliti:
        print(f"{len(falliti)} cataloghi falliti: {', '.join(falliti)}")


def main():
    p = argparse.ArgumentParser(description="Catalogo ricambi Goldoni")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("indice", help="scarica l'elenco dei cataloghi")
    s.set_defaults(func=c_indice)

    s = sub.add_parser("cataloghi", help="cerca il catalogo di una macchina")
    s.add_argument("--modello")
    s.add_argument("--matricola")
    s.add_argument("--testo")
    s.set_defaults(func=c_cataloghi)

    s = sub.add_parser("scarica", help="scarica un catalogo completo")
    s.add_argument("codice")
    s.add_argument("--delay", type=float, default=1.0)
    s.add_argument("--max-pagine", type=int, default=600)
    s.add_argument("--debug", action="store_true",
                   help="mostra i link trovati e scartati")
    s.set_defaults(func=c_scarica)

    s = sub.add_parser("cerca", help="cerca un ricambio")
    s.add_argument("testo")
    s.add_argument("--catalogo")
    s.add_argument("--matricola")
    s.add_argument("--modello")
    s.add_argument("--limite", type=int, default=40)
    s.set_defaults(func=c_cerca)

    s = sub.add_parser("stato", help="cosa c'e' in archivio")
    s.set_defaults(func=c_stato)

    s = sub.add_parser("elenco", help="elenca tutti i cataloghi presenti nell'indice")
    s.set_defaults(func=c_elenco)

    s = sub.add_parser("scarica_tutti", help="scarica tutti i cataloghi non ancora scaricati")
    s.add_argument("--delay", type=float, default=1.0)
    s.add_argument("--max-pagine", type=int, default=600)
    s.add_argument("--debug", action="store_true")
    s.set_defaults(func=c_scarica_tutti)

    args = p.parse_args()
    con = db.apri()
    try:
        args.func(args, con)
    finally:
        con.close()

if __name__ == "__main__":
    sys.exit(main())
