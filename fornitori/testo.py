"""
Normalizzazione del testo per la ricerca nei cataloghi ricambi.

Vive in fornitori/ e non dentro una singola marca perche' il problema e'
identico ovunque: il cliente scrive al plurale o con accenti, il catalogo
usa il singolare, e una LIKE letterale non trova niente.

Due trasformazioni, entrambe volutamente grossolane:

1. RADICE ITALIANA
   Il catalogo scrive "disco freno" ma la tavola si chiama "FRENI
   POSTERIORI": senza unificare singolare e plurale, cercare "freni
   posteriori" non trova il disco. Si taglia la vocale finale (e la 'h'
   che resta nei plurali tipo dischi -> disch -> disc).

   Non e' uno stemmer linguistico e non vuole esserlo: serve solo a far
   collidere le due forme dentro una LIKE. Sovra-genera un po' ("vite" e
   "viti" diventano "vit", che pesca anche "vitone"), ma nella ricerca
   ricambi un falso positivo in fondo alla lista costa molto meno di un
   pezzo che il cliente non trova.

2. ACCENTI
   "idraulico"/"idràulico", "però"/"pero": si riducono alla forma senza
   segni diacritici prima del confronto.
"""
import re
import unicodedata
from difflib import SequenceMatcher
import csv
import config

VOCALI = "aeiou"
LUNGHEZZA_MINIMA = 4        # sotto questa soglia la parola resta intatta

# parole che non aiutano a discriminare un ricambio
FERMA = {
    "il", "lo", "la", "i", "gli", "le", "un", "uno", "una",
    "di", "del", "della", "dello", "dei", "delle", "degli",
    "da", "dal", "dalla", "in", "nel", "nella", "con", "per", "su",
    "e", "ed", "o", "che", "mi", "serve", "vorrei", "cerco", "cercare",
    "avere", "trovare", "pezzo", "ricambio", "ricambi", "codice",
    # forme elise: "dell'olio" viene spezzato dall'apostrofo in dell + olio
    "dell", "all", "nell", "sull", "dall", "quell", "l", "un", "d", "c",
}

_SINONIMI = None


def senza_accenti(testo):
    n = unicodedata.normalize("NFKD", testo or "")
    return "".join(c for c in n if not unicodedata.combining(c))


def radice(parola):
    """'freni' -> 'fren', 'dischi' -> 'disc', 'kit' -> 'kit'."""
    w = senza_accenti(parola).lower()
    if len(w) >= LUNGHEZZA_MINIMA and w[-1] in VOCALI:
        w = w[:-1]
    if len(w) >= LUNGHEZZA_MINIMA and w.endswith("h"):
        w = w[:-1]
    return w


def parole_chiave(testo, togli_ferma=True, massimo=6):
    """
    'mi serve il filtro dell'olio' -> ['filtr', 'oli']

    Le parole con cifre o punti (i codici ricambio: '2.0112.635.2') non
    vengono ridotte: li' ogni carattere conta.
    """
    grezze = re.split(r"[^0-9A-Za-zÀ-ÿ./-]+", testo or "")
    out = []
    for w in grezze:
        if not w:
            continue
        base = senza_accenti(w).lower()
        if togli_ferma and base in FERMA:
            continue
        if len(base) < 2:
            continue
        # sembra un codice: lascialo com'e'
        if any(ch.isdigit() for ch in base):
            out.append(base)
        else:
            out.append(radice(base))
        if len(out) >= massimo:
            break
    return out


# -------------------------------------------------------------------
# Ricerca approssimata: refusi di battitura su nomi macchina/modello
# ("Goldni" -> "Goldoni", "Argom 65" -> "Argon 65").
#
# Usa difflib.SequenceMatcher (libreria standard, nessuna dipendenza
# esterna) invece di implementare a mano la distanza di Levenshtein:
# per confrontare nomi/modelli di poche parole la differenza di
# precisione e' irrilevante, e SequenceMatcher tollera meglio anche
# blocchi di caratteri spostati, non solo lettere singole sbagliate.
# -------------------------------------------------------------------

def _somiglianza(a, b):
    return SequenceMatcher(None, senza_accenti(a).lower(),
                           senza_accenti(b).lower()).ratio()


def piu_simili(query, candidati, soglia=0.72, massimo=5, chiave=None):
    """
    Restituisce i candidati piu' simili a 'query', ordinati per somiglianza
    decrescente, scartando quelli sotto 'soglia' (0-1). 'chiave' estrae il
    testo da confrontare se 'candidati' non e' una lista di stringhe.

    Non e' pensata per sostituire una ricerca esatta/normalizzata: va usata
    solo come fallback quando quella non trova nulla, perche' puo' produrre
    falsi positivi su nomi brevi o molto simili tra loro.
    """
    testo = lambda c: chiave(c) if chiave else c
    valutati = [(c, _somiglianza(query, testo(c))) for c in candidati]
    valutati = [(c, p) for c, p in valutati if p >= soglia]
    valutati.sort(key=lambda cp: -cp[1])
    return [c for c, _ in valutati[:massimo]]

def _carica_sinonimi():
    global _SINONIMI
    if _SINONIMI is not None:
        return _SINONIMI
    path = config.DATI / "sinonimi.csv"
    coppie = []

    with open(path, encoding="utf-8-sig", newline="") as f:
        sinonimi = csv.reader(f, delimiter=';')
        next(sinonimi)
        for riga in sinonimi:
            corretto, impropri = riga[0], riga[1]

            for improprio in impropri.split(";"):
                coppie.append((improprio.strip(), corretto.strip()))

    _SINONIMI = coppie
    return _SINONIMI

def correggi_termine(query):
    coppie = _carica_sinonimi()
    query_norm = senza_accenti(query).lower().strip()

    # TODO fase 1: match diretto — confronta query_norm con senza_accenti(improprio).lower()
    #   per ogni (improprio, preciso) in coppie; se uguali, return preciso
    for (improprio, corretto) in coppie:
        if query_norm == senza_accenti(improprio).lower():
            return corretto

    # Fallback fuzzy: soglia alta (non i 0.72 usati per i refusi sui nomi
    # macchina) perche' qui confrontiamo parole intere di significato
    # diverso, non variazioni di battitura dello stesso nome - con 0.72
    # "acceleratore" risultava piu' simile a "accumulatore" (0.75) che a
    # se stesso, e veniva silenziosamente cercato come "batteria".
    ris = piu_simili(query, coppie, soglia=0.85, chiave=lambda c: c[0])
    if ris: return ris[0][1]

    return query  # nessun match: query originale, invariata
