"""
L'arricchimento risolve il problema "il cliente usa parole che nel
catalogo non ci sono"? Misura su domande scritte come le scriverebbe
un cliente (italiano colloquiale, qualche siciliano, termini imprecisi).

Le domande sono formulate apposta in modo DIVERSO dai sinonimi di
arricchimento.py (altrimenti il test sarebbe truccato); fanno
eccezione poche domande che per natura coincidono (es. "copiglia").

Confronta questi modi di cercare:
  parole/base    parole della domanda nelle descrizioni inglesi (com'e' oggi)
  parole/ricco   parole della domanda nel testo arricchito
  <b>/base       embedding (backend b: voyage, gemini) sulle sole descrizioni inglesi
  <b>/ricco      embedding sul testo arricchito
(solo i backend i cui vettori sono gia' calcolati con embeddings.py)

Metriche (a livello di concetto, cioe' "tipo di pezzo giusto"):
  top1  il primo risultato e' giusto
  top5  un risultato giusto e' tra i primi 5

Uso: ./venv/Scripts/python.exe lancer_db/valuta.py [-v]
"""
import re
import sys
import unicodedata

import numpy as np

from db import connetti
from embeddings import carica, testo_base, testo_ricco, vettorizza

sys.stdout.reconfigure(encoding="utf-8")

# (domanda del cliente, concetti accettati come risposta giusta)
DOMANDE = [
    ("mi serve il pistone che alza il rullo", {"cilindro_idraulico"}),
    ("il martinetto per spostare la trincia di lato", {"cilindro_idraulico"}),
    ("u pistuni idraulicu", {"cilindro_idraulico"}),
    ("le zappe della fresa sono consumate", {"zappa"}),
    ("coltelli a L della zappatrice", {"zappa"}),
    ("i martelli della trinciatrice", {"mazza", "rotore_completo"}),
    ("cuteddi pa trincia", {"mazza", "coltello_y", "coltello_dritto"}),
    ("si e' rotta la cinghia della trincia", {"cinghia"}),
    ("cuscinetto del rullo dietro", {"cuscinetto_sfere", "supporto_flangiato", "cuscinetto_orientabile"}),
    ("perde olio dalla scatola ingranaggi, che guarnizione devo cambiare", {"paraolio", "guarnizione", "oring"}),
    ("la gomma che pende dietro la trinciatrice", {"bandella_gomma"}),
    ("le catene davanti alla trincia", {"catenelle"}),
    ("il ferro che striscia per terra ai lati della fresa", {"slitta"}),
    ("pattino laterale", {"slitta"}),
    ("il tirante in alto che va al trattore", {"terzo_punto"}),
    ("il piede per tenerla in piedi quando la stacco", {"cavalletto"}),
    ("la punta che si consuma sul dente dell'estirpatore", {"puntale"}),
    ("ancore del vibrocoltivatore", {"dente_coltivatore"}),
    ("disco dell'erpice", {"disco", "gruppo_disco"}),
    ("i gommini che tengono i dischi", {"tampone_gomma"}),
    ("attacco dell'olio da mettere nel trattore", {"innesto_rapido"}),
    ("tubo flessibile del pistone", {"tubo_idraulico"}),
    ("ingrassatore storto", {"ingrassatore"}),
    ("tappo per mettere l'olio nella scatola", {"tappo_olio", "tappo_sfiato"}),
    ("dado che non si svita", {"dado_autobloccante"}),
    ("rondella elastica spaccata M12", {"rondella_grower"}),
    ("anello che blocca il cuscinetto nell'albero", {"seeger_esterno", "seeger_interno"}),
    ("ingranaggi del carter laterale", {"ingranaggio_cascata", "ingranaggio_cambio"}),
    ("la coppia conica della scatola", {"pignone_conico", "corona_conica"}),
    ("lamiera che livella dietro la fresa", {"cofano_posteriore"}),
    ("molla che tiene giu' il cofano", {"tirante_molla", "molla"}),
    ("rullo dell'erpice rotante", {"rullo_packer", "rullo"}),
    ("denti dell'erpice rotante", {"dente_erpice"}),
    ("la lamiera di lato all'erpice che contiene la terra", {"sponda_mobile", "sponda_laterale"}),
    ("la protezione di plastica del cardano", {"protezione_cardano"}),
    ("albero dove infilo il cardano", {"albero_entrata"}),
    ("u bulluni du tilaiu", {"bullone"}),
    ("lama del tosaerba", {"lama_rasaerba"}),
    ("la lama fissa dentro la trincia", {"controcoltello"}),
    ("il pettine della trinciatrice", {"pettine"}),
    ("castello attacco trattore", {"attacco_tre_punti"}),
    ("spessori per registrare gli ingranaggi", {"spessori"}),
    ("tenuta del mozzo del rotore della fresa", {"tenuta_meccanica", "paraolio"}),
    ("la cassa dove girano gli ingranaggi dell'erpice", {"cassone"}),
    ("chiavetta dell'albero", {"linguetta"}),
    ("copiglia", {"coppiglia", "copiglia_r", "spina_scatto"}),
    ("mozzo del disco con cuscinetto", {"mozzo_disco"}),
    ("scatola cambio completa della fresa", {"scatola_ingranaggi"}),
    ("braccio pieghevole dell'erpice a dischi", {"braccio"}),
    ("supporto con cuscinetto del rullo", {"supporto_flangiato"}),
]

_STOP = set("il lo la i gli le un una uno di da del della dei delle dello al alla ai "
            "che per con su in e a mi serve devo mettere si e' sono dove quando nel "
            "nella nell dell all u a du pa di".split())


def parole(testo):
    t = unicodedata.normalize("NFKD", testo.lower()).encode("ascii", "ignore").decode()
    out = set()
    for w in re.findall(r"[a-z]{3,}", t):
        if w in _STOP:
            continue
        out.add(w[:-1] if len(w) > 4 else w)     # radice grezza: zappa/zappe, rullo/rulli
    return out


def classifica_parole(domanda, docs):
    q = parole(domanda)
    punti = [(len(q & d), t) for t, d in docs.items()]
    punti = [p for p in punti if p[0] > 0]
    punti.sort(key=lambda x: -x[0])
    return [t for _, t in punti]


def concetti_in_ordine(tipi_ordinati, tipo2conc):
    visti = []
    for t in tipi_ordinati:
        c = tipo2conc[t]
        if c not in visti:
            visti.append(c)
    return visti


def vettori_domande(con, backend):
    """Le domande si vettorizzano una volta sola per backend (cache nel DB)."""
    chiave = lambda d: f"{backend}|domanda:{d}"  # noqa: E731
    mancanti = [d for d, _ in DOMANDE if not con.execute(
        "SELECT 1 FROM embeddings WHERE chiave=?", (chiave(d),)).fetchone()]
    if mancanti:
        for d, v in zip(mancanti, vettorizza(mancanti, backend, domanda=True)):
            con.execute("INSERT OR REPLACE INTO embeddings VALUES (?,?,?,?)",
                        (chiave(d), d, backend, v.tobytes()))
        con.commit()
    return {d: np.frombuffer(con.execute("SELECT vettore FROM embeddings WHERE chiave=?",
                                         (chiave(d),)).fetchone()[0], dtype=np.float32)
            for d, _ in DOMANDE}


def main(verboso=False):
    con = connetti()
    tipi = con.execute("SELECT * FROM tipi WHERE concetto IS NOT NULL").fetchall()
    tipo2conc = {t["tipo_en"]: t["concetto"] for t in tipi}
    docs = {"parole/base": {t["tipo_en"]: parole(testo_base(t)) for t in tipi},
            "parole/ricco": {t["tipo_en"]: parole(testo_ricco(t)) for t in tipi}}
    # backend con i vettori del catalogo completi
    indici, qv = {}, {}
    for b in ("voyage", "gemini"):
        base, ricco = carica(con, "base:", b), carica(con, "ricco:", b)
        if len(base[0]) == len(tipi) and len(ricco[0]) == len(tipi):
            indici[f"{b}/base"], indici[f"{b}/ricco"] = base, ricco
            qv[b] = vettori_domande(con, b)

    metodi = ["parole/base", "parole/ricco"] + list(indici)
    ris = {m: [0, 0, 0] for m in metodi}          # top1, top5, nessun risultato
    for domanda, attesi in DOMANDE:
        riga = []
        for m in metodi:
            if m.startswith("parole"):
                ordine = classifica_parole(domanda, docs[m])
            else:
                chiavi, mat = indici[m]
                q = qv[m.split("/")[0]][domanda]
                ordine = [chiavi[i] for i in np.argsort(-(mat @ q))[:30]]
            conc = concetti_in_ordine(ordine, tipo2conc)
            t1 = bool(conc) and conc[0] in attesi
            t5 = bool(set(conc[:5]) & attesi)
            ris[m][0] += t1
            ris[m][1] += t5
            ris[m][2] += not conc
            riga.append(("1" if t1 else "5" if t5 else "-") + " " + (conc[0] if conc else "(nulla)"))
        if verboso:
            print(f"{domanda[:44]:44s} | " + " | ".join(f"{r[:20]:20s}" for r in riga))

    n = len(DOMANDE)
    if verboso:
        print("\n(colonne: " + ", ".join(metodi) + ";  1 = giusto al primo posto, 5 = nei primi 5)")
    print(f"\n{n} domande        top1        top5     senza risultati")
    for m in metodi:
        t1, t5, zero = ris[m]
        print(f"  {m:13s}  {t1:3d} ({t1 / n:4.0%})  {t5:3d} ({t5 / n:4.0%})  {zero:3d}")


if __name__ == "__main__":
    main("-v" in sys.argv)
