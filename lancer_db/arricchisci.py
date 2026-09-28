"""
Arricchimento del catalogo LANCER: nome italiano, categoria, funzione,
sinonimi colloquiali e siciliani per ogni ricambio.

Problema che risolve: le descrizioni nei PDF sono in inglese tecnico
("Nylock Nut M12-8 Zp D985", "Hydraulic cylinder ... stroke 195mm"),
mentre il cliente scrive "dado autobloccante", "il pistone che alza il
rullo", "u pistuni". Senza un ponte tra i due vocabolari la ricerca non
trova nulla anche se il pezzo e' a catalogo.

I dati di arricchimento sono SCRITTI A MANO in arricchimento.py
(concetti + regole), non generati da un LLM a runtime: cosi' sono
verificabili, versionabili e correggibili dall'azienda.

Passi:
  1. ogni descrizione viene ridotta a un "tipo" senza misure/modelli
     ("Hex Bolt M12 x 35 8.8 Zn D933" -> "hex bolt");
  2. ogni tipo viene assegnato a un concetto con le REGOLE di
     arricchimento.py (la prima che corrisponde vince);
  3. il concetto (+ destro/sinistro ricavato da LH/RH) viene scritto in
     `tipi` e propagato a `ricambi`.

Se un tipo non corrisponde a nessuna regola viene elencato a fine
esecuzione: va aggiunta una regola.

Uso:
  ./venv/Scripts/python.exe lancer_db/arricchisci.py          # applica
  ./venv/Scripts/python.exe lancer_db/arricchisci.py --csv    # esporta per revisione
"""
import argparse
import csv
import re
import sys
from collections import defaultdict
from pathlib import Path

from arricchimento import CATEGORIE, CONCETTI, REGOLE
from db import CARTELLA, connetti

sys.stdout.reconfigure(encoding="utf-8")

# parole da togliere per ottenere il "tipo": misure, trattamenti,
# norme DIN (hanno cifre, spariscono da sole), sigle di serie/modello
_STOP = {"zn", "zp", "ht", "new", "lg", "mm", "x", "wa", "with", "for", "and", "the",
         "of", "type", "complete", "assly", "assembly", "assy", "std", "weld",
         "welded", "no", "z", "dia", "d", "id", "od", "thk", "p", "l", "n", "s", "t"}
_SIGLE = {"hd", "hp", "hs", "jh", "jl", "jm", "kh", "km", "kx", "mp", "master", "maximo",
          "mb", "pl", "pm", "pmf", "um", "gm", "m", "h", "jmh", "jmf", "jmhs", "jhf",
          "jhh", "jhhs", "jlf", "jlh", "jlhs", "bold", "sb"}

_REGOLE = [(re.compile(r), c) for r, c in REGOLE]


def senza_ripetizione(d):
    """Alcune celle del PDF ripetono il testo: 'Frame KH 290 Frame KH 290'."""
    p = (d or "").split()
    n = len(p)
    if n % 2 == 0 and n and p[:n // 2] == p[n // 2:]:
        return " ".join(p[:n // 2])
    return d


def tipo_di(descrizione):
    t = re.sub(r"\(.*?\)", " ", senza_ripetizione(descrizione or "").lower())
    t = re.sub(r"[-/.&,_]", " ", t)
    parole = [w for w in t.split()
              if not re.search(r"\d", w) and w not in _STOP and w not in _SIGLE]
    return " ".join(parole) or (descrizione or "").lower().strip()


def lato(descrizione):
    d = descrizione or ""
    if re.search(r"\bL\.?\s?H\b|\bLEFT\b", d, re.I):
        return "sinistro"
    if re.search(r"\bR\.?\s?H\b|\bRIGHT\b", d, re.I):
        return "destro"
    return None


def concetto_di(tipo):
    for rx, c in _REGOLE:
        if rx.search(tipo):
            return c
    return None


def popola_ricambi(con):
    """Un record per codice, con la descrizione piu' frequente e il tipo."""
    descr = defaultdict(lambda: defaultdict(int))
    for codice, d in con.execute("SELECT codice, descrizione FROM righe"):
        descr[codice][senza_ripetizione(d)] += 1
    con.execute("DELETE FROM ricambi")
    for codice, varianti in descr.items():
        d = max(varianti, key=varianti.get)
        con.execute("INSERT INTO ricambi(codice, descrizione_en, tipo_en) VALUES (?,?,?)",
                    (codice, d, tipo_di(d)))
    con.execute("DELETE FROM tipi")
    for (tipo,) in con.execute("SELECT DISTINCT tipo_en FROM ricambi").fetchall():
        con.execute("INSERT INTO tipi(tipo_en) VALUES (?)", (tipo,))
    con.commit()


def applica(con):
    senza = []
    usati = set()
    for (tipo,) in con.execute("SELECT tipo_en FROM tipi").fetchall():
        c = concetto_di(tipo)
        if not c:
            senza.append(tipo)
            continue
        usati.add(c)
        nome, cat, funzione, sinonimi, sic = CONCETTI[c]
        assert cat in CATEGORIE, (c, cat)
        esempio = con.execute("SELECT descrizione_en FROM ricambi WHERE tipo_en=? LIMIT 1",
                              (tipo,)).fetchone()[0]
        con.execute("UPDATE tipi SET esempio=?, nome_it=?, categoria=?, funzione=?, "
                    "sinonimi=?, siciliano=?, concetto=? WHERE tipo_en=?",
                    (esempio, nome, cat, funzione, sinonimi, sic, c, tipo))

    for codice, d, tipo in con.execute(
            "SELECT codice, descrizione_en, tipo_en FROM ricambi").fetchall():
        t = con.execute("SELECT nome_it, categoria, sinonimi, siciliano FROM tipi "
                        "WHERE tipo_en=? AND nome_it IS NOT NULL", (tipo,)).fetchone()
        if not t:
            continue
        nome = t[0] + (f" {lato(d)}" if lato(d) else "")
        sinonimi = "; ".join(s for s in (t[2], t[3]) if s)
        con.execute("UPDATE ricambi SET descrizione_it=?, categoria=?, sinonimi=? WHERE codice=?",
                    (f"{nome} - {d}", t[1], sinonimi, codice))
    con.commit()

    tot = con.execute("SELECT COUNT(*) FROM ricambi").fetchone()[0]
    ok = con.execute("SELECT COUNT(*) FROM ricambi WHERE descrizione_it IS NOT NULL").fetchone()[0]
    print(f"ricambi arricchiti: {ok}/{tot}   concetti usati: {len(usati)}/{len(CONCETTI)}")
    for cat, n in con.execute("SELECT categoria, COUNT(*) FROM ricambi GROUP BY 1 ORDER BY 2 DESC"):
        print(f"   {n:5d}  {cat}")
    if senza:
        print(f"\n! {len(senza)} tipi senza regola (aggiungerla in arricchimento.py):")
        for t in senza:
            print("   ", t)
    inutilizzati = set(CONCETTI) - usati
    if inutilizzati:
        print("concetti non usati:", ", ".join(sorted(inutilizzati)))


def esporta_csv(con, percorso=None):
    """CSV per la revisione dell'azienda (BOM UTF-8 + ';' per Excel)."""
    percorso = Path(percorso or CARTELLA / "tipi_da_rivedere.csv")
    q = ("SELECT t.concetto, t.nome_it, t.categoria, t.funzione, t.sinonimi, t.siciliano, "
         "group_concat(t.tipo_en, ' | '), "
         "(SELECT COUNT(*) FROM ricambi r JOIN tipi t2 ON t2.tipo_en = r.tipo_en "
         " WHERE t2.concetto = t.concetto) "
         "FROM tipi t GROUP BY t.concetto ORDER BY t.categoria, t.nome_it")
    with open(percorso, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["concetto", "nome_it", "categoria", "funzione", "sinonimi", "siciliano",
                    "tipi_catalogo_inglese", "n_codici", "note_azienda"])
        for r in con.execute(q):
            w.writerow(list(r) + [""])
    print(f"esportato: {percorso}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", action="store_true", help="esporta il CSV di revisione")
    a = ap.parse_args()
    con = connetti()
    if a.csv:
        esporta_csv(con)
    else:
        popola_ricambi(con)
        applica(con)
