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
