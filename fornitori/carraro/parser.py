"""
Estrazione dati dalle pagine HTML del portale Antonio Carraro.

Funzioni pure: ricevono l'HTML (scaricato da client.py) e restituiscono
dict/liste Python. Nessuna rete, nessun DB: si collaudano sui file
salvati, senza toccare il sito.

Pagine gestite:
    parse_categoria        /catalogo/serie/...(/)   sottocategorie e prodotti
    parse_modello          pagina di un modello     tavole con gruppo e matricole
    parse_tavola           pagina di una tavola     righe ricambi
    parse_scheda_codice    /<codice>                codici precedenti, tavole e modelli che lo usano
    parse_ricerca_codici   ricerca 'codice'         codice, descrizione, codici precedenti
    parse_ricerca_modelli  ricerca 'modello'        come i prodotti di una categoria
    parse_ricerca_matricola ricerca 'matricola'     modelli con quella matricola
    parse_ricerca_tavole   ricerca 'tavola'         tavole per titolo

Matricole: sul sito compaiono come progressivo a 5 cifre ("00906") con
una freccia. Freccia DOPO il numero = "da quel numero in poi", freccia
PRIMA = "fino a quel numero", numeri ai due lati = intervallo. Le
rappresentiamo sempre come {"da": str|None, "a": str|None} (stringhe,
per non perdere gli zeri iniziali); in colonna matricola possono esserci
anche sigle (es. "mt" = a metraggio, "ar"), che finiscono in "nota".
"""

import re
from urllib.parse import urlsplit

from bs4 import BeautifulSoup, NavigableString

NON_FORNIBILE = "00000000"
_FRECCIA = re.compile(r"\s*(?:→|->)\s*")


def _soup(html):
    return BeautifulSoup(html, "lxml")


def _testo(el):
    if el is None:
        return ""
    return re.sub(r"\s+", " ", el.get_text(" ", strip=True)).strip()


def _percorso(url):
    """Solo il percorso, senza dominio ne' query (?validateserialnumber=...)."""
    if not url:
        return ""
    p = urlsplit(url).path
    return p if p.startswith("/") else "/" + p


def _intervallo(testo):
    """'sn: 00001→00905' / '(-> 15-03909)' / '15-15275->' -> {'da','a'}.
    None se nel testo non c'e' nessuna freccia."""
    if not testo:
        return None
    t = re.sub(r"^\s*sn:\s*", "", testo.strip(" ()"), flags=re.I)
    if not _FRECCIA.search(t):
        return None
    da, a = _FRECCIA.split(t, maxsplit=1)
    return {"da": da.strip() or None, "a": a.strip() or None}


def _intervallo_nel_nome(nome):
    """'Tigre 2000 (-> 15-09599)' -> ('Tigre 2000', {'da': None, 'a': '15-09599'}).
    Il nome restituito e' pulito dalla parentesi delle matricole."""
    for m in re.finditer(r"\(([^()]*(?:→|->)[^()]*)\)", nome):
        inter = _intervallo(m.group(1))
        if inter:
            pulito = (nome[:m.start()] + nome[m.end():]).strip()
            return re.sub(r"\s+", " ", pulito), inter
    return nome, None


def numero_pagine(html_o_soup):
    """Numero di pagine dei risultati (1 se non c'e' paginazione)."""
    s = html_o_soup if hasattr(html_o_soup, "select") else _soup(html_o_soup)
    numeri = [int(t) for t in (_testo(li) for li in s.select(".pagination li"))
              if t.isdigit()]
    return max(numeri) if numeri else 1


def link_pagine(html_o_soup):
    """Percorsi delle altre pagine di risultati linkate dalla paginazione
    (senza duplicati). Per le categorie il formato degli URL di pagina non
    e' noto a priori: si seguono i link che il sito stesso mette."""
    s = html_o_soup if hasattr(html_o_soup, "select") else _soup(html_o_soup)
    out = []
    for a in s.select(".pagination a[href]"):
        p = a["href"]
        if p.startswith("javascript") or p == "#":
            continue
        p = urlsplit(p).path + ("?" + urlsplit(p).query if urlsplit(p).query else "")
        if p not in out:
            out.append(p)
    return out


def _risultati(s):
    """Il contenitore dei risultati: esclude header, lista desideri, ecc."""
    return s.select_one("section.result_list") or s.select_one("#mainwrapper") or s


# ----------------------------------------------------------------------
# Navigazione del catalogo
# ----------------------------------------------------------------------
def _prodotti(contenitore):
    """Riquadri <article> dei prodotti (modelli, kit, attrezzi)."""
    out = []
    for art in contenitore.select("article.prodotto_item"):
        link = art.select_one(".product-actions a[href]") or art.select_one("a[href]")
        if not link:
            continue
        nc = art.select_one(".nome-categoria")
        # dentro .nome-categoria: "<nome visibile><br/><slug>"
        pezzi = [t.strip() for t in nc.stripped_strings] if nc else []
        nome_grezzo = pezzi[0] if pezzi else ""
        slug = pezzi[-1] if len(pezzi) > 1 else _percorso(link["href"]).rstrip("/").rsplit("/", 1)[-1]
        nome, matricole = _intervallo_nel_nome(nome_grezzo)
        out.append({
            "nome": nome,
            "nome_completo": nome_grezzo,
            "slug": slug,
            "url": _percorso(link["href"]),
            "matricole": matricole,
            # riquadri che non sono macchine ma raccolte di kit/attrezzi
            "tipo": ("kit" if slug.startswith("kits-")
                     else "attrezzi" if slug.startswith("attrezzi-")
                     else "modello"),
        })
    return out


def parse_categoria(html):
    """Pagina di una categoria del catalogo.

    Le categorie intermedie mostrano pannelli di sottocategorie; quelle
    finali mostrano i riquadri dei prodotti. Una pagina puo' avere
    entrambe le cose: si restituisce tutto quello che c'e'.
    """
    s = _soup(html)
    sotto, visti = [], set()
    for pan in s.select("section.category_list .panel-schema"):
        # il titolo puo' essere un percorso "PADRE → FIGLIA": conta l'ultima
        titoli = pan.select(".panel-heading a[href]")
        if not titoli:
            continue
        a = titoli[-1]
        url = _percorso(a["href"])
        if url in visti:
            continue
        visti.add(url)
        tutti = pan.select_one("a.btn[href]")
        m = re.search(r"\((\d+)\)", _testo(tutti)) if tutti else None
        sotto.append({"nome": _testo(a), "url": url,
                      "n_prodotti": int(m.group(1)) if m else None})
    return {
        "titolo": _titolo_pagina(s),
        "sottocategorie": sotto,
        "prodotti": _prodotti(_risultati(s)),
        "pagine": numero_pagine(s),
    }


def _titolo_pagina(s):
    t = _testo(s.title)
    return re.sub(r"\s*-\s*tQuadra\s*$", "", t)


# ----------------------------------------------------------------------
# Modello -> tavole
# ----------------------------------------------------------------------
def _voce_tavola(li):
    """<li class='go_to_schema'> della lista tavole (pagina modello o scheda codice)."""
    return {
        "codice": _testo(li.select_one(".schema-title")),
        "titolo": _testo(li.select_one(".schema-subtitle")),
        "descrizione": _testo(li.select_one(".long-description")),
        "matricole": _intervallo(_testo(li.select_one(".schema-description .schema-serialdata"))),
        "url": _percorso(li.get("data-schemaurl", "")),
    }


def parse_modello(html):
    """Pagina di un modello: nome e tavole raggruppate per gruppo
    (A - MOTORE, B - AUTOTELAIO...). Le tavole sono tutte nell'HTML,
    la fisarmonica dei gruppi e' solo grafica."""
    s = _soup(html)
    h2 = s.select_one("h2")
    slug = _testo(h2.select_one(".label")) if h2 else ""
    nome = _testo(h2)
    if slug and nome.startswith(slug):
        nome = nome[len(slug):].strip()

    tavole = []
    for pan in s.select("#schemes-accordion > .panel"):
        titolo = pan.select_one(".panel-title a")
        gruppo = _testo(titolo)
        badge = _testo(titolo.select_one(".badge")) if titolo else ""
        if badge and gruppo.endswith(badge):
            gruppo = gruppo[: -len(badge)].strip()
        for li in pan.select("li.go_to_schema"):
            voce = _voce_tavola(li)
            voce["gruppo"] = gruppo
            tavole.append(voce)
    return {"nome": nome or _titolo_pagina(s), "slug": slug, "tavole": tavole}


# ----------------------------------------------------------------------
# Tavola -> righe
# ----------------------------------------------------------------------
def _matricola_cella(td):
    """Cella 'Matricola' di una riga. La direzione sta nella POSIZIONE
    dell'icona freccia rispetto al numero, non nel testo."""
    if td is None:
        return None, ""
    pezzi = []
    for nodo in td.descendants:
        if isinstance(nodo, NavigableString):
            t = nodo.strip()
            if t:
                pezzi.append(t)
        elif "arrow-right" in (nodo.get("class") or []) or "right-arrow" in (nodo.get("class") or []):
            pezzi.append("→")
    testo = " ".join(pezzi)
    if "→" not in testo:
        return None, testo            # sigla (mt, ar, ...) o vuoto
    return _intervallo(testo), ""


def _intero(testo):
    m = re.search(r"\d+", testo or "")
    return int(m.group()) if m else None


def parse_tavola(html):
    """Pagina di una tavola: intestazione e righe della distinta."""
    s = _soup(html)
    # intestazione vera: <div id="schema-title">. Altri .schema-title nella
    # pagina appartengono all'elenco "altri prodotti" nella colonna laterale
    intestazione = s.select_one("#schema-title") or s
    codice = _testo(intestazione.select_one(".schema-title"))
    titolo = _testo(intestazione.select_one("h2"))
    etichetta = intestazione.select_one("#schema_validity_label")

    # modelli (e raccolte kit) che usano questa stessa tavola
    modelli = []
    for fig in s.select("#otherproducts figure.schemafigure"):
        modelli.append({"slug": _testo(fig.select_one(".schema-title")),
                        "nome": _testo(fig.select_one(".schema-subtitle"))})

    righe = []
    for tr in s.select("#spare_parts_table tr"):
        cod = tr.select_one("td.colcodice")
        if cod is None:
            continue                      # intestazione
        codice_ric = _testo(cod)
        matricole, nota = _matricola_cella(tr.select_one("td.colserials"))
        righe.append({
            "posizione": _testo(tr.select_one("td.colnumero")),
            "codice": codice_ric,
            "descrizione": _testo(tr.select_one("td.coltitolo")),
            "matricole": matricole,
            "nota": nota,
            "info": _testo(tr.select_one("td.extra")),
            "quantita": _intero(_testo(tr.select_one(".colminqta"))),
            "fornibile": codice_ric != NON_FORNIBILE,
        })
    return {
        "codice": codice,
        "titolo": titolo,
        "matricole": _intervallo(_testo(etichetta)),
        "righe": righe,
        "modelli": modelli,
    }


# ----------------------------------------------------------------------
# Scheda di un codice ricambio
# ----------------------------------------------------------------------
def _codici_precedenti(testo):
    return [c.strip() for c in re.split(r"[,;\s]+", testo or "") if c.strip()]


def parse_scheda_codice(html):
    """Pagina /<codice>: descrizione, codici precedenti (sostituiti da
    questo) e tavole in cui compare, con i modelli e le matricole per
    ciascuna. L'elenco dei modelli per tavola usa gli slug del sito
    (es. '100-tgf-tgf-con-arco'), non i nomi leggibili."""
    s = _soup(html)
    h2 = s.select_one("h2")
    codice = _testo(h2.select_one(".label")) if h2 else ""
    descrizione = _testo(h2)
    if codice and descrizione.startswith(codice):
        descrizione = descrizione[len(codice):].strip()

    prec = ""
    for small in s.find_all("small"):
        t = _testo(small)
        if t.lower().startswith("codice precedente"):
            prec = t.split(":", 1)[-1]
            break

    tavole = []
    for li in s.select("li.go_to_schema"):
        voce = _voce_tavola(li)
        voce["url"] = "/" + voce["codice"] if voce["codice"] else voce["url"]
        voce["modelli"] = []
        for m in li.select(".model-list li"):
            sn = m.select_one(".schema-serialdata")
            voce["modelli"].append({
                "slug": _testo(m).replace(_testo(sn), "", 1).strip() if sn else _testo(m),
                "matricole": _intervallo(_testo(sn)),
            })
        tavole.append(voce)
    return {"codice": codice, "descrizione": descrizione,
            "codici_precedenti": _codici_precedenti(prec), "tavole": tavole}


# ----------------------------------------------------------------------
# Ricerche
# ----------------------------------------------------------------------
def _tabella_risultati(s, intestazione):
    """Tabella dei risultati che contiene una certa colonna."""
    for t in _risultati(s).select("table"):
        if intestazione in [_testo(th) for th in t.select("th")]:
            return t
    return None


def parse_ricerca_codici(html):
    s = _soup(html)
    t = _tabella_risultati(s, "Codice")
    out = []
    for tr in (t.select("tr") if t else []):
        celle = tr.select("td")
        if len(celle) < 3:
            continue
        out.append({"codice": _testo(celle[0]), "descrizione": _testo(celle[1]),
                    "codici_precedenti": _codici_precedenti(_testo(celle[2]))})
    return {"risultati": out, "pagine": numero_pagine(s)}


def parse_ricerca_modelli(html):
    s = _soup(html)
    return {"risultati": _prodotti(_risultati(s)), "pagine": numero_pagine(s)}


def parse_ricerca_matricola(html):
    """Ogni riga: un modello compatibile con la matricola cercata. Il link
    porta alla pagina del modello gia' filtrata (?validateserialnumber=)."""
    s = _soup(html)
    t = _tabella_risultati(s, "Matricola")
    out = []
    for tr in (t.select("tr") if t else []):
        celle = tr.select("td")
        if len(celle) < 4:
            continue
        a = tr.select_one("a[href]")
        serie, intervallo = _intervallo_nel_nome(_testo(celle[0]))
        sn = None
        if a:
            m = re.search(r"validateserialnumber=([^&]+)", a["href"])
            sn = m.group(1) if m else None
        out.append({
            "serie": serie,
            "matricole_serie": intervallo,
            "codice_macchina": _testo(celle[1]),
            "matricola": _testo(celle[2]),
            "modello": _testo(celle[3]),
            "url": _percorso(a["href"]) if a else "",
            "sn": sn,                         # progressivo a 5 cifre usato dal sito
        })
    return {"risultati": out, "pagine": numero_pagine(s)}


def parse_ricerca_tavole(html):
    s = _soup(html)
    t = _tabella_risultati(s, "Gruppo")
    out = []
    for tr in (t.select("tr") if t else []):
        celle = tr.select("td")
        if len(celle) < 4:
            continue
        out.append({"codice": _testo(celle[1]), "titolo": _testo(celle[2]),
                    "gruppo": _testo(celle[3]), "url": "/" + _testo(celle[1])})
    return {"risultati": out, "pagine": numero_pagine(s)}
