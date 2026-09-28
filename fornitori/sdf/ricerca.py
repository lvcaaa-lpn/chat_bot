"""
Ricerca ricambi SDF sul testo ARRICCHITO (vedi arricchimento.py): stessa
tecnica del bot LANCER, adattata al catalogo SDF.

Differenze rispetto a FornitoreSdf._query (LIKE su ogni parola):

1. Nessuna parola e' obbligatoria. _query chiedeva che OGNI parola della
   richiesta comparisse da qualche parte: "il coso dove metti le chiavi"
   o "paraolio albero frizione" (il pezzo si chiama "anello tenuta")
   davano zero risultati. Qui ogni riga prende un punteggio e si ordina.

2. Il nome del pezzo include i sinonimi del suo concetto: "pomolo" si
   trova anche come "pomello", "tirante" come "filo", "dispositivo
   avviam." come "blocchetto accensione", "anello tenuta" come "paraolio".

3. Pesi (gli stessi che nel test LANCER hanno dato il risultato migliore):
   una parola trovata nel NOME del pezzo (descrizione o sinonimi) vale 2;
   trovata solo nel CONTESTO (gruppo / sottogruppo / tavola, piu' le
   parole di CONTESTI e l'"evoca" del concetto) vale 1, la meta' se il
   pezzo e' bulloneria (MINUTERIA). Cosi' "paraolio albero frizione"
   mette prima l'anello di tenuta della tavola ALBERO FRIZIONE CAMBIO
   (2 + 1 + 1) di un paraolio qualsiasi (2) e della vite della stessa
   tavola (0.5 + 0.5).

4. Misure: "paraolio 25 32 7" trova "anello tenuta speciale 25 x 32 x 7"
   (i numeri della richiesta, se sono almeno due, compaiono nello stesso
   ordine tra quelli della descrizione).

5. Codici: una parola che sembra un codice ("2.1529.125.0", "04411512.4")
   cerca nel codice del pezzo, e se lo trova vince su tutto.

Il risultato ha le stesse colonne di _query (rilevanza e pertinenza_tavola
comprese), piu' "trovato_come": il nome del concetto quando il pezzo e'
stato trovato grazie a un sinonimo e non alla sua descrizione, cosi' il
bot puo' dichiarare l'interpretazione ("cercato come paraolio, a
catalogo e' anello tenuta").

Prova da riga di comando:
  ./venv/Scripts/python.exe -m fornitori.sdf.ricerca LAMBORGHINI 11572 "paraolio albero frizione"
  ./venv/Scripts/python.exe -m fornitori.sdf.ricerca --senza-regola
"""
import re
import sys
import threading
from collections import Counter, OrderedDict

from ..testo import FERMA, radice, senza_accenti
from .arricchimento import CONCETTI, CONTESTI, MINUTERIA, REGOLE

PESO_NOME = 2
PESO_CONTESTO = 1
PESO_CONTESTO_MINUTERIA = 0.5
BONUS_MISURE = 4
BONUS_CODICE = 100
# spareggi (piu' piccoli di qualunque parola trovata, decidono solo i pari merito)
BONUS_TESTA = 0.5          # il pezzo e' la prima parola del cliente
PENALITA_PAROLA_IN_PIU = 0.1
MAX_PENALITA = 0.4

# parole della richiesta che non dicono nulla sul pezzo (oltre a FERMA)
_VUOTE = set(FERMA) | set("""
    anche ancora bisogno ho hai ha abbiamo avrei servono servirebbe serviva
    vorrei volevo cerco cercavo trattore trattorino macchina mezzo coso cosa
    affare aggeggio quello quella quelli quelle questo questa dove come quando
    metti mette mettere si ci va vanno sono sta stanno mio mia miei mie tuo
    tipo tutto tutti poi pure allora non no ok grazie ciao altro altra altri
    gli al alla ai agli sul sulla nei negli per tra fra sempre solo proprio
    fatto fatta intendo intendevo dicevo chiamato chiama sarebbe credo penso
""".split())

_REGOLE = [(re.compile(r), c) for r, c in REGOLE]
_CONTESTI = [(re.compile(r, re.I), p) for r, p in CONTESTI]


# ------------------------------------------------------------ testo
def _normalizza(testo):
    t = senza_accenti(testo or "").lower()
    # sigle con i punti: "p.t.o." -> "pto", "s.a.c." -> "sac"
    t = re.sub(r"\bp\.\s?t\.\s?o\b\.?", " pto ", t)
    t = re.sub(r"\bs\.\s?a\.\s?c\b\.?", " sac ", t)
    return t


def parole(testo):
    """Radici delle parole significative: 'i dischi della frizione' -> {'disc', 'frizion'}."""
    out = set()
    for w in re.findall(r"[a-z0-9]+", _normalizza(testo)):
        if w.isdigit() or len(w) < 2 or w in _VUOTE:
            continue
        out.add(radice(w))
    return out


def numeri(testo):
    """Misure nella descrizione o nella richiesta: '25 x 32 x 7' -> ['25', '32', '7']."""
    return [n.replace(",", ".") for n in re.findall(r"\d+(?:[.,]\d+)?", testo or "")]


def _sottosequenza(corta, lunga):
    it = iter(lunga)
    return all(x in it for x in corta)


def _sembra_codice(w):
    return bool(re.fullmatch(r"[0-9][0-9a-z./-]{5,}", w)) and (
        "." in w or w.isdigit())


def tipo_di(descrizione):
    """Descrizione senza misure, sigle, lato e note:
    'anello tenuta speciale 25 x 32 x 7' -> 'anello tenuta speciale',
    'semiasse - DX/RH/RE' -> 'semiasse', "leva x frizione" -> 'leva frizione'."""
    t = senza_accenti(descrizione or "").lower()
    t = re.sub(r"\(.*?\)", " ", t)
    t = re.split(r"\s-\s", t)[0]
    t = re.sub(r"[/.,\"'°\-]", " ", t)
    scarta = {"mm", "x", "m", "p", "z", "kpl", "dx", "sx", "rh", "lh", "re", "li", "a", "d"}
    return " ".join(w for w in t.split() if not re.search(r"\d", w) and w not in scarta)


# ---------------------------------------------------------- concetti
_CACHE_CONCETTO = {}


def concetto_di(tipo):
    if tipo not in _CACHE_CONCETTO:
        _CACHE_CONCETTO[tipo] = next((c for rx, c in _REGOLE if rx.search(tipo)), None)
    return _CACHE_CONCETTO[tipo]


_PAROLE_CONCETTO = {}


def _testa(frase):
    """Prima parola significativa di un sinonimo: 'tubo del radiatore' -> 'tub'."""
    for w in re.findall(r"[a-z0-9]+", _normalizza(frase)):
        if not w.isdigit() and len(w) >= 2 and w not in _VUOTE:
            return radice(w)
    return None


def _parole_concetto(c):
    """(parole che NOMINANO il pezzo, parole di contorno) di un concetto.

    Di ogni sinonimo conta solo la prima parola: in "tubo del radiatore"
    (sinonimo di manicotto in gomma) "radiatore" dice DOVE sta quel tubo,
    non cos'e', e non vale per tutti i manicotti. Contandola, "radiatore"
    metteva i manicotti davanti al radiatore e "pomello del cambio" dava
    "cambio" anche ai pomoli del distributore. Le parole di contorno valide
    per TUTTO il concetto stanno in "evoca". Il nome tecnico (nome_it)
    conta invece per intero."""
    if c not in _PAROLE_CONCETTO:
        nome, sinonimi, siciliano, evoca = CONCETTI[c]
        frasi = [f for f in f"{sinonimi};{siciliano}".split(";") if f.strip()]
        nomi = parole(nome) | {t for t in map(_testa, frasi) if t}
        _PAROLE_CONCETTO[c] = (nomi, parole(evoca) - nomi)
    return _PAROLE_CONCETTO[c]


_PAROLE_CONTESTO = {}


def _parole_contesto(contesto):
    """(parole scritte nel gruppo / sottogruppo / tavola, parole aggiunte da
    CONTESTI). Separate perche' negli spareggi conta chi e' scritto davvero
    nel catalogo: "fermi dell'albero primario" deve preferire la tavola
    ALBERO PRIMARIO a ALBERO ENTRATA CAMBIO (dove "primario" e' solo
    aggiunto da CONTESTI)."""
    if contesto not in _PAROLE_CONTESTO:
        scritte = parole(contesto)
        extra = parole(" ".join(p for rx, p in _CONTESTI if rx.search(contesto)))
        _PAROLE_CONTESTO[contesto] = (scritte, extra - scritte)
    return _PAROLE_CONTESTO[contesto]


def _trova(w, insieme):
    """w (radice della richiesta) compare in insieme? Uguale, oppure una
    delle due e' l'inizio dell'altra con al massimo 2 lettere di scarto
    ('marc'/'marci', 'ventol'/'ventole'), mai sotto le 4 lettere: e' il
    limite che evita il vecchio falso positivo 'ari' dentro 'particolari'."""
    if w in insieme:
        return True
    for d in insieme:
        corta, lunga = (w, d) if len(w) <= len(d) else (d, w)
        if len(corta) >= 4 and len(lunga) - len(corta) <= 2 and lunga.startswith(corta):
            return True
    return False


# ------------------------------------------------ catalogo del modello
SQL_MODELLO = """
    SELECT p.code, p.description, p.position, p.quantity, p.price,
           p.sellable, p.replaced, p.abolished,
           d.revision_id, d.name AS drawing_name, d.notes,
           d.tractor_sn_range, d.preview_url,
           g.name AS group_name, s.name AS subgroup_name
    FROM part p
    JOIN drawing d        ON d.revision_id = p.revision_id
    JOIN model_drawing md ON md.revision_id = p.revision_id
    LEFT JOIN grp g       ON g.row_id = md.group_id
    LEFT JOIN subgroup s  ON s.row_id = md.subgroup_id
    WHERE md.brand = ? AND md.model_id = ?
    ORDER BY d.revision_id, p.position
"""

# Catalogo preparato per modello (parole di nome e contesto gia' calcolate).
# Si chiama solo a catalogo completo (cerca_ricambio risponde
# "in_preparazione" prima), quindi il contenuto non cambia piu'. Tenuto in
# memoria per pochi modelli: e' condiviso tra le sessioni ma di sola
# lettura, come l'indice di lancer_db/ricerca.py.
MAX_MODELLI_IN_MEMORIA = 20
_PREPARATI = OrderedDict()
_PREPARATI_LK = threading.Lock()


def _prepara(db, brand_code, model_id):
    chiave = (brand_code, model_id)
    with _PREPARATI_LK:
        if chiave in _PREPARATI:
            _PREPARATI.move_to_end(chiave)
            return _PREPARATI[chiave]
    righe = []
    for r in db.query(SQL_MODELLO, (brand_code, model_id)):
        r = dict(r)
        c = concetto_di(tipo_di(r["description"]))
        nome_c, contorno_c = _parole_concetto(c) if c else (set(), set())
        contesto = " / ".join(x for x in (r["group_name"], r["subgroup_name"], r["drawing_name"]) if x)
        tavola, extra = _parole_contesto(contesto)
        righe.append((r, parole(r["description"]), nome_c, tavola, extra | contorno_c,
                      c, numeri(r["description"])))
    with _PREPARATI_LK:
        _PREPARATI[chiave] = righe
        while len(_PREPARATI) > MAX_MODELLI_IN_MEMORIA:
            _PREPARATI.popitem(last=False)
    return righe


# ------------------------------------------------------------ ricerca
def cerca(db, brand_code, model_id, testo, limit=60):
    """Righe del modello ordinate per pertinenza alla richiesta, con le
    stesse colonne di FornitoreSdf._query (+ 'trovato_come')."""
    grezze = _normalizza(testo).split()
    codici = [w.strip(".,;") for w in grezze if _sembra_codice(w.strip(".,;"))]
    senza_codici = " ".join(w for w in grezze if w.strip(".,;") not in codici)
    cercate = parole(senza_codici)
    # In italiano il pezzo viene prima dei complementi: "la VENTOLA del
    # radiatore", "il FILO dell'acceleratore". A parita' di punteggio vince
    # la riga che nomina questa parola (la ventola, non il radiatore).
    testa = _testa(senza_codici)
    misure = numeri(testo) if not codici else []
    if len(misure) < 2:
        misure = []
    if not (cercate or codici or misure):
        return []

    trovate = []
    for r, nome_desc, nome_c, contesto, contorno_c, c, num in _prepara(db, brand_code, model_id):
        punti = 0.0
        nel_nome = nella_tavola = nel_contorno = 0
        via_sinonimo = False
        if codici and any(k in r["code"].lower() for k in codici):
            punti += BONUS_CODICE
        for w in cercate:
            if _trova(w, nome_desc):
                nel_nome += 1
            elif _trova(w, nome_c):
                nel_nome += 1
                via_sinonimo = True
            elif _trova(w, contesto):
                nella_tavola += 1
            elif _trova(w, contorno_c):
                nel_contorno += 1
        peso_ctx = PESO_CONTESTO_MINUTERIA if c in MINUTERIA else PESO_CONTESTO
        punti += PESO_NOME * nel_nome + peso_ctx * (nella_tavola + nel_contorno)
        if nel_nome:
            if testa and (_trova(testa, nome_desc) or _trova(testa, nome_c)):
                punti += BONUS_TESTA
            # "termostato" prima di "termostato aria condizionata"
            in_piu = sum(1 for d in nome_desc if not any(_trova(w, {d}) for w in cercate))
            punti -= min(MAX_PENALITA, PENALITA_PAROLA_IN_PIU * in_piu)
        if misure and _sottosequenza(misure, num):
            punti += BONUS_MISURE
        if punti <= 0:
            continue
        r = dict(r)
        r["rilevanza"] = punti
        r["pertinenza_tavola"] = nella_tavola
        r["trovato_come"] = CONCETTI[c][0] if via_sinonimo and c else None
        trovate.append(r)

    trovate.sort(key=lambda r: (-r["rilevanza"], -r["pertinenza_tavola"],
                                r["revision_id"], r["position"] or ""))
    return trovate[:limit]


# ------------------------------------------------------- manutenzione
def tipi_senza_regola(db):
    """Tipi del catalogo in cache che nessuna regola copre, dal piu' frequente."""
    cnt = Counter()
    for (d,) in db.query("SELECT description FROM part"):
        t = tipo_di(d)
        if t and not concetto_di(t):
            cnt[t] += 1
    return cnt.most_common()


if __name__ == "__main__":
    from .client import BRANDS
    from .db import Db

    sys.stdout.reconfigure(encoding="utf-8")
    db = Db()
    if sys.argv[1:2] == ["--senza-regola"]:
        for t, n in tipi_senza_regola(db):
            print(f"{n:5d}  {t}")
    else:
        brand, model_id, testo = sys.argv[1], int(sys.argv[2]), " ".join(sys.argv[3:])
        for r in cerca(db, BRANDS[brand.upper()], model_id, testo, limit=15):
            print(f"{r['rilevanza']:5.1f}  {r['code']:18} {r['description'][:40]:40} "
                  f"{(r['trovato_come'] or '')[:25]:25} | {r['subgroup_name']} / {r['drawing_name']}")
