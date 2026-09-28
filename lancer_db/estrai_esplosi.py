"""
Estrazione delle tabelle ricambi dai PDF "Esplosi ricambi" LANCER
(esclusa la cartella "1° Serie") nel DB lancer_db/lancer.db.

I PDF hanno testo nativo: niente OCR. Le tabelle vengono lette con
PyMuPDF (page.find_tables), che restituisce le celle unite come
valore + None nelle celle coperte: e' cosi' che si distingue
"qta 1 per TUTTI i modelli" (cella unita) da "qta 1 solo per HD 200".

Layout gestiti (vedi README.md):
  - colonna QTY singola (JH/JL/JM, PL/PM/PMF, UM, HS, GM, Maximo Bold)
  - una colonna di quantita' per modello (HD, HP, KH, KM, Master)
  - MP e Maximo: nessuna colonna QTY
  - due tabelle affiancate nella stessa pagina (HS gearbox, Maximo)
  - qta come elenco "(10-12-14-16)" + modelli nella riga sotto (PL/PM/PMF)
  - colonna "Model" con "MB 205, 280 , 350" (Maximo Bold)

A fine estrazione stampa un controllo di copertura: per ogni pagina,
i codici presenti nel testo ma non finiti in nessuna riga.

Uso:  ./venv/Scripts/python.exe lancer_db/estrai_esplosi.py
Rilanciabile: svuota e riscrive serie/tavole/righe/righe_modelli
(NON tocca ricambi/tipi/embeddings, cioe' l'arricchimento).
"""
import re
import sys

import pymupdf

from db import MACCHINE, PDF_ESPLOSI, connetti

sys.stdout.reconfigure(encoding="utf-8")

# sigla serie dal nome file (i nomi file non sono uniformi)
SIGLE = [
    (r"^GM", "GM"), (r"^HD", "HD"), (r"^HP", "HP"), (r"^HS", "HS"),
    (r"^JH", "JH"), (r"^JL", "JL"), (r"^JM", "JM"), (r"^KH", "KH"),
    (r"^KM", "KM"), (r"^MP", "MP"), (r"^Master", "MASTER"),
    (r"^Maximo Bold", "MAXIMO BOLD"), (r"^Maximo", "MAXIMO"),
    (r"^PL", "PL"), (r"^PMF", "PMF"), (r"^PM", "PM"), (r"^UM", "UM"),
]
ESCLUDI = {"Blade Information.pdf"}   # solo disegni coltelli + peso, nessun codice

# codice ricambio: 7 cifre (quasi tutti) oppure "20001 200 001A" (GM)
RE_CODICE = re.compile(r"^(\d{7}|\d{5} \d{3} \d{3}[A-Z]?)$")
RE_CODICE_TESTO = re.compile(r"\b(\d{7}|\d{5} \d{3} \d{3}[A-Z]?)\b")
RE_MODELLO = re.compile(r"^[A-Za-z]{1,6}\s*\d{2,3}$")


def pulisci(c):
    if c is None:
        return None
    c = c.replace("\n", " ")
    c = re.sub(r"(\w) _(\w)", r"\1/\2", c)      # "G1 _2" nel PDF = "G1/2"
    c = re.sub(r"(\w)_(\w)", r"\1/\2", c)
    return re.sub(r"\s+", " ", c).strip()


def sigla_serie(nome_file):
    for pat, sigla in SIGLE:
        if re.match(pat, nome_file, re.I):
            return sigla
    raise ValueError(f"serie sconosciuta: {nome_file}")


def e_codice(v):
    return bool(v) and bool(RE_CODICE.match(v))


def trova_intestazione(rows):
    """Indice della riga 'Item No | Part No | Description | QTY' e mappa
    nome_colonna -> indice. Se 'Item No' compare due volte (due tabelle
    affiancate) restituisce due mappe."""
    for i, r in enumerate(rows):
        celle = [(pulisci(c) or "").lower() for c in r]
        idx_item = [j for j, c in enumerate(celle) if re.match(r"item\s*no", c)]
        if not idx_item:
            continue
        mappe = []
        confini = idx_item + [len(celle)]
        for a, b in zip(confini, confini[1:]):
            m = {"item": a}
            for j in range(a, b):
                c = celle[j]
                if re.match(r"(part|sales)\s*(no|code)", c):
                    m["codice"] = j
                elif re.match(r"(description|item name)", c):
                    m["desc"] = j
                elif c == "qty":
                    m["qty"] = j
                elif c == "model":
                    m["model"] = j
            m["fine"] = b
            if "codice" in m:
                mappe.append(m)
        if mappe:          # "ITEM NO. 9" delle tabelline ingranaggi non e' un'intestazione
            return i, mappe
    return None, []


def modelli_colonne(rows, h, m):
    """Per il layout a colonne-modello: {indice_colonna: [modelli]}.
    Le etichette sono nelle righe sopra l'intestazione, a volte spezzate
    su due righe ('HD' / '125'), a volte due modelli ('KH 130' / 'KM 130')."""
    if "qty" not in m:
        return {}
    out = {}
    for j in range(m["qty"], m["fine"]):
        parti = [pulisci(rows[i][j]) for i in range(h) if pulisci(rows[i][j])]
        if not parti:
            continue
        if all(RE_MODELLO.match(p) for p in parti):
            out[j] = [re.sub(r"\s+", " ", p) for p in parti]
        else:
            out[j] = [" ".join(parti)]
    return out if len(out) > 1 else {}


def titolo_tabella(rows, h):
    for i in range(h):
        r = rows[i]
        testi = [pulisci(c) for c in r if pulisci(c)]
        if len(testi) == 1 and not RE_MODELLO.match(testi[0]):
            return testi[0]
    return None


def espandi_modelli_elenco(testo):
    """'MB 205, 280 , 350' -> ['MB 205', 'MB 280', 'MB 350']"""
    pezzi = [p.strip() for p in testo.split(",") if p.strip()]
    out, prefisso = [], ""
    for p in pezzi:
        mm = re.match(r"^([A-Za-z ]*?)\s*(\d{2,3})$", p)
        if not mm:
            return []
        if mm.group(1).strip():
            prefisso = mm.group(1).strip()
        out.append(f"{prefisso} {mm.group(2)}".strip())
    return out


def leggi_blocco(rows, h, m, col_modelli, dati_da=None, sigla=""):
    """Righe dati di una (mezza) tabella. Restituisce una lista di dict
    {posizione, codice, descrizione, qta, modelli: {modello: qta}, note}."""
    out = []
    pos_corrente = None
    ic, idesc = m.get("codice"), m.get("desc")
    iq, imod = m.get("qty"), m.get("model")
    for r in rows[(dati_da if dati_da is not None else h + 1):]:
        cel = [pulisci(c) for c in r]
        item = cel[m["item"]] if m["item"] < len(cel) else None
        codice = cel[ic] if ic is not None and ic < len(cel) else None

        # testo descrizione: tutte le celle testuali tra codice e qty/model/fine
        fine_desc = min(x for x in (iq, imod, m["fine"]) if x is not None)
        desc_parti = [cel[j] for j in range((idesc or 0), fine_desc)
                      if j < len(cel) and cel[j] and j != ic]
        desc = " ".join(desc_parti)

        if not codice or not e_codice(codice):
            # riga di continuazione della precedente
            if not out:
                continue
            prec = out[-1]
            if imod is not None and imod < len(cel) and cel[imod]:
                q = cel[iq] if iq is not None and iq < len(cel) else None
                for mod in espandi_modelli_elenco(cel[imod]):
                    prec["modelli"][mod] = q
            elif desc:
                prec["descrizione"] = f"{prec['descrizione']} {desc}".strip()
            continue

        if item and re.match(r"^\d+[A-Za-z]?$", item):
            pos_corrente = item
        riga = {"posizione": pos_corrente, "codice": codice,
                "descrizione": desc, "qta": None, "modelli": {}, "note": None}

        if col_modelli:
            # celle unite: valore seguito da None = stessa qta sulle colonne coperte
            valore = None
            testo_non_num = None
            for j in range(iq, m["fine"]):
                c = r[j] if j < len(r) else ""
                if c is None:
                    v = valore
                else:
                    v = pulisci(c) or None
                    valore = v
                if v and not re.match(r"^\d+$", v):
                    testo_non_num = v
                    continue
                if v and j in col_modelli:
                    for mod in col_modelli[j]:
                        riga["modelli"][mod] = v
            if testo_non_num:
                riga["note"] = testo_non_num
        else:
            if iq is not None and iq < len(cel):
                riga["qta"] = cel[iq]
            if imod is not None and imod < len(cel) and cel[imod]:
                for mod in espandi_modelli_elenco(cel[imod]):
                    riga["modelli"][mod] = riga["qta"]
        out.append(riga)

    # qta a elenco: "(10-12-14-16)" + descrizione "... (PM 250-PM 300-...)"
    for riga in out:
        q = riga["qta"] or ""
        mq = re.match(r"^\(([\d\s\-]+)\)$", q)
        md = re.search(r"\(([^()]+)\)\s*$", riga["descrizione"])
        if mq and md:
            qte = [x.strip() for x in mq.group(1).split("-")]
            mods = [x.strip() for x in md.group(1).split("-")]
            # JM/JH/JL: solo le larghezze "(160-180-200)" -> "JM 160"...
            mods = [f"{sigla} {x}" if re.match(r"^\d{2,3}$", x) else x for x in mods]
            if len(qte) == len(mods) and all(RE_MODELLO.match(x) for x in mods):
                riga["modelli"] = dict(zip(mods, qte))
                riga["descrizione"] = riga["descrizione"][:md.start()].strip()

    # qta "unica" solo se uguale per tutti i modelli
    for riga in out:
        qte = set(riga["modelli"].values())
        if len(qte) > 1:
            riga["qta"] = None
        elif col_modelli:
            riga["qta"] = qte.pop() if qte else None
    return out


def mezza_tabella_nascosta(rows, h, m):
    """Maximo: due tabelle affiancate ma con una sola intestazione; la
    meta' destra (item|codice|desc) inizia gia' sulla riga d'intestazione."""
    if "qty" in m or "desc" not in m:
        return None
    j0 = m["desc"] + 1
    if j0 + 2 >= len(rows[h]) + 0 and j0 + 2 > len(rows[h]) - 1:
        return None
    righe_cod = [i for i, r in enumerate(rows)
                 if len(r) > j0 + 1 and e_codice(pulisci(r[j0 + 1]))]
    if len(righe_cod) < 2:
        return None
    return {"item": j0, "codice": j0 + 1, "desc": j0 + 2, "fine": len(rows[h]),
            "da": righe_cod[0]}


def estrai_pdf(con, percorso):
    nome = percorso.name
    sigla = sigla_serie(nome)
    doc = pymupdf.open(percorso)
    copertina = " ".join(doc[0].get_text().split())
    cur = con.execute("INSERT INTO serie(sigla, macchina, nome, file_pdf) VALUES (?,?,?,?)",
                      (sigla, MACCHINE.get(sigla), copertina[:200], nome))
    serie_id = cur.lastrowid

    mancanti = {}
    ultima_pag_senza_tabella = None
    n_righe = 0
    for np_, page in enumerate(doc, start=1):
        tabelle = page.find_tables().tables
        trovate_qui = set()
        ha_intestazione = False
        for t in tabelle:
            rows = t.extract()
            h, mappe = trova_intestazione(rows)
            if h is None:
                continue
            ha_intestazione = True
            titolo = titolo_tabella(rows, h)
            blocchi = []
            for m in mappe:
                blocchi.append((m, modelli_colonne(rows, h, m), None))
                nasc = mezza_tabella_nascosta(rows, h, m)
                if nasc:
                    m["fine"] = nasc["item"]
                    blocchi.append((nasc, {}, nasc["da"]))
            if not titolo or re.search(r"series$", titolo, re.I):
                # nessun titolo, o solo il nome serie (KH/KM): prova la pagina del disegno
                titolo = titolo_da_pagina(doc, np_, ultima_pag_senza_tabella) or titolo
            cur = con.execute(
                "INSERT INTO tavole(serie_id, titolo, pagina_tabella, pagina_disegno) "
                "VALUES (?,?,?,?)",
                (serie_id, titolo, np_,
                 ultima_pag_senza_tabella if ultima_pag_senza_tabella == np_ - 1 else None))
            tavola_id = cur.lastrowid
            for m, cm, dati_da in blocchi:
                for k, r in enumerate(leggi_blocco(rows, h, m, cm, dati_da, sigla)):
                    cur = con.execute(
                        "INSERT INTO righe(tavola_id, posizione, codice, descrizione, qta, note, ordine) "
                        "VALUES (?,?,?,?,?,?,?)",
                        (tavola_id, r["posizione"], r["codice"], r["descrizione"],
                         r["qta"], r["note"], k))
                    for mod, q in r["modelli"].items():
                        con.execute("INSERT INTO righe_modelli VALUES (?,?,?,'tabella')",
                                    (cur.lastrowid, mod, q))
                    trovate_qui.add(r["codice"])
                    trovate_qui.update(RE_CODICE_TESTO.findall(r["descrizione"]))
                    n_righe += 1
        if not ha_intestazione:
            ultima_pag_senza_tabella = np_
        nel_testo = set(RE_CODICE_TESTO.findall(page.get_text()))
        persi = nel_testo - trovate_qui
        if persi:
            recuperate = tabellina_ingranaggi(page, persi)
            if recuperate:
                cur = con.execute(
                    "INSERT INTO tavole(serie_id, titolo, pagina_tabella, pagina_disegno) "
                    "VALUES (?,?,?,?)",
                    (serie_id, f"{titolo_da_pagina(doc, np_ + 1, np_) or ''} - GEAR SET DETAIL".strip(" -"),
                     np_, np_))
                for k, (pos, codice, desc) in enumerate(recuperate):
                    con.execute(
                        "INSERT INTO righe(tavola_id, posizione, codice, descrizione, qta, note, ordine) "
                        "VALUES (?,?,?,?,?,?,?)",
                        (cur.lastrowid, pos, codice, desc, None,
                         "tabellina ingranaggi sul disegno (varianti rapporto)", k))
                    n_righe += 1
                persi -= {c for _, c, _ in recuperate}
        if persi:
            mancanti[np_] = persi
    return sigla, n_righe, mancanti


def tabellina_ingranaggi(page, codici):
    """Tabelline 'GEAR SET DETAIL' / 'Item No 9 | Item No 10' stampate sul
    disegno: coppie 'Gear Z 17 | 1102030' raggruppate per posizione.
    Si lavora sulle parole con coordinate: per ogni codice, la descrizione
    sono le parole alla sua sinistra sulla stessa riga (fino al codice
    precedente), la posizione e' l'intestazione 'Item No N' piu' vicina
    a sinistra sopra di esso."""
    # coordinate "visive": alcune pagine sono ruotate di 90 gradi
    parole = []
    for w in page.get_text("words"):     # x0, y0, x1, y1, testo, ...
        r = pymupdf.Rect(w[:4]) * page.rotation_matrix
        parole.append((r.x0, r.y0, r.x1, r.y1, w[4]))
    parole.sort(key=lambda w: (round(w[1]), w[0]))
    intest = []
    for i, w in enumerate(parole):
        if w[4].lower() == "no" or w[4].lower() == "no.":
            succ = parole[i + 1] if i + 1 < len(parole) else None
            prec = parole[i - 1] if i > 0 else None
            if prec and prec[4].lower() == "item" and succ and succ[4].isdigit():
                intest.append((prec[0], prec[1], succ[4]))
    out = []
    for w in parole:
        if w[4] not in codici:
            continue
        yc = (w[1] + w[3]) / 2
        stessa = sorted((v for v in parole if abs((v[1] + v[3]) / 2 - yc) < 2.5 and v[2] <= w[0] + 1),
                        key=lambda v: v[0])
        desc = []
        for v in reversed(stessa):
            if v is w:
                continue
            if RE_CODICE.match(v[4]):
                break
            desc.insert(0, v[4])
        sopra = [h for h in intest if h[1] < w[1] and h[0] <= w[0] + 5]
        pos = max(sopra, key=lambda h: (h[0], h[1]))[2] if sopra else None
        if desc:
            out.append((pos, w[4], " ".join(desc)))
    return out


RE_NON_TITOLO = re.compile(
    r"www\.|\bdoc\b|\brev\b|series|part no|descri|^model name$|item no", re.I)


def titolo_da_pagina(doc, np_, pag_disegno):
    """Titolo di ripiego dalla pagina del disegno: la prima riga di testo
    che non sia testata (sito, 'Doc XX Rev', 'HD Series') ne' numeri."""
    if pag_disegno != np_ - 1:
        return None
    for riga in doc[pag_disegno - 1].get_text().splitlines():
        riga = riga.strip()
        if len(riga) > 6 and re.search(r"[A-Za-z]{3}", riga) \
                and not RE_NON_TITOLO.search(riga) and not re.match(r"^[\d\s]+$", riga):
            return riga
    return None


def modelli_da_descrizione(con):
    """Serie con colonna QTY unica: il modello e' scritto solo nella
    descrizione ('Rotor Welded JM 160', 'FRAME WA JMH 180', 'PL 175 FRAME WA',
    'Frame KH 180 & 185'). Si accettano solo sigle che iniziano con la
    stesse due lettere della serie (esclude misure tipo 'Z 46' o 'M 12',
    e i pezzi condivisi tipo 'Rotor HD 145' dentro il catalogo HS)."""
    iniziali = {"MAXIMO BOLD": ("MB", "MAXIMO"), "MAXIMO": ("MAXIMO",)}
    righe = con.execute(
        "SELECT r.id, r.descrizione, r.qta, s.sigla FROM righe r "
        "JOIN tavole t ON t.id = r.tavola_id JOIN serie s ON s.id = t.serie_id "
        "WHERE NOT EXISTS (SELECT 1 FROM righe_modelli m WHERE m.riga_id = r.id)").fetchall()
    n = 0
    for rid, desc, qta, sigla in righe:
        trovati = set()
        for mm in re.finditer(r"\b([A-Z]{2,6})[\s-]?(\d{2,3})\b((?:\s*[&,]?\s*\d{2,3}\b)*)",
                              (desc or "").upper()):
            pref, num = mm.group(1), mm.group(2)
            if not pref.startswith(iniziali.get(sigla, (sigla[:2],))):
                continue
            trovati.add(f"{pref} {num}")
            # "HD-125 45 55" = 125, 145, 155 ; "KH 180 & 185"
            for extra in re.findall(r"\d{2,3}", mm.group(3)):
                if len(extra) == 2 and len(num) == 3:
                    extra = num[0] + extra
                trovati.add(f"{pref} {extra}")
        for mod in trovati:
            con.execute("INSERT INTO righe_modelli VALUES (?,?,?,'descrizione')", (rid, mod, qta))
            n += 1
    return n


def main():
    con = connetti()
    for t in ("righe_modelli", "righe", "tavole", "serie"):
        con.execute(f"DELETE FROM {t}")
    pdf = sorted(p for p in PDF_ESPLOSI.iterdir()
                 if p.suffix.lower() == ".pdf" and p.name not in ESCLUDI)
    tot = 0
    for p in pdf:
        sigla, n, mancanti = estrai_pdf(con, p)
        tot += n
        print(f"{sigla:12s} {n:4d} righe   {p.name}")
        for pag, codici in mancanti.items():
            print(f"     ! pag {pag}: {len(codici)} codici nel testo non estratti: "
                  f"{sorted(codici)[:8]}")
    print(f"\nModelli ricavati dalle descrizioni: {modelli_da_descrizione(con)}")
    con.commit()
    print(f"Totale righe: {tot}")
    print("Codici univoci:", con.execute("SELECT COUNT(DISTINCT codice) FROM righe").fetchone()[0])


if __name__ == "__main__":
    main()
