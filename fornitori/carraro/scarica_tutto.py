"""
Scarica in locale l'intero catalogo Antonio Carraro in
fornitori/carraro/carraro.db, piu' richieste in parallelo con UN solo
login. Struttura e comportamento ricalcati su fornitori/sdf/scarica_tutto.py.

Cosa fa:
  1. elenco: per ogni sezione legge le categorie (a qualunque profondita')
     e salva i prodotti con la loro foto. ~200 richieste.
  2. anagrafica: legge tutta la ricerca codici '*' (2.500 pagine da 20):
     tutti i codici del sito con descrizione e CODICI PRECEDENTI (le
     sostituzioni, come substitution in SDF). ~2.500 richieste.
  3. modelli: come crawl_model in SDF. Per ogni prodotto legge la pagina
     (tavole, gruppi, matricole, allegati PDF) e poi apre OGNI sua tavola
     dal percorso del modello: il sito mostra righe diverse a seconda del
     modello (a01-0050-01: 52 righe dal Tony 10900 SR, 51 dall'8900 SR),
     quindi qui, a differenza di SDF, una tavola gia' nota non basta
     collegarla. Il disegno e i punti si salvano una volta per tavola.
     ~59.000 richieste. Un prodotto finito e' segnato nel DB (prod:<url>).

Velocita': tutte le richieste passano da UN CarraroClient (un login, una
sessione), che ne lascia partire al massimo una ogni --ritardo secondi in
totale. Il sito impiega ~1 s a rispondere: in sequenza sarebbero ~20 ore.
Con piu' modelli in parallelo (--paralleli) le attese si sovrappongono
(misurato nel browser, stessa sessione: 1 alla volta ~0,9 s a tavola, 6
insieme ~0,38 s, 12 insieme ~0,28 s: il server non mette in fila le
richieste della stessa sessione). A quel punto la velocita' la decide
--ritardo. E' il portale del costruttore con l'account dell'azienda: si
scende col ritardo un passo alla volta guardando rich/s ed errori. Su
errori di sovraccarico (429, 502-504, timeout) lo script aumenta da solo
il ritardo; dopo molti errori di fila si ferma.

Interruzione e ripresa: Ctrl+C ferma tutto in pochi secondi (le richieste
non ancora partite non partono piu'). Al riavvio riparte da dove era:
sezioni, pagine di anagrafica, prodotti e singole tavole gia' fatte sono
segnate nel DB. Ogni tavola si salva in una transazione. Secondo Ctrl+C =
uscita immediata.

Tavole rotte sul sito (errore 500 o pagina vuota anche dall'indirizzo
diretto, viste 8 nel 2026): restano "da scaricare", il prodotto si segna
comunque finito e al prossimo avvio si ritentano solo quelle.

Uso (dalla radice del progetto):
  ./venv/Scripts/python.exe -m fornitori.carraro.scarica_tutto                    # tutto: 8 in parallelo, ritardo 0.3 s
  ./venv/Scripts/python.exe -m fornitori.carraro.scarica_tutto --ritardo 0.2      # un passo piu' veloce
  ./venv/Scripts/python.exe -m fornitori.carraro.scarica_tutto --limite 5         # prova: solo 5 modelli
  ./venv/Scripts/python.exe -m fornitori.carraro.scarica_tutto --solo-elenco
  ./venv/Scripts/python.exe -m fornitori.carraro.scarica_tutto --solo-anagrafica
  ./venv/Scripts/python.exe -m fornitori.carraro.scarica_tutto --solo-modelli
  ./venv/Scripts/python.exe -m fornitori.carraro.scarica_tutto --sezioni serie,motori
  ./venv/Scripts/python.exe -m fornitori.carraro.scarica_tutto --aggiorna-elenco  # rilegge sezioni e prodotti
  ./venv/Scripts/python.exe -m fornitori.carraro.scarica_tutto --stato            # punto della situazione, dal DB

Errori dettagliati in dati/log/scarica_carraro.log.
"""

import argparse
import logging
import os
import queue
import sys
import threading
import time
from collections import Counter, deque
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

import requests
from tqdm import tqdm

import config
from . import parser
from .client import CarraroClient, CarraroNonRaggiungibile
from .db import Db, chiave_tavola

log = logging.getLogger("carraro.scarica_tutto")

# sezioni del menu "Catalogo" (ricontrollate sul sito il 12/11/2026:
# kit-service esiste nel menu ma e' vuota; si legge comunque, costa una
# richiesta e se un giorno si riempie non sfugge)
SEZIONI = ["serie", "motori", "kits-aftermarket", "rasaerba", "contenitori",
           "accessori", "special-tools", "materiali-di-consumo", "kit-service"]

RITARDO_MINIMO = 0.1        # sotto non si scende nemmeno a mano
RITARDO_MASSIMO = 3.0       # tetto del rallentamento automatico
TENTATIVI_PER_MODELLO = 3   # poi il modello si lascia al prossimo avvio
ERRORI_DI_FILA_PER_FERMARSI = 20    # tavole/pagine fallite di fila: portale giu' o login rotto
STATUS_SOVRACCARICO = {429, 502, 503, 504}
# il 500 NON conta come sovraccarico: il sito lo da' fisso su alcune
# tavole rotte (d18-0100-01, d18-0031-02, ...), rallentare non serve


class Interrotto(BaseException):
    """Ctrl+C: BaseException, cosi' non viene presa per un errore qualsiasi."""


def _tipo_errore(e):
    if isinstance(e, requests.HTTPError) and e.response is not None:
        return f"HTTP {e.response.status_code}"
    return type(e).__name__


def _sovraccarico(e):
    return isinstance(e, CarraroNonRaggiungibile) or (
        isinstance(e, requests.HTTPError) and e.response is not None
        and e.response.status_code in STATUS_SOVRACCARICO)


# ------------------------------------------------------------------ stato
def stampa_stato(db):
    """Punto della situazione letto solo dal DB: niente rete, niente login."""
    s = db.statistiche()
    mb = os.path.getsize(db.path) / 1e6
    print("--- stato carraro.db ---")
    print(f"prodotti:          {s['prodotti']}  {s['prodotti_per_sezione']}")
    print(f"  scaricati:       {s['prodotti_pronti']} / {s['prodotti']}   "
          f"con foto: {s['prodotti_con_foto']}   solo PDF: {s['prodotti_solo_pdf']}")
    print(f"tavole:            {s['tavole']}   con disegno: {s['tavole_con_disegno']}   "
          f"punti: {s['punti']}")
    print(f"tavole per modello:{s['legami_scaricati']:>7} / {s['prodotto_tavola']} scaricate")
    print(f"righe ricambio:    {s['righe']}   codici distinti: {s['codici_in_tavole']}")
    print(f"anagrafica:        {s['anagrafica']} codici ({s['pagine_anagrafica']} pagine)   "
          f"codici precedenti: {s['codici_precedenti']}")
    if s["anagrafica"]:
        print(f"  codici delle tavole assenti dall'anagrafica: {s['codici_fuori_anagrafica']}")
    print(f"allegati:          {s['allegati']}   DB: {mb:.0f} MB")


# ------------------------------------------------------------ scaricatore
class Scaricatore:
    def __init__(self, paralleli, ritardo):
        self.stop = threading.Event()
        self.paralleli = paralleli
        self.client = CarraroClient(delay=ritardo, connessioni=paralleli + 4)
        self._conta_richieste()
        self.db = Db()
        self.errori = Counter()
        self._lk = threading.Lock()
        self._richieste = deque()   # istanti delle richieste dell'ultimo minuto
        self.n_richieste = 0
        self.errori_di_fila = 0
        self.righe_nuove = 0

    # -------------------------------------------------- freno e contatori
    def _conta_richieste(self):
        """Avvolge il freno del client: conta le richieste (per misurare la
        velocita' reale) e, dopo Ctrl+C, blocca quelle non ancora partite."""
        originale = self.client._throttle

        def freno():
            if self.stop.is_set():
                raise Interrotto()
            originale()
            ora = time.time()
            with self._lk:
                self.n_richieste += 1
                self._richieste.append(ora)
                while self._richieste and ora - self._richieste[0] > 60:
                    self._richieste.popleft()
        self.client._throttle = freno

    def richieste_al_secondo(self):
        with self._lk:
            if len(self._richieste) < 2:
                return 0.0
            return len(self._richieste) / max(1.0, time.time() - self._richieste[0])

    def rallenta(self, motivo):
        with self._lk:
            prima = self.client.delay
            self.client.delay = min(RITARDO_MASSIMO, prima * 1.5 + 0.05)
        tqdm.write(f"! {motivo}: ritardo {prima:.2f}s -> {self.client.delay:.2f}s")

    def _esito(self, errore=None, dove=""):
        """Conta errori e successi. Su sovraccarico rallenta; dopo troppi
        errori di fila ferma tutto (portale giu', login rotto...)."""
        with self._lk:
            if errore is None:
                self.errori_di_fila = 0
                return
            tipo = _tipo_errore(errore)
            self.errori[tipo] += 1
            self.errori_di_fila += 1
            di_fila = self.errori_di_fila
        if _sovraccarico(errore):
            self.rallenta(f"{tipo} su {dove[:40]}")
        if di_fila >= ERRORI_DI_FILA_PER_FERMARSI and not self.stop.is_set():
            tqdm.write(f"! {di_fila} errori di fila: mi fermo. "
                       "Controlla login e portale (dati/log/scarica_carraro.log).")
            self.stop.set()

    def _postfix(self):
        return (f"{self.richieste_al_secondo():.1f} rich/s, ritardo {self.client.delay:.2f}s, "
                f"errori {sum(self.errori.values())}")

    def _in_parallelo(self, lavori, funzione, barra):
        """Esegue funzione(lavoro) su tutti i lavori, al massimo
        self.paralleli alla volta. Gestisce Ctrl+C come SDF. Restituisce
        i lavori finiti con un'eccezione: [(lavoro, eccezione)]."""
        in_attesa, falliti = deque(lavori), []
        with ThreadPoolExecutor(max_workers=self.paralleli) as ex:
            attivi = {}

            def riempi():
                while in_attesa and len(attivi) < self.paralleli and not self.stop.is_set():
                    lavoro = in_attesa.popleft()
                    attivi[ex.submit(funzione, lavoro)] = lavoro
            riempi()
            try:
                while attivi:
                    fatti, _ = wait(list(attivi), timeout=0.5, return_when=FIRST_COMPLETED)
                    barra.set_postfix_str(self._postfix(), refresh=False)
                    barra.refresh()
                    for fut in fatti:
                        lavoro = attivi.pop(fut)
                        try:
                            fut.result()
                        except Interrotto:
                            pass
                        except Exception as e:
                            falliti.append((lavoro, e))
                    riempi()
            except KeyboardInterrupt:
                self.stop.set()
                tqdm.write("Interruzione: chiudo le richieste in corso "
                           "(di nuovo Ctrl+C per uscire subito)...")
                try:
                    wait(list(attivi))
                except KeyboardInterrupt:
                    os._exit(1)
        return falliti

    # ------------------------------------------------------- 1. elenco
    def _prodotti_categoria(self, url, nome, visitate, barra):
        """Prodotti di una categoria e di TUTTE le sue sottocategorie, a
        qualunque profondita', seguendo la paginazione. Ogni prodotto porta
        il nome della categoria piu' vicina (es. 'Supertigre' dentro '22').
        Senza nome (radice di una sezione) si usa il titolo della pagina."""
        da_visitare, prodotti = [url], []
        while da_visitare:
            u = da_visitare.pop(0)
            if u in visitate:
                continue
            visitate.add(u)
            html = self.client.get(u)
            barra.update(1)
            pagina = parser.parse_categoria(html)
            nome = nome or pagina["titolo"]
            prodotti += [{**p, "categoria": nome} for p in pagina["prodotti"]]
            for sotto in pagina["sottocategorie"]:
                if sotto["url"] in visitate:
                    continue
                barra.total += 1
                prodotti += self._prodotti_categoria(sotto["url"], sotto["nome"],
                                                     visitate, barra)
            nuove = [p for p in parser.link_pagine(html) if p not in visitate]
            barra.total += len(nuove)
            da_visitare += nuove
        return prodotti

    def scarica_elenco(self, sezioni, aggiorna):
        for sezione in sezioni:
            if self.stop.is_set():
                return
            chiave = f"sez:{sezione}"
            if self.db.fatto(chiave) and not aggiorna:
                continue
            barra = tqdm(total=1, desc=f"elenco {sezione}", unit="pagine",
                         dynamic_ncols=True, leave=False)
            try:
                trovati = self._prodotti_categoria(f"/catalogo/{sezione}/", None, set(), barra)
            finally:
                barra.close()
            unici = {}
            for p in trovati:
                unici.setdefault(p["url"], p)    # la prima categoria incontrata
            self.db.salva_prodotti(sezione, list(unici.values()))
            self.db.segna_fatto(chiave)
            tqdm.write(f"elenco {sezione}: {len(unici)} prodotti")
            if aggiorna:
                # i prodotti si rileggono nella fase modelli
                self.db._write([("DELETE FROM stato_download WHERE chiave=?",
                                 [(f"prod:{u}",) for u in unici], True)])

    # --------------------------------------------------- 2. anagrafica
    def scarica_anagrafica(self):
        if not self.db.fatto("ana:0"):
            prima = parser.parse_ricerca_codici(self.client.ricerca("codice", "*", 0))
            self.db.salva_anagrafica(prima["risultati"])
            self.db.segna_fatto("ana:0")
            # quante pagine ha l'anagrafica (letto una volta, dalla prima)
            self.db.segna_fatto(f"anapagine:{prima['pagine']}")
        pagine = max((int(k.rsplit(":", 1)[1]) for k in self.db.chiavi_fatte("anapagine:")),
                     default=1)
        fatte = self.db.chiavi_fatte("ana:")
        da_fare = [n for n in range(pagine) if f"ana:{n}" not in fatte]
        if not da_fare:
            tqdm.write(f"anagrafica: gia' letta ({pagine} pagine)")
            return
        barra = tqdm(total=pagine, initial=pagine - len(da_fare), desc="anagrafica",
                     unit="pagine", dynamic_ncols=True)

        def pagina(n):
            try:
                dati = parser.parse_ricerca_codici(self.client.ricerca("codice", "*", n))
            except (requests.HTTPError, CarraroNonRaggiungibile) as e:
                log.warning("anagrafica pagina %d: %r", n, e)
                self._esito(e, f"anagrafica pagina {n}")
                return
            self._esito()
            if not dati["risultati"]:
                # resta da fare: al prossimo avvio si ritenta
                log.warning("anagrafica pagina %d: nessun codice", n)
                return
            self.db.salva_anagrafica(dati["risultati"])
            self.db.segna_fatto(f"ana:{n}")
            barra.update(1)
        try:
            self._in_parallelo(da_fare, pagina, barra)
        finally:
            barra.close()

    # ------------------------------------------------------ 3. modelli
    def _leggi_tavola(self, tentativi, codice):
        """Prova gli indirizzi in ordine: (dati, None) alla prima pagina che e'
        davvero la tavola cercata, altrimenti (None, ultimo errore)."""
        errore = None
        for url in tentativi:
            try:
                dati = parser.parse_tavola(self.client.get(url))
            except requests.HTTPError as e:
                errore = e
                continue
            if chiave_tavola(dati["codice"]) == codice:
                return dati, None
            # un'altra pagina (home, modello, tavola vuota senza intestazione):
            # NON si salva, altrimenti risulterebbe scaricata con 0 righe
            errore = ValueError(f"pagina inattesa ({dati['codice'] or 'senza codice tavola'})")
        return None, errore

    def scarica_modello(self, prodotto, posti):
        url = prodotto["url"]
        posto = posti.get()
        # dynamic_ncols: vedi SDF, con larghezza fissa le barre vanno a capo
        barra = tqdm(total=1, position=posto, leave=False, unit="tav", dynamic_ncols=True,
                     desc=f"{(prodotto['nome'] or url.rsplit('/', 1)[-1])[:28]:28}")
        try:
            html = self.client.get(url)
            tavole = parser.parse_modello(html)["tavole"]
            allegati = parser.parse_allegati(html)
            if not tavole:
                # pagine che raccolgono altri prodotti: si registrano, la coda
                # del prossimo avvio (o del giro in corso) li prende
                figli = parser.parse_categoria(html)["prodotti"]
                if figli:
                    self.db.salva_prodotti(prodotto["sezione"], [
                        {**f, "categoria": prodotto["categoria"]} for f in figli])
                    log.info("%s: nessuna tavola, %d prodotti dentro", url, len(figli))
                elif allegati:
                    log.info("%s: solo allegati (%s)", url,
                             ", ".join(a["nome"] for a in allegati))
                else:
                    log.warning("%s: nessuna tavola, nessun prodotto dentro e "
                                "nessun allegato: pagina da controllare", url)
            self.db.salva_tavole_prodotto(url, tavole)
            self.db.salva_allegati(url, allegati)

            legami = self.db.legami_da_scaricare(url)
            barra.reset(total=len(legami) or 1)
            for lg in legami:
                # prima la tavola vista da questo modello; l'indirizzo diretto
                # solo se il link e' rotto (visto: 0512-4741 con link
                # ".../tcd2-9l4-ht-8700/" senza codice). Dall'indirizzo diretto
                # arrivano le righe di TUTTI i modelli: meglio di niente, ma
                # si annota nel log
                tentativi = list(dict.fromkeys(u for u in (
                    lg["url"] or f"{url}/{lg['codice_sito'].lower()}",
                    "/" + lg["codice_sito"]) if u))
                dati, errore = self._leggi_tavola(tentativi, lg["tavola"])
                if dati is None:
                    log.warning("%s -> tavola %s: %r", url, lg["tavola"], errore)
                    self._esito(errore, f"tavola {lg['tavola']}")
                else:
                    self._esito()
                    if not dati["righe"]:
                        log.warning("%s -> tavola %s: nessuna riga", url, lg["tavola"])
                    self.db.salva_righe(url, lg["tavola"], dati)
                    with self._lk:
                        self.righe_nuove += len(dati["righe"])
                barra.update(1)
            # finito anche se qualche tavola e' rotta: quelle restano
            # "da scaricare" e si ritentano da sole al prossimo avvio
            self.db.segna_fatto(f"prod:{url}")
        finally:
            barra.close()
            posti.put(posto)

    def scarica_modelli(self, limite=None):
        coda = self.db.modelli_da_scaricare()
        if limite:
            coda = coda[:limite]
        tot = self.db.statistiche()["prodotti"]
        gia = 0 if limite else tot - len(coda)
        if not coda:
            tqdm.write("modelli: tutti scaricati.")
            return
        generale = tqdm(total=len(coda) if limite else tot, initial=gia, position=0,
                        unit="modelli", desc="modelli Carraro", dynamic_ncols=True)
        posti = queue.Queue()
        for p in range(1, self.paralleli + 1):
            posti.put(p)
        tentativi = Counter()

        def un_modello(p):
            self.scarica_modello(p, posti)
            generale.update(1)

        try:
            while coda and not self.stop.is_set():
                falliti = self._in_parallelo(coda, un_modello, generale)
                coda = []
                for p, e in falliti:
                    # errore sulla pagina del prodotto (le tavole hanno il loro
                    # conteggio dentro scarica_modello)
                    log.error("modello %s: %r", p["url"], e, exc_info=e)
                    self._esito(e, p["url"])
                    tentativi[p["url"]] += 1
                    if tentativi[p["url"]] < TENTATIVI_PER_MODELLO:
                        coda.append(p)
                    else:
                        tqdm.write(f"! {p['url']}: {_tipo_errore(e)} per "
                                   f"{TENTATIVI_PER_MODELLO} volte, lo lascio al prossimo avvio")
                # prodotti nuovi scoperti dentro pagine-raccolta
                if not limite and not self.stop.is_set():
                    nuovi = [p for p in self.db.modelli_da_scaricare()
                             if p["url"] not in tentativi and p not in coda
                             and not self.db.fatto(f"prod:{p['url']}")]
                    generale.total = self.db.statistiche()["prodotti"]
                    coda += [p for p in nuovi if p["url"] not in {c["url"] for c in coda}]
        finally:
            generale.close()

    # ---------------------------------------------------------- riepilogo
    def riepilogo(self, inizio):
        durata = time.time() - inizio
        print(f"\ndurata {durata / 60:.1f} min, {self.n_richieste} richieste "
              f"({self.n_richieste / max(durata, 1):.2f} al secondo), "
              f"{self.righe_nuove} righe ricambio nuove")
        print(f"ritardo finale {self.client.delay:.2f}s")
        if self.errori:
            print("errori: " + ", ".join(f"{k} x{v}" for k, v in self.errori.most_common()))
        print()
        stampa_stato(self.db)


# -------------------------------------------------------------------- main
def _configura_log():
    """Solo su file: a schermo ci sono le barre, un log a video le romperebbe."""
    cartella = config.DATI / "log"
    cartella.mkdir(parents=True, exist_ok=True)
    h = logging.FileHandler(cartella / "scarica_carraro.log", encoding="utf-8")
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    radice = logging.getLogger()
    radice.handlers[:] = [h]
    radice.setLevel(logging.INFO)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--paralleli", type=int, default=8,
                    help="modelli (e pagine di anagrafica) scaricati insieme (default 8)")
    ap.add_argument("--ritardo", type=float, default=0.3,
                    help="secondi minimi tra due richieste al portale, in totale (default 0.3)")
    ap.add_argument("--sezioni", default=",".join(SEZIONI),
                    help="sezioni separate da virgola (default: tutte)")
    ap.add_argument("--limite", type=int, help="scarica al massimo N modelli (per le prove)")
    ap.add_argument("--solo-elenco", action="store_true", help="solo fase 1")
    ap.add_argument("--solo-anagrafica", action="store_true", help="solo fase 2")
    ap.add_argument("--solo-modelli", action="store_true", help="solo fase 3")
    ap.add_argument("--aggiorna-elenco", action="store_true",
                    help="rilegge sezioni e pagine prodotto (prodotti/tavole nuove sul sito)")
    ap.add_argument("--stato", action="store_true", help="mostra solo a che punto e' il download")
    a = ap.parse_args()

    sys.stdout.reconfigure(encoding="utf-8")
    if a.stato:
        stampa_stato(Db())
        return
    if a.ritardo < RITARDO_MINIMO:
        ap.error(f"--ritardo sotto {RITARDO_MINIMO}s non e' consentito")
    _configura_log()

    solo = a.solo_elenco or a.solo_anagrafica or a.solo_modelli
    sezioni = [s.strip() for s in a.sezioni.split(",") if s.strip()]
    s = None
    inizio = time.time()
    try:
        s = Scaricatore(a.paralleli, a.ritardo)
        if a.solo_elenco or not solo:
            print("Fase 1: elenco prodotti")
            s.scarica_elenco(sezioni, a.aggiorna_elenco)
        if (a.solo_anagrafica or not solo) and not s.stop.is_set():
            print("Fase 2: anagrafica codici e codici precedenti")
            s.scarica_anagrafica()
        if (a.solo_modelli or not solo) and not s.stop.is_set():
            print("Fase 3: tavole di ogni modello")
            s.scarica_modelli(a.limite)
    except (KeyboardInterrupt, Interrotto):
        print("\nInterrotto: al prossimo avvio riparte da qui.")
    except CarraroNonRaggiungibile as e:
        print(f"\nPortale non raggiungibile, mi fermo: {e}")
    if s:
        s.riepilogo(inizio)


if __name__ == "__main__":
    main()
