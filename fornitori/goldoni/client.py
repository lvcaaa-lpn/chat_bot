"""
Client HTTP per il catalogo Goldoni.

Il sito e' pubblico e statico: nessun login, nessun cookie, nessuna
sessione che scade. Questo permette di scaricare un catalogo una volta
e riusarlo per sempre.

Strategia del crawler: invece di indovinare gli schemi degli URL,
partiamo da index.htm e seguiamo i link .htm rimanendo dentro la
cartella del catalogo. Ogni pagina che contiene un elenco di ricambi
viene analizzata. Cosi' funziona anche se cataloghi diversi hanno
strutture di navigazione differenti.
"""

import time
from datetime import datetime
from urllib.parse import urljoin, urlparse

import requests

from .parser import (BASE, parse_indice_cataloghi, parse_indice_gruppi,
                     parse_tavole, e_pagina_tavola)
from . import db

DELAY = 1.0          # pausa fra le richieste: e' un sito altrui
TIMEOUT = 20
INDICE = BASE + "/"


class GoldoniClient:
    def __init__(self, delay=DELAY):
        self.s = requests.Session()
        self.s.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/126.0 Safari/537.36",
            "Accept-Language": "it-IT,it;q=0.9",
        })
        self.delay = delay
        self._ultimo = 0.0

    def get(self, url):
        attesa = self.delay - (time.time() - self._ultimo)
        if attesa > 0:
            time.sleep(attesa)
        r = self.s.get(url, timeout=TIMEOUT)
        self._ultimo = time.time()
        r.raise_for_status()
        # le pagine sono in latin-1: senza questo le accentate si rompono
        if not r.encoding or r.encoding.lower() == "iso-8859-1":
            r.encoding = "latin-1"
        return r.text

    # ---------------------------------------------------------------
    def scarica_indice(self, con, url=INDICE):
        """Popola la tabella dei cataloghi disponibili."""
        cataloghi = parse_indice_cataloghi(self.get(url), base_url=url)
        db.salva_cataloghi(con, cataloghi)
        return cataloghi

    # ---------------------------------------------------------------
    def scarica_catalogo(self, con, codice, max_pagine=600, verbose=True,
                         debug=False):
        """
        Percorre tutte le pagine di un catalogo e salva le tavole trovate.
        Visita in ampiezza partendo da index.htm.
        """
        from urllib.parse import quote, unquote
        radice = f"{BASE}/cat/{quote(codice)}/"
        partenza = radice + "index.htm"

        def interno(u):
            """Confronto tollerante alla codifica percentuale."""
            return unquote(u).startswith(unquote(radice))

        da_visitare = [partenza]
        visitati = set()
        n_tavole = 0

        while da_visitare and len(visitati) < max_pagine:
            url = da_visitare.pop(0)
            if url in visitati:
                continue
            visitati.add(url)

            try:
                html = self.get(url)
            except Exception as e:
                if verbose:
                    print(f"    ! {url.rsplit('/', 1)[-1]}: {e}")
                continue

            if e_pagina_tavola(html):
                for t in parse_tavole(html):
                    db.salva_tavola(con, codice, t, url)
                    n_tavole += 1
                if verbose:
                    print(f"    tavole da {url.rsplit('/', 1)[-1]} "
                          f"(totale {n_tavole})")

            # accodo i link interni al catalogo
            nuovi = 0
            for link in parse_indice_gruppi(html, url):
                u = link["url"].split("#")[0]
                if interno(u) and u not in visitati:
                    da_visitare.append(u)
                    nuovi += 1

            if debug:
                nome = url.rsplit("/", 1)[-1] or "index"
                print(f"    [{nome}] tavola={e_pagina_tavola(html)} "
                      f"link_nuovi={nuovi} dimensione={len(html)}")
                if nuovi == 0:
                    for link in parse_indice_gruppi(html, url)[:8]:
                        print(f"        scartato: {link['url']}")

        con.commit()
        db.segna_scaricato(con, codice, datetime.now().isoformat(timespec="seconds"))
        if verbose:
            print(f"  visitate {len(visitati)} pagine, {n_tavole} tavole salvate")
        return n_tavole

    # ---------------------------------------------------------------
    def dettaglio_articolo(self, codice):
        """
        Scheda articolo (prezzo/disponibilita'), da chiedere in tempo reale:
        e' l'unico dato che non ha senso mettere in cache.
        """
        url = (BASE + "/tsweb/ricambi/articoli"
                      f"?ACTION=scegliArt&CODICE={codice}")
        return self.get(url)
