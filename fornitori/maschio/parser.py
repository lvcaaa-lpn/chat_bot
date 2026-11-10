"""
Estrazione dati dalle risposte del portale Maschio Gaspardo.
Solo funzioni pure (testo -> dati), niente rete: vedi client.py.
"""

import json
import re

from bs4 import BeautifulSoup

BASE = "https://work.maschiogaspardo.com"

_RE_PAGINE = re.compile(r"Pagina\s*(\d+)\s*/\s*(\d+)")
_RE_RECORD = re.compile(r"Records?:\s*([\d.]+)")
_RE_RELEASE = re.compile(r"openSelectedRelease\((\d+)\)")
_RE_CARTELLA = re.compile(r"""dir:\s*["']([^"']+)["']""")
_RE_POSIZIONE = re.compile(
    r"""openDialogSVG\(\s*(\d+)\s*,\s*'([^']*)'\s*,\s*(\d+)\s*\)""")
_RE_FOTO = re.compile(r"""openImage\(\s*'(?:[^'\\]|\\.)*'\s*,\s*'([^']+)'""")


def _testo(td):
    return td.get_text(" ", strip=True).replace("\xa0", " ").strip()


def lista_json(testo):
    """Risposte act=lines/categories/models: [{id, des}] -> [{id, nome}]."""
    testo = (testo or "").strip()
    if not testo:
        return []
    return [{"id": str(x["id"]).strip(), "nome": (x.get("des") or "").strip()}
            for x in json.loads(testo)]


def paginazione(html):
    """(pagina corrente, pagine totali, record totali). (1, 1, 0) se la
    risposta non ha la barra di paginazione (nessun risultato)."""
    m = _RE_PAGINE.search(html or "")
    r = _RE_RECORD.search(html or "")
    record = int(r.group(1).replace(".", "")) if r else 0
    if not m:
        return 1, 1, record
    return int(m.group(1)), int(m.group(2)), record


def releases(html):
    """Righe della tabella act=table: [{id, descrizione, codice_doc, data}].
    Colonne: (vuota) | Descrizione | Codice Parte | Release | freccia."""
    out = []
    for tr in BeautifulSoup(html or "", "lxml").find_all("tr"):
        m = _RE_RELEASE.search(str(tr))
        if not m:
            continue
        celle = [_testo(td) for td in tr.find_all("td")]
        # 5 colonne con la prima vuota; senza di essa 4: si conta da destra
        desc, codice, data = (["", "", ""] + celle[:-1])[-3:]
        out.append({"id": int(m.group(1)), "descrizione": desc,
                    "codice_doc": codice, "data": data})
    return out


def viewer(html):
    """Pagina book_parts_expl-view.asp: cartella dei disegni (es.
    'B0D5P0081-2009-03') e tavole [{id, numero, titolo, ordine}]."""
    m = _RE_CARTELLA.search(html or "")
    cartella = m.group(1) if m else None
    tavole = []
    soup = BeautifulSoup(html or "", "lxml")
    for i, div in enumerate(soup.find_all("div", attrs={"tableid": True})):
        etichetta = _testo(div)                    # "009 - Telaio inferiore ..."
        numero, _, resto = etichetta.partition(" - ")
        titolo = (div.get("tabledescription") or resto or etichetta).strip()
        tavole.append({"id": int(div["tableid"]), "numero": numero.strip(),
                       "titolo": titolo, "ordine": i})
    return cartella, tavole


def posizioni_svg(svg):
    """Posizioni cliccabili del disegno: [(prd_id, tab_id, row_id, etichetta)],
    una per row_id (sul disegno lo stesso numero puo' comparire piu' volte)."""
    visti, out = set(), []
    for m in re.finditer(r"<text\b[^>]*>(.*?)</text>", svg or "", re.S):
        p = _RE_POSIZIONE.search(m.group(0))
        if not p:
            continue
        prd, tab, row = int(p.group(1)), p.group(2), int(p.group(3))
        if (tab, row) in visti:
            continue
        visti.add((tab, row))
        etichetta = re.sub(r"<[^>]+>", "", m.group(1)).strip()
        out.append((prd, tab, row, etichetta))
    return out


def anagrafica(html):
    """Righe di una pagina dell'anagrafica ricambi.
    Colonne: foto | azienda | codice | descrizione | rimpiazzato da |
    q.ta' min. | peso (kg) | dettaglio."""
    out = []
    for tr in BeautifulSoup(html or "", "lxml").find_all("tr"):
        tds = tr.find_all("td")
        if len(tds) < 7 or not tr.find("td", class_="wGridRow01"):
            continue
        celle = [_testo(td) for td in tds]
        _, azienda, codice, descr, sostituto, qta, peso = celle[:7]
        if not codice:
            continue
        foto = _RE_FOTO.search(str(tds[0]))
        dismesso = sostituto.upper().startswith("$DISMISSED")
        out.append({
            "codice": codice,
            "azienda": azienda,
            "descrizione": descr,
            "sostituito_da": None if (dismesso or not sostituto) else sostituto,
            "dismesso": int(dismesso),
            "qta_minima": _numero(qta),
            "peso": _numero(peso),
            "url_foto": (BASE + foto.group(1)) if foto else None,
        })
    return out


def _numero(s):
    s = (s or "").strip().replace(".", "").replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None
