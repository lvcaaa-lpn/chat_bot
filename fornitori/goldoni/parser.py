"""
Parser delle pagine del catalogo ricambi Goldoni.

Gestisce DUE formati di pagina tavola:

  vecchio (starqu, tav21580.htm)          nuovo (SERIE Q, 20_02_C.html)
  ---------------------------------------------------------------------
  righe in un'unica cella, separate       vera <table> con <tr>/<td>
  da <br><!-- 21620 -->                   e intestazione POS./MATRIC.
  codice tavola numerico "21620"          alfanumerico "20_02_C"
  niente colonna quantita'                colonna Q.TA'
  indice gruppi con mappa immagine        grupNN.html con <a class="grup">

parse_tavole() sceglie da sola il formato giusto.
"""

import re
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup


BASE = "https://goldoni-ricambi.studioilgranello.com"

# --- riga componente: POS <a>CODICE</a> DESCRIZIONE (note) <br><!-- TAV -->
RE_RIGA = re.compile(
    r'(?P<pos>\d+)\s+'
    r'<a[^>]*CODICE=(?P<codice>[^"&]+)[^>]*>(?P<cod2>[^<]+)</a>\s*'
    r'(?P<resto>.*?)\s*<br\s*/?>\s*<!--\s*(?P<tav>\d+)\s*-->',
    re.I)

# --- riga generica marcata dalla tavola (per note e validita')
RE_RIGA_TAV = re.compile(r'(?P<testo>.*?)<br\s*/?>\s*<!--\s*(?P<tav>\d+)\s*-->', re.S | re.I)

RE_NOTA_MARK = re.compile(r'\((\d+)\)')
RE_NOTA_DEF = re.compile(r'^\s*\((\d+)\)\s*(.*)$')
RE_MODELLI = re.compile(r'MOD\.\s*(.+)', re.I)
RE_MATRICOLA = re.compile(r'\b([A-Z]\d{6})\b')
RE_VAL_FINO = re.compile(r'VALIDO\s+FINO\s+ALLA\s+MACCHINA', re.I)
RE_VAL_DA = re.compile(r'VALIDO\s+DALLA\s+MACCHINA', re.I)
RE_GR_TAV = re.compile(r'GR\s*TAV\s*(\d+)\s+(\d+)\s*([\d.]+)?', re.I)


def pulisci(html):
    """Toglie i tag e normalizza gli spazi."""
    testo = re.sub(r'<[^>]+>', ' ', html)
    testo = testo.replace('&nbsp;', ' ').replace('&amp;', '&')
    return re.sub(r'\s+', ' ', testo).strip()


@dataclass
class Riga:
    posizione: str
    codice: str
    descrizione: str
    note: list = field(default_factory=list)   # ["1", "4"]
    quantita: str = ""                         # solo formato nuovo


@dataclass
class Tavola:
    tavola: str                 # "21620"
    gruppo: str = ""            # "21"
    numero: str = ""            # "620"
    titolo: str = ""
    macchina: str = ""
    modelli_tavola: str = ""
    data: str = ""
    immagine: str = ""
    righe: list = field(default_factory=list)
    note: dict = field(default_factory=dict)   # {"1": "MOD. 65 - 75 ..."}
    validita_da: str = ""
    validita_a: str = ""
    validita_testo: str = ""


# --- estrazione modelli da testi tipo:
#     "Mod. 3450, 3460."
#     "Mod. 65, 75, 85, 100. Mod. John Deere: 1847F, 2447F."
RE_BLOCCO_MOD = re.compile(r'MOD\.\s*([^.]*?)(?=\s*(?:MOD\.|$|\.))', re.I)


def estrai_modelli(testo):
    """Restituisce solo le sigle di modello, scartando marche e parole."""
    modelli = []
    for blocco in RE_BLOCCO_MOD.findall(testo or ""):
        # "John Deere: 1847F" -> tengo solo la parte dopo i due punti
        if ":" in blocco:
            blocco = blocco.split(":", 1)[1]
        for pezzo in re.split(r'[,;/]| - ', blocco):
            p = pezzo.strip(" .,")
            # una sigla di modello contiene almeno una cifra
            if p and re.search(r'\d', p) and len(p) <= 12:
                modelli.append(p)
    visti, out = set(), []
    for m in modelli:
        if m.upper() not in visti:
            visti.add(m.upper())
            out.append(m)
    return out


# -------------------------------------------------------------------
# 1. INDICE DEI CATALOGHI
# -------------------------------------------------------------------
def parse_indice_cataloghi(html, base_url=BASE + "/"):
    """
    Dalla pagina 'Catalogo Ricambi' estrae le pubblicazioni HTML sfogliabili.

    Colonne: serie | codice PBT | validita' da | validita' a | ... | modelli

    Attenzione: nella pagina live i link sono RELATIVI ("cat/starqu/index.htm"),
    mentre in un file salvato dal browser diventano assoluti. Normalizziamo
    tutto con urljoin, altrimenti si perde quasi ogni catalogo.
    """
    from bs4 import BeautifulSoup
    from urllib.parse import urljoin, unquote
    soup = BeautifulSoup(html, "lxml")
    cataloghi = []

    for tr in soup.find_all("tr"):
        link = None
        for a in tr.find_all("a", href=True):
            u = urljoin(base_url, a["href"])
            if "/cat/" in u and u.lower().endswith(("index.htm", "index.html")):
                link = u
                break
        if not link:
            continue

        celle = [td.get_text(" ", strip=True) for td in tr.find_all("td")]
        if len(celle) < 5:
            continue

        codice = unquote(link.split("/cat/")[-1].rsplit("/", 1)[0])

        def val(x):
            """'A......' e '-' significano estremo aperto."""
            x = (x or "").strip()
            return "" if (not x or "." in x or x == "-") else x

        modelli_txt = " ".join(celle[5:]).strip()
        modelli = estrai_modelli(modelli_txt)

        cataloghi.append({
            "codice": codice,
            "url": link,
            "serie": celle[1].strip(),
            "pubblicazione": celle[2].strip(),
            "validita_da": val(celle[3]),
            "validita_a": val(celle[4]),
            "modelli": [m.strip(" ,.") for m in modelli if m.strip(" ,.")],
            "descrizione": " ".join(c for c in celle if c).strip(),
        })

    # deduplica per codice mantenendo il primo
    visti, out = set(), []
    for c in cataloghi:
        if c["codice"] not in visti:
            visti.add(c["codice"])
            out.append(c)
    return out


# -------------------------------------------------------------------
# 2. INDICE DEI GRUPPI
# -------------------------------------------------------------------
def parse_indice_gruppi(html, base_url):
    """
    Raccoglie TUTTI i link navigabili di una pagina.

    Non basta guardare <a href>: questi cataloghi usano anche mappe
    immagine (<area href>) e frameset, e alcuni link non hanno
    estensione .htm. Meglio raccogliere tutto e filtrare a valle.
    """
    from bs4 import BeautifulSoup
    from urllib.parse import urljoin
    soup = BeautifulSoup(html, "lxml")

    grezzi = []
    for tag in soup.find_all(["a", "area"], href=True):
        img = tag.find("img") if tag.name == "a" else None
        grezzi.append((tag["href"], tag.get_text(" ", strip=True),
                       img.get("src") if img else ""))
    for tag in soup.find_all(["frame", "iframe"], src=True):
        grezzi.append((tag["src"], "", ""))

    link, visti = [], set()
    for href, testo, img in grezzi:
        h = href.strip()
        if not h or h.startswith(("javascript:", "mailto:", "#")):
            continue
        url = urljoin(base_url, h).split("#")[0]
        # escludo i formati che non sono pagine navigabili
        if url.lower().endswith((".pdf", ".gif", ".jpg", ".jpeg", ".png",
                                 ".zip", ".exe", ".doc", ".xls")):
            continue
        if url in visti:
            continue
        visti.add(url)
        link.append({"url": url, "etichetta": testo, "immagine": img})
    return link


# -------------------------------------------------------------------
# 3. PAGINA TAVOLA
# -------------------------------------------------------------------
def e_pagina_tavola(html):
    """Riconosce una pagina che contiene un elenco di ricambi (entrambi i formati)."""
    return e_pagina_tavola_vecchia(html) or e_pagina_tavola_nuova(html)


def e_pagina_tavola_vecchia(html):
    return ("POS" in html and "MATRIC" in html and "CODICE=" in html
            and RE_RIGA.search(html) is not None)


def parse_tavole_vecchio(html):
    """
    Estrae tutte le tavole presenti nella pagina.
    Una pagina puo' contenerne piu' d'una: le distinguiamo con il
    codice nel commento HTML di ogni riga.
    """
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "lxml")
    tavole = {}

    # --- righe componente ------------------------------------------
    for m in RE_RIGA.finditer(html):
        tav = m.group("tav")
        t = tavole.setdefault(tav, Tavola(tavola=tav,
                                          gruppo=tav[:2], numero=tav[2:]))
        resto = pulisci(m.group("resto"))
        note = RE_NOTA_MARK.findall(resto)
        descrizione = RE_NOTA_MARK.sub("", resto).strip()
        t.righe.append(Riga(posizione=m.group("pos"),
                            codice=m.group("codice").strip(),
                            descrizione=re.sub(r'\s+', ' ', descrizione),
                            note=note))

    # --- righe non-componente: legenda note e validita' -------------
    for tav, t in tavole.items():
        stato = None          # None | "fino" | "da"
        nota_corrente = None
        for m in RE_RIGA_TAV.finditer(html):
            if m.group("tav") != tav:
                continue
            riga = pulisci(m.group("testo"))
            if not riga or "CODICE=" in m.group("testo"):
                continue

            if RE_VAL_FINO.search(riga):
                stato = "fino"; continue
            if RE_VAL_DA.search(riga):
                stato = "da"; continue

            d = RE_NOTA_DEF.match(riga)
            if d:
                nota_corrente = d.group(1)
                t.note[nota_corrente] = d.group(2).strip()
                continue

            mat = RE_MATRICOLA.search(riga)
            if mat and stato:
                if stato == "fino":
                    t.validita_a = mat.group(1)
                else:
                    t.validita_da = mat.group(1)
                t.validita_testo = (t.validita_testo + " " + riga).strip()
                stato = None
                continue

            # continuazione della nota corrente (testo multilingue)
            if nota_corrente and not t.note.get(nota_corrente):
                t.note[nota_corrente] = riga

    # --- intestazioni: macchina, titolo, GR TAV, data ---------------
    for tabella in soup.find_all("table"):
        righe = tabella.find_all("tr")
        if len(righe) < 2:
            continue
        celle = righe[-1].find_all("td")
        if len(celle) < 3:
            continue
        testa = celle[2].get_text(" ", strip=True)
        m = RE_GR_TAV.search(testa)
        if not m:
            continue
        tav = m.group(1) + m.group(2)
        t = tavole.get(tav)
        if not t:
            continue
        blocco = celle[0].get_text("\n", strip=True).split("\n")
        t.macchina = " ".join(blocco[:2]).strip()
        t.modelli_tavola = " ".join(blocco[2:]).strip()
        t.titolo = celle[1].get_text(" ", strip=True)
        t.data = (m.group(3) or "").strip()
        img = tabella.find("img")
        if img:
            t.immagine = img.get("src", "")

    return list(tavole.values())


# -------------------------------------------------------------------
# 4. FORMATO NUOVO (SERIE Q e simili)
# -------------------------------------------------------------------
from urllib.parse import parse_qs

# la posizione puo' essere "1", "12", "3-4", "10*"
RE_POS = re.compile(r'^\s*\d+[\w\-*/.]*\s*$')
RE_CODICE_URL = re.compile(r'CODICE=([^&"\s]+)', re.I)
RE_NOTA_MARK = re.compile(r'\((\d+)\)')
RE_NOTA_DEF = re.compile(r'^\s*\((\d+)\)\s*(.*)$')
RE_MATRICOLA = re.compile(r'\b([A-Z]\d{5,7})\b')
RE_VAL_FINO = re.compile(r'(VALIDO|VALIDA)\s+FINO\s+ALLA?\s+MACCHINA', re.I)
RE_VAL_DA = re.compile(r'(VALIDO|VALIDA)\s+DALLA?\s+MACCHINA', re.I)
RE_DATA = re.compile(r'\b(\d{2}\.\d{4})\b')
# "20_02_C", "10_01_C_01", "02_A_01"
RE_TAV_ID = re.compile(r'\b(\d{2}(?:_[0-9A-Z]{1,3})+)\b')


def _txt(nodo):
    return re.sub(r'\s+', ' ', nodo.get_text(" ", strip=True)) if nodo else ""


def e_pagina_tavola_nuova(html):
    """Pagina con l'elenco componenti nel formato nuovo."""
    h = html.upper()
    return ("CODICE=" in h
            and "POS." in h
            and ("GR TAV" in h or "MATRIC" in h)
            and "<TR" in h)


def e_pagina_gruppo_nuova(html):
    """Pagina grupNN.html: elenco delle tavole che compongono il gruppo."""
    return 'class="grup"' in html or "TAVOLE COMPONENTI IL GRUPPO" in html.upper()


# -------------------------------------------------------------------
# indice del gruppo:  grup20.html -> [{tavola, url, titolo}, ...]
# -------------------------------------------------------------------
def parse_gruppo_nuovo(html, base_url):
    soup = BeautifulSoup(html, "lxml")
    voci, visti = [], set()
    for a in soup.find_all("a", class_="grup", href=True):
        url = urljoin(base_url, a["href"]).split("#")[0]
        if url in visti:
            continue
        visti.add(url)
        etichetta = _txt(a)                      # "01_B", "02_A_01"
        # il titolo sta nell'ultima cella della stessa riga
        tr = a.find_parent("tr")
        celle = tr.find_all("td") if tr else []
        titolo = _txt(celle[-1]) if celle else ""
        # id completo della tavola: gruppo + etichetta, dedotto dal filename
        nome = urlparse(url).path.rsplit("/", 1)[-1]
        m = RE_TAV_ID.search(nome)
        voci.append({
            "tavola": m.group(1) if m else etichetta,
            "etichetta": etichetta,
            "titolo": titolo,
            "url": url,
        })
    return voci


# -------------------------------------------------------------------
# pagina tavola
# -------------------------------------------------------------------
def _codice_da_link(a):
    m = RE_CODICE_URL.search(a.get("href", ""))
    if m:
        return m.group(1).strip()
    q = parse_qs(urlparse(a.get("href", "")).query)
    for k, v in q.items():
        if k.upper() == "CODICE" and v:
            return v[0].strip()
    return _txt(a)


def _e_tabella_componenti(tab):
    intest = " ".join(_txt(th) for th in tab.find_all("th")).upper()
    if "POS" in intest and ("MATRIC" in intest or "DESCRIZIONE" in intest):
        return True
    # alcune tavole non usano <th>: mi accontento dei link a CODICE=
    return bool(tab.find("a", href=RE_CODICE_URL))


def _blocchi_tavola(soup):
    """
    Una pagina puo' contenere piu' tavole. Ogni tavola e' una <table>
    esterna che contiene sia l'elenco componenti sia la testata GR TAV.
    Prendo le tabelle piu' esterne che contengono 'GR TAV'.
    """
    blocchi = []
    for tab in soup.find_all("table"):
        if "GR TAV" not in tab.get_text(" ", strip=True).upper():
            continue
        if tab.find_parent("table") is not None and any(
                tab in b.find_all("table") for b in blocchi):
            continue
        # scarto le tabelle annidate dentro un blocco gia' preso
        if any(tab in b.descendants for b in blocchi):
            continue
        blocchi.append(tab)
    if not blocchi:                       # fallback: tutta la pagina
        body = soup.find("body") or soup
        blocchi = [body]
    return blocchi


def _id_tavola(blocco, soup, html, url=""):
    # 1. <h2 class="C"> sotto "GR TAV"
    for h in blocco.find_all(["h2", "h3", "p", "td"]):
        t = _txt(h)
        if RE_TAV_ID.fullmatch(t or ""):
            return t
    # 2. <a name="20_02_C">
    a = soup.find("a", attrs={"name": True})
    if a and RE_TAV_ID.fullmatch(a["name"]):
        return a["name"]
    # 3. commento <!-- TAVOLA 20_02_C -->
    m = re.search(r'TAVOLA\s+(\d{2}(?:_[0-9A-Z]{1,3})+)', html, re.I)
    if m:
        return m.group(1)
    # 4. nome del file
    m = RE_TAV_ID.search(urlparse(url).path.rsplit("/", 1)[-1])
    return m.group(1) if m else ""


def parse_tavole_nuovo(html, url="", Tavola=Tavola, Riga=Riga):
    """
    Estrae le tavole dal formato nuovo.

    Restituisce oggetti Tavola/Riga. Passando Tavola=dict si ottengono
    dizionari grezzi, comodi per i test.
    """
    soup = BeautifulSoup(html, "lxml")
    risultati = []

    for blocco in _blocchi_tavola(soup):
        tav_id = _id_tavola(blocco, soup, html, url)
        if not tav_id:
            continue
        gruppo, _, numero = tav_id.partition("_")

        righe, note, val = [], {}, {"da": "", "a": "", "testo": ""}
        stato = None
        nota_corrente = None

        for tab in blocco.find_all("table"):
            if not _e_tabella_componenti(tab):
                continue
            for tr in tab.find_all("tr"):
                celle = tr.find_all("td")
                if not celle:
                    continue                      # riga di intestazione (<th>)
                testi = [_txt(c) for c in celle]
                link = tr.find("a", href=RE_CODICE_URL)

                if link and len(celle) >= 3:
                    pos = testi[0]
                    if not RE_POS.match(pos):
                        pos = ""
                    descr = testi[2] if len(testi) > 2 else ""
                    marcatori = RE_NOTA_MARK.findall(descr)
                    descr = RE_NOTA_MARK.sub("", descr).strip(" -")
                    quantita = testi[3] if len(testi) > 3 else ""
                    righe.append({
                        "posizione": pos,
                        "codice": _codice_da_link(link),
                        "descrizione": re.sub(r'\s+', ' ', descr),
                        "quantita": quantita,
                        "note": marcatori,
                    })
                    continue

                # riga senza codice: legenda note o validita' matricola
                testo = " ".join(t for t in testi if t).strip()
                if not testo:
                    continue
                if RE_VAL_FINO.search(testo):
                    stato = "a"
                    m = RE_MATRICOLA.search(testo)
                    if m:
                        val["a"] = m.group(1)
                        stato = None
                    val["testo"] = (val["testo"] + " " + testo).strip()
                    continue
                if RE_VAL_DA.search(testo):
                    stato = "da"
                    m = RE_MATRICOLA.search(testo)
                    if m:
                        val["da"] = m.group(1)
                        stato = None
                    val["testo"] = (val["testo"] + " " + testo).strip()
                    continue
                d = RE_NOTA_DEF.match(testo)
                if d:
                    nota_corrente = d.group(1)
                    note[nota_corrente] = d.group(2).strip()
                    continue
                m = RE_MATRICOLA.search(testo)
                if m and stato:
                    val[stato] = m.group(1)
                    val["testo"] = (val["testo"] + " " + testo).strip()
                    stato = None
                    continue
                if nota_corrente and not note.get(nota_corrente):
                    note[nota_corrente] = testo

        # --- testata: macchina, modelli, titolo, data -----------------
        macchina = modelli = titolo = data = ""
        testata = _txt(blocco)
        m = RE_DATA.search(testata)
        if m:
            data = m.group(1)

        h2 = blocco.find_all(["h2", "h3"])
        for h in h2:
            t = _txt(h)
            if not t or RE_TAV_ID.fullmatch(t):
                continue
            if not macchina:
                macchina = t
        # modelli: il <p class="L"> subito dopo il titolo macchina
        for p in blocco.find_all("p"):
            t = _txt(p)
            if not t:
                continue
            if re.fullmatch(r'[\w\s./+-]*\d[\w\s./+-]*', t) and "/" in t and not modelli:
                modelli = t
            elif t.isupper() and len(t) > 3 and not titolo and t != macchina:
                titolo = t
        if not titolo:
            # ultima spiaggia: la cella "TESTINA"
            m = re.search(r'TESTINA[^>]*-->\s*([^<]+)', html, re.I)
            if m:
                titolo = re.sub(r'\s+', ' ', m.group(1)).strip()

        img = blocco.find("img", class_="tav") or blocco.find("img")
        immagine = urljoin(url, img.get("src", "")) if (img and url) else (
            img.get("src", "") if img else "")

        dati = {
            "tavola": tav_id,
            "gruppo": gruppo,
            "numero": numero,
            "titolo": titolo,
            "macchina": macchina,
            "modelli_tavola": modelli,
            "data": data,
            "immagine": immagine,
            "righe": righe,
            "note": note,
            "validita_da": val["da"],
            "validita_a": val["a"],
            "validita_testo": val["testo"].strip(),
        }
        if not righe:
            continue
        if Tavola is None:
            risultati.append(dati)
        else:
            t = Tavola(tavola=dati["tavola"], gruppo=dati["gruppo"],
                       numero=dati["numero"], titolo=dati["titolo"],
                       macchina=dati["macchina"],
                       modelli_tavola=dati["modelli_tavola"],
                       data=dati["data"], immagine=dati["immagine"],
                       note=dati["note"], validita_da=dati["validita_da"],
                       validita_a=dati["validita_a"],
                       validita_testo=dati["validita_testo"])
            for r in righe:
                t.righe.append(Riga(posizione=r["posizione"],
                                    codice=r["codice"],
                                    descrizione=r["descrizione"],
                                    note=r["note"],
                                    quantita=r["quantita"]))
            risultati.append(t)
    return risultati


# -------------------------------------------------------------------
# 5. DISPATCHER + UTILITA'
# -------------------------------------------------------------------
def parse_tavole(html, url=""):
    """Riconosce il formato e usa il parser giusto."""
    tavole = parse_tavole_vecchio(html)
    if tavole:
        return tavole
    return parse_tavole_nuovo(html, url)


def decodifica(byte, header_encoding=None):
    """
    Le pagine dichiarano windows-1252 nel <meta>, ma alcune sono
    davvero UTF-8. Provo UTF-8 stretto e ricado su cp1252.
    """
    if isinstance(byte, str):
        return byte
    for enc in filter(None, [header_encoding, "utf-8", "cp1252", "latin-1"]):
        try:
            return byte.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return byte.decode("cp1252", errors="replace")
