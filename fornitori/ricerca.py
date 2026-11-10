"""
Motore di ricerca ricambi COMUNE a tutte le marche.

Nato come fornitori/sdf/ricerca.py (tecnica del bot LANCER, collaudata
dall'azienda su SDF) e reso indipendente dalla marca: lavora su righe in un
formato standard, con il vocabolario di fornitori/arricchimento.py piu'
eventuali regole della marca. Ogni fornitore deve solo:

  1. caricare le righe della macchina dal proprio DB, con queste chiavi
     (le altre colonne passano intatte fino a 'articolo'):
        codice, descrizione, posizione, quantita
        tavola        id della tavola (qualunque valore ordinabile)
        gruppo, sottogruppo, nome_tavola   -> il "contesto" della riga
        nota_tavola   nota della tavola (facoltativa)
        validita      validita' della tavola, es. intervallo di matricola
                      (facoltativa: le tavole uguali ma con validita'
                      diverse vengono unite in un blocco solo)
  2. dire come trasformare una riga in articolo (prezzo, stato, sostituti,
     matricole... sono campi suoi).

Uso tipico in un adapter:

    preparate = ricerca.CATALOGHI.prepara(("marca", id_macchina),
                                          lambda: carica_righe(), VOCABOLARIO)
    trovate = ricerca.cerca(preparate, testo, VOCABOLARIO)
    esito = ricerca.componi_blocchi(trovate, righe_tavola, articolo)

COME FUNZIONA LA RICERCA (cerca)

1. Nessuna parola e' obbligatoria: "il coso dove metti le chiavi" o
   "paraolio albero frizione" (il pezzo si chiama "anello tenuta") con una
   LIKE su ogni parola davano zero risultati. Qui ogni riga prende un
   punteggio e si ordina.

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

Ogni riga trovata riceve "rilevanza", "pertinenza_tavola" e "trovato_come":
il nome del concetto quando il pezzo e' stato trovato grazie a un sinonimo
e non alla sua descrizione, cosi' il bot puo' dichiarare l'interpretazione
("cercato come paraolio, a catalogo e' anello tenuta").
"""
import re
import threading
from collections import Counter, OrderedDict

from . import arricchimento as _comune
from .testo import FERMA, radice, senza_accenti

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


def _testa(frase):
    """Prima parola significativa di un sinonimo: 'tubo del radiatore' -> 'tub'."""
    for w in re.findall(r"[a-z0-9]+", _normalizza(frase)):
        if not w.isdigit() and len(w) >= 2 and w not in _VUOTE:
            return radice(w)
    return None


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


def contesto_di(r):
    """'GRUPPO / SOTTOGRUPPO / TAVOLA' di una riga, senza le parti vuote."""
    return " / ".join(x for x in (r.get("gruppo"), r.get("sottogruppo"),
                                  r.get("nome_tavola")) if x)


# ---------------------------------------------------------- vocabolario
class Vocabolario:
    """Vocabolario comune (fornitori/arricchimento.py) piu' le aggiunte di
    una marca. Le REGOLE della marca vengono provate PRIMA di quelle comuni
    (vince la prima che corrisponde), i CONTESTI si sommano. Tiene le
    proprie cache: due marche con regole diverse non si confondono."""

    def __init__(self, regole=(), contesti=(), concetti=None, minuteria=()):
        self.concetti = {**_comune.CONCETTI, **(concetti or {})}
        self._regole = [(re.compile(r), c)
                        for r, c in list(regole) + list(_comune.REGOLE)]
        self._contesti = [(re.compile(r, re.I), p)
                          for r, p in list(contesti) + list(_comune.CONTESTI)]
        self.minuteria = set(_comune.MINUTERIA) | set(minuteria)
        self._cache_concetto = {}
        self._cache_parole_concetto = {}
        self._cache_parole_contesto = {}

    def concetto_di(self, tipo):
        if tipo not in self._cache_concetto:
            self._cache_concetto[tipo] = next(
                (c for rx, c in self._regole if rx.search(tipo)), None)
        return self._cache_concetto[tipo]

    def parole_concetto(self, c):
        """(parole che NOMINANO il pezzo, parole di contorno) di un concetto.

        Di ogni sinonimo conta solo la prima parola: in "tubo del radiatore"
        (sinonimo di manicotto in gomma) "radiatore" dice DOVE sta quel tubo,
        non cos'e', e non vale per tutti i manicotti. Contandola, "radiatore"
        metteva i manicotti davanti al radiatore e "pomello del cambio" dava
        "cambio" anche ai pomoli del distributore. Le parole di contorno valide
        per TUTTO il concetto stanno in "evoca". Il nome tecnico (nome_it)
        conta invece per intero."""
        if c not in self._cache_parole_concetto:
            nome, sinonimi, siciliano, evoca = self.concetti[c]
            frasi = [f for f in f"{sinonimi};{siciliano}".split(";") if f.strip()]
            nomi = parole(nome) | {t for t in map(_testa, frasi) if t}
            self._cache_parole_concetto[c] = (nomi, parole(evoca) - nomi)
        return self._cache_parole_concetto[c]

    def parole_contesto(self, contesto):
        """(parole scritte nel gruppo / sottogruppo / tavola, parole aggiunte da
        CONTESTI). Separate perche' negli spareggi conta chi e' scritto davvero
        nel catalogo: "fermi dell'albero primario" deve preferire la tavola
        ALBERO PRIMARIO a ALBERO ENTRATA CAMBIO (dove "primario" e' solo
        aggiunto da CONTESTI)."""
        if contesto not in self._cache_parole_contesto:
            scritte = parole(contesto)
            extra = parole(" ".join(p for rx, p in self._contesti if rx.search(contesto)))
            self._cache_parole_contesto[contesto] = (scritte, extra - scritte)
        return self._cache_parole_contesto[contesto]

    def tipi_senza_regola(self, descrizioni):
        """Tipi che nessuna regola copre, dal piu' frequente: da qui si
        scrivono le regole nuove (comuni o della marca)."""
        cnt = Counter()
        for d in descrizioni:
            t = tipo_di(d)
            if t and not self.concetto_di(t):
                cnt[t] += 1
        return cnt.most_common()


COMUNE = Vocabolario()


# ------------------------------------------------ catalogo della macchina
def prepara(righe, vocabolario=COMUNE):
    """Calcola una volta per tutte, per ogni riga, le parole di nome e di
    contesto. 'righe': dict (o sqlite3.Row) nel formato standard."""
    out = []
    for r in righe:
        r = dict(r)
        c = vocabolario.concetto_di(tipo_di(r["descrizione"]))
        nome_c, contorno_c = vocabolario.parole_concetto(c) if c else (set(), set())
        tavola, extra = vocabolario.parole_contesto(contesto_di(r))
        out.append((r, parole(r["descrizione"]), nome_c, tavola, extra | contorno_c,
                    c, numeri(r["descrizione"])))
    return out


class _CachePreparati:
    """Cataloghi preparati, tenuti in memoria per poche macchine (LRU).

    Va usata solo per cataloghi COMPLETI (che non cambiano piu'): e'
    condivisa tra le sessioni ma di sola lettura, come l'indice di
    lancer_db/ricerca.py. La chiave la sceglie il fornitore e deve
    contenere la marca, es. ("sdf", "SAME", 11572)."""

    def __init__(self, massimo=20):
        self.massimo = massimo
        self._dati = OrderedDict()
        self._lk = threading.Lock()

    def prepara(self, chiave, carica_righe, vocabolario=COMUNE):
        with self._lk:
            if chiave in self._dati:
                self._dati.move_to_end(chiave)
                return self._dati[chiave]
        preparate = prepara(carica_righe(), vocabolario)
        with self._lk:
            self._dati[chiave] = preparate
            while len(self._dati) > self.massimo:
                self._dati.popitem(last=False)
        return preparate


CATALOGHI = _CachePreparati()


# ------------------------------------------------------------ ricerca
def cerca(preparate, testo, vocabolario=COMUNE, limit=60):
    """Righe del catalogo preparato ordinate per pertinenza alla richiesta,
    con in piu' 'rilevanza', 'pertinenza_tavola' e 'trovato_come'."""
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
    for r, nome_desc, nome_c, contesto, contorno_c, c, num in preparate:
        punti = 0.0
        nel_nome = nella_tavola = nel_contorno = 0
        via_sinonimo = False
        if codici and any(k in r["codice"].lower() for k in codici):
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
        peso_ctx = PESO_CONTESTO_MINUTERIA if c in vocabolario.minuteria else PESO_CONTESTO
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
        r["trovato_come"] = vocabolario.concetti[c][0] if via_sinonimo and c else None
        trovate.append(r)

    trovate.sort(key=lambda r: (-r["rilevanza"], -r["pertinenza_tavola"],
                                r["tavola"], r["posizione"] or ""))
    return trovate[:limit]


# ------------------------------------------------- composizione risposta
# A differenza del numero di righe extra per blocco (dove tagliare aiuta,
# si e' visto che riduce solo rumore), tagliare il NUMERO di blocchi e'
# rischioso: il punteggio mette spesso a pari merito una tavola pertinente
# e una che ha agganciato la ricerca solo di striscio, quindi l'ordine tra
# i blocchi non e' affidabile quanto sembra. Si e' visto in sessione (su
# SDF) un caso reale con la tavola giusta ottava su nove: un tetto a 8
# l'avrebbe esclusa. Restava gia' a 8 nella versione precedente - lo
# teniamo alto.
MAX_BLOCCHI = 8
MAX_EXTRA_PER_BLOCCO = 15
MAX_TROVATI_PER_BLOCCO = 10


def _numero_posizione(posizione):
    m = re.match(r"\d+", posizione or "")
    return int(m.group()) if m else None


def pezzi_vicini(righe_tavola, trovati, codici_trovati, massimo=MAX_EXTRA_PER_BLOCCO):
    """Righe della stessa tavola non agganciate dalla ricerca testuale,
    ordinate per vicinanza di posizione ai pezzi trovati (numero di
    posizione principale, es. '11.2' -> 11) e limitate a 'massimo': una via
    di mezzo tra 'solo la stessa posizione esatta' (troppo stretto) e
    'tutta la tavola' (troppo rumore su tavole grandi)."""
    posizioni_trovate = {n for n in
                         (_numero_posizione(r["posizione"]) for r in trovati)
                         if n is not None}
    extra = [r for r in righe_tavola if r["codice"] not in codici_trovati]
    if posizioni_trovate:
        def distanza(r):
            n = _numero_posizione(r["posizione"])
            if n is None:
                return 10 ** 9
            return min(abs(n - pt) for pt in posizioni_trovate)
        extra.sort(key=distanza)
    return extra[:massimo]


def componi_blocchi(trovate, righe_tavola, articolo, max_blocchi=MAX_BLOCCHI):
    """Dalle righe trovate ai 'blocchi' della risposta (uno per tavola).

    righe_tavola(id_tavola) -> tutte le righe di quella tavola (formato
                               standard), per i pezzi vicini
    articolo(riga)          -> dict dell'articolo, coi campi della marca

    Restituisce {"blocchi": [...], "totale_codici": n}.
    """
    per_tavola = {}
    for r in trovate:
        per_tavola.setdefault(r["tavola"], []).append(r)

    # Piu' tavole con lo stesso contesto e gli stessi pezzi trovati (cambia
    # solo la validita', es. il range di telaio) valgono un blocco solo: si
    # tiene la prima e se ne uniscono le validita' nella nota.
    unite = {}
    for tav, elenco in per_tavola.items():
        p = elenco[0]
        chiave = (p.get("gruppo"), p.get("sottogruppo"), p.get("nome_tavola"),
                  p.get("nota_tavola"),
                  tuple(sorted((r["codice"], r["posizione"], r["quantita"]) for r in elenco)))
        if chiave in unite:
            unite[chiave]["validita"].append(p.get("validita"))
        else:
            unite[chiave] = {"tavola": tav, "elenco": elenco,
                             "validita": [p.get("validita")]}

    # A pari rilevanza/pertinenza (frequente: sono punteggi grezzi),
    # l'ordine tra tavole NON puo' dipendere dall'id interno della tavola:
    # non ha alcun legame con quanto la tavola sia pertinente, ed e' quello
    # che faceva restare fuori dal tetto MAX_BLOCCHI tavole importanti per
    # puro caso (su SDF: FRENI ANTERIORI scartata a favore di una valvola
    # dell'impianto frenatura, a parita' di punteggio). Terzo criterio:
    # quante righe di QUESTA tavola la ricerca ha agganciato (una tavola
    # dove rispondono 2 pezzi diversi e' piu' centrale di una dove ne
    # risponde 1). Ultimo pareggio: nome tavola, cosi' l'ordine e' sempre
    # riproducibile e spiegabile, mai arbitrario.
    def punteggio_tavola(u):
        el = u["elenco"]
        return (max(r["rilevanza"] for r in el),
                max(r["pertinenza_tavola"] for r in el),
                len(el),
                el[0].get("nome_tavola") or "")

    ordinate = sorted(unite.values(), key=punteggio_tavola, reverse=True)

    blocchi = []
    for u in ordinate[:max_blocchi]:
        elenco = u["elenco"]
        p = elenco[0]
        validita = ", ".join(x for x in u["validita"] if x)
        nota = " - ".join(x for x in (p.get("nota_tavola"), validita) if x)

        # Il testo cercato puo' agganciare solo alcune righe della tavola:
        # le altre righe vicine per posizione sono spesso altri componenti
        # dello stesso gruppo meccanico (es. le altre tenute di uno stesso
        # gruppo mozzo-disco-pistone), utili se il cliente sta revisionando
        # l'intero gruppo. Si e' visto in sessione che lasciare all'LLM il
        # compito di NOTARE da solo questi pezzi dentro una lista JSON e'
        # inaffidabile (stesso identico input, a volte li usa a volte no) -
        # quindi li segnaliamo gia' pronti, e lasciamo al modello solo la
        # decisione se e come usarli nella risposta.
        trovati = elenco[:MAX_TROVATI_PER_BLOCCO]
        codici_trovati = {r["codice"] for r in trovati}
        extra = pezzi_vicini(righe_tavola(u["tavola"]), trovati, codici_trovati)

        blocchi.append({
            "contesto": contesto_di(p),
            "nota": nota or "",
            "articoli": [articolo(a) for a in trovati],
            "nota_correlati": [{"codice": a["codice"],
                                "descrizione": a["descrizione"] or "",
                                "posizione": a["posizione"] or ""}
                               for a in extra],
        })

    return {"blocchi": blocchi,
            "totale_codici": len({r["codice"] for r in trovate})}
