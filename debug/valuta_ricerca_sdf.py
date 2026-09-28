"""
Misura quanto la ricerca ricambi SDF trova il pezzo giusto partendo dalle
frasi VERE dei clienti (prese da dati/conversazioni_4.db, test dell'azienda
di settembre 2026), sui modelli gia' scaricati in fornitori/sdf/sdf.db.

Ogni caso e' (frase del cliente, regex sulla descrizione del pezzo atteso,
regex sul contesto gruppo / sottogruppo / tavola). Per ogni modello in cache
dove esiste almeno un pezzo che rispetta entrambe le regex, si lancia
cerca_ricambio con la frase cosi' com'e' e si guarda in quale blocco
(tavola) compare per la prima volta un pezzo atteso:
  @1  nel primo blocco (quello che l'LLM legge di sicuro)
  @3  entro i primi tre
  -   non trovato (o fuori dagli 8 blocchi restituiti)

Confronta la ricerca attuale (FornitoreSdf._query, LIKE su ogni parola)
con quella arricchita (fornitori/sdf/ricerca.py). Non tocca la rete: il
fornitore viene costruito senza login, solo con il DB locale.

Uso (dalla radice del progetto):
  ./venv/Scripts/python.exe debug/valuta_ricerca_sdf.py          # riepilogo
  ./venv/Scripts/python.exe debug/valuta_ricerca_sdf.py -v       # caso per caso
  ./venv/Scripts/python.exe debug/valuta_ricerca_sdf.py -v -f "paraolio"
"""
import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8")

from fornitori.sdf.adapter import FornitoreSdf      # noqa: E402
from fornitori.sdf.client import BRANDS             # noqa: E402
from fornitori.sdf.db import Db                     # noqa: E402
from fornitori.sdf import ricerca                   # noqa: E402

TUTTO = "."
NO_PTO = r"^(?!.*P\.\s?T\.\s?O).*"

# (frase del cliente, descrizione attesa, contesto atteso)
CASI = [
    # --- vocabolario diverso dal catalogo
    ("mi serve la ventola del radiatore", r"^ventola", r"VENTOL(A|E)$|VISCOSTATICA|RAFFREDDAMENTO$"),
    ("la ventola di raffreddamento", r"^ventola", r"VENTOL(A|E)$|VISCOSTATICA|RAFFREDDAMENTO$"),
    ("mi serve il paraolio dell'albero della frizione", r"anello tenuta|paraolio", r"ALBERO (FRIZIONE|ENTRATA)"),
    ("è un paraolio 25 32 7", r"25 ?x ?32 ?x ?7", TUTTO),
    ("il paraolio dell'albero primario", r"anello tenuta|paraolio", r"PRIMARIO"),
    ("il pomello della leva delle marce", r"pomolo|pomello", r"MARCE|CAMBIO"),
    ("mi serve il pomello del cambio", r"pomolo|pomello", r"MARCE|CAMBIO"),
    ("mi serve il quadro di accensione", r"dispositivo avviam|interruttore", r"BLOCCHETTO (DI )?(ACCENSIONE|AVVIAMENTO)"),
    ("il blocchetto accensione, quadro chiave", r"dispositivo avviam|interruttore", r"BLOCCHETTO (DI )?(ACCENSIONE|AVVIAMENTO)"),
    ("il coso dove metti le chiavi per accendere il trattore", r"dispositivo avviam|interruttore", r"BLOCCHETTO (DI )?(ACCENSIONE|AVVIAMENTO)"),
    ("mi serve anche il devioluci", r"deviatore", TUTTO),
    ("ho bisogno del filo dell'acceleratore", r"^(tirante|cavo)", r"ACCELERATORE"),
    ("il filo dell'acceleratore a mano", r"^(tirante|cavo)", r"ACCELERATORE"),
    ("il potenziometro del pedale acceleratore", r"sensore di posizione|potenziometro", r"ACCELERATORE"),
    ("ho bisogno delle prese idrauliche", r"^innesto", r"PRESE|INNEST|DISTRIBUTORE"),
    ("il galleggiante del gasolio", r"galleggiante|sensore di livello|indicatore livello", r"CARBURANTE|GALLEGGIANTE|ALIMENTAZIONE - SERBATOIO"),
    ("la meccia della pto", r"albero presa potenza", TUTTO),
    ("l'albero dove attacca il cardano per gli attrezzi", r"albero presa potenza", TUTTO),
    ("mi servono i fermi dell'albero primario", r"anello elastico", r"PRIMARIO"),
    ("mi servono i cuscinetti reggispinta della frizione", r"cuscinetto", r"COMANDO DELLA FRIZIONE"),
    ("il manicotto dell'albero sotto il trattore che collega ponte anteriore e ponte posteriore", r"manicotto", r"ALBERO DI TRASMISSIONE"),
    ("l'albero di trasmissione della doppia trazione", r"^albero", r"ALBERO DI TRASMISSIONE"),
    ("gli adesivi del cofano", r"targhetta|adesiv|simbolo|scritta", r"COFANO|TARGHETTA PRODOTTO|LETTERING"),
    ("pompetta alimentazione gasolio", r"pompa alimentazione", TUTTO),
    ("la pompetta ac", r"pompa alimentazione", TUTTO),
    ("mi serve la molla dello sforzo controllato", r"^molla", r"ORGAN[OI] SENSIBIL"),
    ("anche il kit guarnizioni per i pistoni sollevatori", r"anello tenuta|guarnizione|serie", r"SOLLEVA"),
    ("mi serve la guarnizione del pistone sollevatore", r"anello tenuta|guarnizione", r"SOLLEVA"),
    ("mi serve anche l'albero dei bracci sollevatore", r"^albero", r"SOLLEVA"),
    ("ho bisogno di pompa e cilindretto frizione", r"^(pompa|cilindro)", r"COMANDO DELLA FRIZIONE|COMANDI FRIZIONE"),
    ("il cilindretto della frizione", r"^cilindro", r"COMANDO DELLA FRIZIONE|COMANDI FRIZIONE"),
    ("la molla del pedale frizione", r"^molla", r"COMANDO DELLA FRIZIONE"),
    ("mi serve la frizione centrale", r"frizione completa|disco frizione", NO_PTO + r"FRIZION"),
    ("il termostato dell'acqua", r"^termostato$", TUTTO),
    ("il tappo del serbatoio del gasolio", r"^tappo", r"SERBATOIO CARBURANTE|ALIMENTAZIONE - SERBATOIO"),
    ("il cavo della batteria", r"^cavo", r"BATTERIA"),
    # --- termini gia' presenti nel catalogo (controllo di non regressione)
    ("mi servono i nastri freno", r"nastro freno", TUTTO),
    ("mi servono i dischi della frizione di sterzo", r"^disco", r"FRIZIONE DELLO STERZO"),
    ("mi servono anche le pompe di sterzo", r"^pompa", r"STERZO"),
    ("ho bisogno anche del portasatellite del ponte posteriore", r"portasatellite", r"POSTERIOR|SEMIASSI"),
    ("ho bisogno dell'albero motore", r"^albero motore", TUTTO),
    ("mi serve anche la coppia conica posteriore", r"coppia conica|corona conica|pignone conico", TUTTO),
    ("1 serie guarnizioni motore", r"serie guarnizioni", r"MOTORE|MONTAGGIO"),
    ("4 canne", r"canna|cilindro motore", TUTTO),
    ("serie fasce", r"fasce", TUTTO),
    ("serie bronzine biella", r"semibronzina biella", TUTTO),
    ("mi serve un filtro olio motore", r"filtro olio motore", TUTTO),
    ("dischi freno posteriori", r"disco freno", r"POSTERIOR"),
    ("mi serve il radiatore", r"radiatore acqua", TUTTO),
    ("mi serve il termostato", r"^termostato$", TUTTO),
]

SQL_PARTI = """
SELECT p.code, p.description, g.name AS g, s.name AS s, d.name AS d
FROM part p
JOIN drawing d        ON d.revision_id = p.revision_id
JOIN model_drawing md ON md.revision_id = p.revision_id
LEFT JOIN grp g       ON g.row_id = md.group_id
LEFT JOIN subgroup s  ON s.row_id = md.subgroup_id
WHERE md.brand = ? AND md.model_id = ?
"""


def modelli_in_cache(db):
    out = []
    for r in db.query("SELECT key FROM crawl_state WHERE key LIKE 'mod:%'"):
        _, brand_code, model_id = r["key"].split(":")
        label = next(k for k, v in BRANDS.items() if v == brand_code)
        nome = db.one("SELECT name FROM model WHERE brand=? AND row_id=?",
                      (brand_code, int(model_id)))
        out.append((label, int(model_id), f"{label} {nome['name'] if nome else model_id}"))
    return out


def fornitore(db, label, model_id, nome, metodo):
    """FornitoreSdf senza __init__ (niente login): solo DB locale."""
    f = FornitoreSdf.__new__(FornitoreSdf)
    f.db, f.brand, f.model_id, f.family_id = db, label, model_id, 0
    f.nome_macchina, f.machine = nome, None
    if metodo == "arricchita":
        f._query = lambda testo, limit=60: ricerca.cerca(
            f.db, BRANDS[f.brand], f.model_id, testo, limit=limit)
    return f


def posizione(ris, attesi):
    for i, b in enumerate(ris.get("blocchi", []), 1):
        if any(a["codice"] in attesi for a in b["articoli"]):
            return i
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-v", action="store_true", help="dettaglio caso per caso")
    ap.add_argument("-f", help="solo i casi che contengono questo testo")
    a = ap.parse_args()

    db = Db()
    modelli = modelli_in_cache(db)
    parti = {m[:2]: db.query(SQL_PARTI, (BRANDS[m[0]], m[1])) for m in modelli}

    metodi = ("attuale", "arricchita")
    tot = {m: {"n": 0, "@1": 0, "@3": 0} for m in metodi}
    for frase, rx_desc, rx_ctx in CASI:
        if a.f and a.f.lower() not in frase.lower():
            continue
        rd, rc = re.compile(rx_desc, re.I), re.compile(rx_ctx, re.I)
        for label, mid, nome in modelli:
            attesi = {r["code"] for r in parti[(label, mid)]
                      if rd.search(r["description"] or "")
                      and rc.search(f"{r['g']} / {r['s']} / {r['d']}")}
            if not attesi:
                continue
            riga = []
            for m in metodi:
                # alla ricerca attuale si tolgono le parole di riempimento
                # ("ho bisogno", "anche"...): basta una parola non trovata
                # per azzerarla, e in produzione l'LLM di solito le toglie
                testo = frase if m == "arricchita" else " ".join(
                    w for w in frase.split() if w.lower().strip(",.") not in ricerca._VUOTE)
                ris = fornitore(db, label, mid, nome, m).cerca_ricambio(testo)
                pos = posizione(ris, attesi)
                tot[m]["n"] += 1
                tot[m]["@1"] += pos == 1
                tot[m]["@3"] += bool(pos and pos <= 3)
                riga.append(f"{m}={pos or '-'}")
                if a.v and m == "arricchita" and pos != 1:
                    for b in ris.get("blocchi", [])[:3]:
                        print(f"        {b['contesto'][-60:]:60} "
                              f"{[x['descrizione'] for x in b['articoli']][:4]}")
            if a.v:
                print(f"{'  '.join(riga):28} {frase[:55]:55} | {nome[:35]}")

    print()
    for m in metodi:
        n = tot[m]["n"] or 1
        print(f"{m:11} casi {tot[m]['n']:3}   @1 {tot[m]['@1']:3} ({100 * tot[m]['@1'] // n}%)"
              f"   @3 {tot[m]['@3']:3} ({100 * tot[m]['@3'] // n}%)")


if __name__ == "__main__":
    main()
