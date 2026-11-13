"""
Scarica in locale il catalogo ricambi Maschio Gaspardo in
fornitori/maschio/maschio.db.

Fasi (ognuna riprende da dove si era fermata):
  1. elenco:     linee -> categorie -> modelli, l'elenco di tutte le release
                 (cataloghi) e il legame release-modello. ~500 richieste.
  2. tavole:     per ogni release, l'elenco delle sue tavole. ~2.000 richieste.
  3. posizioni:  per ogni tavola, l'SVG del disegno e poi UNA richiesta per
                 posizione (getPartNumber: e' cosi' che funziona il portale).
                 E' la fase lunga: ~50.000 tavole x ~35 posizioni, ~5 s
                 a tavola per sessione.
  4. anagrafica: tutti i codici (descrizione, sostituto, peso, URL foto),
                 per prefisso di codice. ~4.500 pagine, in sequenza: la
                 ricerca resta salvata nella sessione del portale, quindi
                 non si parallelizza. Si lancia a parte (--solo-anagrafica),
                 NON mentre gira la fase 3: sarebbe un secondo login
                 contemporaneo sull'account dell'azienda.

Interruzione e ripresa: Ctrl+C ferma dopo le richieste in corso (al
massimo ~1 min); un secondo Ctrl+C esce subito. Ogni tavola si salva in
una sola transazione con tutte le sue posizioni, quindi non ne restano di
salvate a meta'.

Velocita' (fase 3): --sessioni login contemporanei (default 3), un
lavoratore per sessione, ognuno con le sue tavole. Il portale esegue una
richiesta alla volta PER SESSIONE (session lock di classic ASP), quindi il
parallelismo vero viene solo da sessioni diverse. Ogni sessione lascia
partire al massimo una richiesta ogni --ritardo secondi; in caso di errori
il ritardo aumenta da solo.

Tavole bloccanti: per alcune tavole il portale non risponde mai (es.
161501: muta anche dopo 10 min). La richiesta resta attiva sul server anche
se chiudiamo la connessione, tiene bloccata la sessione e occupa un
processo del server. Quindi, al primo timeout: la tavola viene segnata come
bloccante ed esclusa per sempre (--riprova-bloccanti per ritentarle),
TUTTI i lavoratori si fermano per una pausa di sicurezza e ripartono solo
se il portale risponde, la sessione viene abbandonata (con un logout che
parte appena il server la libera) e si rifa' il login. Oltre
--max-bloccate tavole bloccanti in un lancio (default 3) ci si ferma.

Uso (dalla radice del progetto):
  ./venv/Scripts/python.exe -m fornitori.maschio.scarica_tutto                   # fasi 1-3
  ./venv/Scripts/python.exe -m fornitori.maschio.scarica_tutto --limite 20       # prova: solo 20 tavole
  ./venv/Scripts/python.exe -m fornitori.maschio.scarica_tutto --sessioni 3 --ritardo 0.15
  ./venv/Scripts/python.exe -m fornitori.maschio.scarica_tutto --debug           # log di ogni richiesta
  ./venv/Scripts/python.exe -m fornitori.maschio.scarica_tutto --riprova-bloccanti
  ./venv/Scripts/python.exe -m fornitori.maschio.scarica_tutto --solo-elenco
  ./venv/Scripts/python.exe -m fornitori.maschio.scarica_tutto --solo-anagrafica
  ./venv/Scripts/python.exe -m fornitori.maschio.scarica_tutto --aggiorna-elenco
  ./venv/Scripts/python.exe -m fornitori.maschio.scarica_tutto --stato

Errori dettagliati in dati/log/scarica_maschio.log.
"""

import argparse
import logging
import os
import queue
import string
import sys
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlencode

import requests
from tqdm import tqdm

import config
from . import parser
from .client import (AJAX_ESPLOSI, BASE, MaschioClient, MaschioNonRaggiungibile,
                     RichiestaAppesa, SessioneScaduta, portale_risponde)
from .db import Db

log = logging.getLogger("maschio.scarica_tutto")

RITARDO_MASSIMO = 2.0
ERRORI_DI_FILA_PER_FERMARSI = 20
PAUSA_DOPO_APPESA = 60      # s di pausa per TUTTI dopo una richiesta appesa
CONTROLLI_PORTALE = 3       # poi, se il portale non risponde ancora, ci si ferma
WATCHDOG = 120              # s senza tavole completate -> stack dei thread nel log
PREFISSI = list(string.ascii_uppercase + string.digits)

# parametri vuoti che il browser manda sempre a act=table
_FILTRI_VUOTI = dict(showAll=1, selectedLine="", selectedCategory="",
                     selectedModel="", documentCode="", serialNumber="",
                     partNumber="", product="", searchModel="",
                     searchModelType="", offsetRows=15)


class Interrotto(BaseException):
    """Ctrl+C: BaseException, cosi' non viene presa per un errore qualsiasi."""


def url_disegno(cartella, table_id):
    return (BASE + AJAX_ESPLOSI + "?" +
            urlencode({"act": "load", "dir": cartella, "tableId": table_id}))


def _con_freno_interrompibile(client, stop):
    """Dopo Ctrl+C le richieste non ancora partite non partono piu'."""
    originale = client._throttle

    def freno():
        if stop.is_set():
            raise Interrotto()
        originale()
    client._throttle = freno


# ----------------------------------------------------------------------
# Fase 1: elenco
# ----------------------------------------------------------------------
def _tutte_le_pagine(client, **filtri):
    params = dict(_FILTRI_VUOTI, **filtri)
    html = client.esplosi("table", page=1, **params)
    out = parser.releases(html)
    _, pagine, record = parser.paginazione(html)
    for p in range(2, pagine + 1):
        out += parser.releases(client.esplosi("table", page=p, **params))
    # il portale elenca a volte la stessa release due volte: il confronto
    # col totale dichiarato si fa sulle righe, poi si deduplica
    if record and len(out) != record:
        log.warning("table %s: attese %d righe, lette %d", filtri, record, len(out))
    return list({r["id"]: r for r in out}.values())


def fase_elenco(client, db, aggiorna=False):
    if db.fatto("elenco") and not aggiorna:
        tqdm.write("Elenco: gia' fatto (usa --aggiorna-elenco per rileggerlo)")
        return
    tutte = _tutte_le_pagine(client)
    db.salva_release(tutte)
    tqdm.write(f"Elenco: {len(tutte)} release in totale")

    vuoti = dict(showAll=1, documentCode="", serialNumber="", partNumber="")
    linee = parser.lista_json(client.esplosi("lines", **vuoti))
    db.salva_linee(linee)
    # il totale dei modelli si scopre strada facendo: la barra si allunga
    barra = tqdm(total=0, desc="elenco modelli", unit="modelli", dynamic_ncols=True)
    try:
        for linea in linee:
            cats = parser.lista_json(client.esplosi(
                "categories", selectedLine=linea["id"], **vuoti))
            db.salva_categorie(linea["id"], cats)
            for cat in cats:
                mods = parser.lista_json(client.esplosi(
                    "models", selectedLine=linea["id"], selectedCategory=cat["id"], **vuoti))
                db.salva_modelli(linea["id"], cat["id"], mods)
                barra.total += len(mods)
                barra.set_postfix_str(f"{linea['nome'][:20]} / {cat['nome'][:20]}")
                for mod in mods:
                    chiave = f"mod:{linea['id']}:{cat['id']}:{mod['id']}"
                    if not (db.fatto(chiave) and not aggiorna):
                        rels = _tutte_le_pagine(client, selectedLine=linea["id"],
                                                selectedCategory=cat["id"],
                                                selectedModel=mod["id"])
                        db.lega_release_modello(linea["id"], cat["id"], mod["id"], rels)
                        db.segna_fatto(chiave)
                    barra.update(1)
    finally:
        barra.close()
    db.segna_fatto("elenco")


# ----------------------------------------------------------------------
# Fase 2: tavole di ogni release
# ----------------------------------------------------------------------
def fase_tavole(client, db, stop, limite=None):
    da_fare = db.release_da_leggere(limite)
    if not da_fare:
        tqdm.write("Tavole: elenco gia' letto per tutte le release")
        return
    errori = 0
    barra = tqdm(da_fare, desc="elenco tavole", unit="release", dynamic_ncols=True)
    try:
        for r in barra:
            if stop.is_set():
                raise Interrotto()
            try:
                cartella, tavole = parser.viewer(client.viewer(r["id"]))
            except (requests.HTTPError, MaschioNonRaggiungibile) as e:
                errori += 1
                barra.set_postfix_str(f"errori {errori}")
                log.warning("release %s: %s", r["id"], e)
                continue
            if not cartella:
                log.warning("release %s (%s %s): cartella disegni non trovata",
                            r["id"], r["codice_doc"], r["data"])
            if not tavole:
                log.warning("release %s (%s %s): nessuna tavola", r["id"],
                            r["codice_doc"], r["data"])
            db.salva_tavole_release(r["id"], cartella, tavole, url_disegno)
    finally:
        barra.close()


# ----------------------------------------------------------------------
# Fase 3: posizioni di ogni tavola
# ----------------------------------------------------------------------
def _scarica_tavola(client, ex, t):
    """Una tavola, su UNA sessione: prima il disegno, DA SOLO, poi i codici.

    Il portale (classic ASP) esegue una richiesta alla volta per sessione:
    misurato, una getPartNumber in coda dietro a un SVG pesante aspetta
    ~10 s. Quindi su una sessione i due tipi di richiesta non si mescolano
    mai. Le getPartNumber invece sono minuscole, e in parallelo (anche se
    il server le esegue in fila) nascondono la latenza di rete.
    Il parallelismo vero viene dalle sessioni (--sessioni): un lavoratore
    per sessione, ognuno con le sue tavole.

    L'SVG si legge solo per sapere quali numeri sono cliccabili e poi si
    scarta: nel DB resta solo il suo URL (tavole.url_disegno).
    Restituisce (posizioni, dettagli per il log)."""
    t0 = time.time()
    svg = client.svg_tavola(t["cartella"], t["id"])
    t_svg = time.time() - t0
    posizioni = parser.posizioni_svg(svg)
    futuri = [ex.submit(client.codice_posizione, p[0], p[1], p[2]) for p in posizioni]
    try:
        codici = [f.result() for f in futuri]
    except BaseException:
        # una richiesta appesa o Ctrl+C: le altre in coda non partono piu'
        for f in futuri:
            f.cancel()
        raise
    pos = [(riga, etichetta, codice)
           for (_, _, riga, etichetta), codice in zip(posizioni, codici)]
    return pos, dict(kb=len(svg) // 1000, t_svg=t_svg,
                     t_codici=time.time() - t0 - t_svg)


class _Lavoro:
    """Stato condiviso dai lavoratori della fase 3 (uno per sessione)."""

    def __init__(self, db, da_fare, stop, barra, clients, max_bloccate):
        self.db, self.stop, self.barra, self.clients = db, stop, barra, clients
        self.max_bloccate = max_bloccate
        self.coda = queue.SimpleQueue()
        for t in da_fare:
            self.coda.put(t)
        self.lk = threading.Lock()
        self.lk_freno = threading.Lock()
        self.via_libera = threading.Event()
        self.via_libera.set()
        self.t0 = time.time()
        self.r0 = sum(c.n_richieste for c in clients)
        self.posizioni = self.errori = self.errori_di_fila = self.bloccate = 0
        self.ultimo_progresso = time.time()

    # ------------------------------------------------------------------
    def ferma(self, motivo):
        tqdm.write("! " + motivo)
        log.error(motivo)
        self.stop.set()

    def attendi_via_libera(self):
        """Prima di ogni tavola: aspetta se e' in corso una pausa di sicurezza."""
        while not self.via_libera.wait(0.5):
            if self.stop.is_set():
                raise Interrotto()
        if self.stop.is_set():
            raise Interrotto()

    def aggiorna_barra(self):
        rps = ((sum(c.n_richieste for c in self.clients) - self.r0)
               / max(time.time() - self.t0, 1e-6))
        self.barra.set_postfix_str(
            f"{self.posizioni} posizioni, {rps:.1f} rich/s, errori {self.errori}, "
            f"bloccanti {self.bloccate}", refresh=False)

    # ------------------------------------------------------------------
    def fatta(self, nome, t, pos, d, durata):
        if not pos:
            log.info("tavola %s (release %s): nessuna posizione", t["id"], t["release"])
        self.db.salva_posizioni(t["id"], pos)
        with self.lk:
            self.errori_di_fila = 0
            self.posizioni += len(pos)
            self.ultimo_progresso = time.time()
            self.aggiorna_barra()
            self.barra.update(1)
        log.info("%s tavola %s (release %s): svg %d KB in %.1fs, %d posizioni, "
                 "codici in %.1fs, totale %.1fs", nome, t["id"], t["release"],
                 d["kb"], d["t_svg"], len(pos), d["t_codici"], durata)

    def errore(self, nome, client, t, e):
        with self.lk:
            self.errori += 1
            self.errori_di_fila += 1
            di_fila = self.errori_di_fila
            self.aggiorna_barra()
        prima = client.delay
        client.delay = min(RITARDO_MASSIMO, max(client.delay, 0.05) * 1.5)
        log.warning("%s tavola %s (release %s): %s: %s -> ritardo %.2fs", nome,
                    t["id"], t["release"], type(e).__name__, e, client.delay)
        tqdm.write(f"! {nome} tavola {t['id']}: {type(e).__name__}, "
                   f"ritardo {prima:.2f}s -> {client.delay:.2f}s")
        if di_fila >= ERRORI_DI_FILA_PER_FERMARSI:
            self.ferma(f"{di_fila} errori di fila: mi fermo. "
                       "Vedi dati/log/scarica_maschio.log")

    def appesa(self, nome, client, t, e):
        """Il portale non risponde su questa tavola. La richiesta resta
        attiva sul server (non si puo' interrompere da qui: misurato, anche
        chiudendo la connessione la sessione resta bloccata) e occupa uno
        dei suoi processi: la tavola si esclude per sempre, si frena tutto e
        si cambia sessione. Al massimo max_bloccate volte per lancio."""
        with self.lk:
            self.bloccate += 1
            n = self.bloccate
            self.aggiorna_barra()
        self.db.segna_bloccata(t["id"], f"{type(e).__name__}: {e}"[:300])
        log.warning("%s tavola %s (release %s, %s) BLOCCANTE (%d/%d): %s", nome,
                    t["id"], t["release"], t["cartella"], n, self.max_bloccate, e)
        tqdm.write(f"! {nome} tavola {t['id']}: il portale non risponde. Segnata come "
                   f"bloccante ed esclusa ({n}/{self.max_bloccate} in questo lancio)")
        if n >= self.max_bloccate:
            self.ferma(f"{n} tavole bloccanti in questo lancio: mi fermo per non "
                       "sovraccaricare il portale (--max-bloccate per cambiare il limite)")
            return
        self.freno_di_emergenza()
        self.attendi_via_libera()
        client.abbandona_sessione()

    def freno_di_emergenza(self):
        """Ferma TUTTI i lavoratori, aspetta, riparte solo se il portale
        risponde (richiesta leggera senza sessione). Se c'e' gia' una
        pausa in corso, non ne parte un'altra."""
        if not self.lk_freno.acquire(blocking=False):
            return
        try:
            self.via_libera.clear()
            for i in range(1, CONTROLLI_PORTALE + 1):
                tqdm.write(f"  pausa di sicurezza {PAUSA_DOPO_APPESA}s, poi controllo "
                           f"se il portale risponde ({i}/{CONTROLLI_PORTALE})")
                if self.stop.wait(PAUSA_DOPO_APPESA):
                    return
                if portale_risponde():
                    log.info("freno: il portale risponde, riparto")
                    tqdm.write("  il portale risponde: riparto")
                    return
                log.warning("freno: il portale NON risponde (controllo %d)", i)
            self.ferma(f"il portale non risponde da {CONTROLLI_PORTALE * PAUSA_DOPO_APPESA}s: "
                       "mi fermo. Riprova piu' tardi")
        finally:
            self.via_libera.set()
            self.lk_freno.release()

    # ------------------------------------------------------------------
    def lavoratore(self, nome, client, paralleli):
        try:
            with ThreadPoolExecutor(paralleli, thread_name_prefix=f"{nome}-codici") as ex:
                while not self.stop.is_set():
                    self.attendi_via_libera()
                    try:
                        t = self.coda.get_nowait()
                    except queue.Empty:
                        return
                    t0 = time.time()
                    try:
                        pos, d = _scarica_tavola(client, ex, t)
                    except RichiestaAppesa as e:
                        self.appesa(nome, client, t, e)
                        continue
                    except (requests.HTTPError, MaschioNonRaggiungibile,
                            SessioneScaduta) as e:
                        self.errore(nome, client, t, e)
                        continue
                    self.fatta(nome, t, pos, d, time.time() - t0)
        except Interrotto:
            pass
        except Exception as e:  # noqa: BLE001 - un lavoratore non muore in silenzio
            log.exception("%s: errore imprevisto", nome)
            self.ferma(f"{nome}: errore imprevisto {type(e).__name__}: {e}")

    def sentinella(self, finito):
        """Se per WATCHDOG secondi non si completa nessuna tavola, scrive nel
        log cosa sta facendo ogni thread: serve a capire dove si blocca."""
        ultimo_dump = 0.0
        while not finito.wait(15):
            fermo = time.time() - self.ultimo_progresso
            if fermo > WATCHDOG and time.time() - ultimo_dump > WATCHDOG:
                ultimo_dump = time.time()
                tqdm.write(f"  (nessuna tavola completata da {fermo:.0f}s: "
                           "stato dei thread nel log)")
                _scrivi_stack_thread(f"nessuna tavola completata da {fermo:.0f}s")


def _scrivi_stack_thread(motivo):
    frames = sys._current_frames()
    righe = [f"=== stack dei thread: {motivo} ==="]
    for th in threading.enumerate():
        f = frames.get(th.ident)
        if f is None:
            continue
        righe.append(f"--- {th.name}")
        righe += [r.rstrip() for r in traceback.format_stack(f)[-6:]]
    log.warning("\n".join(righe))


def fase_posizioni(client, db, stop, paralleli, sessioni, max_bloccate, limite=None):
    da_fare = db.tavole_da_scaricare(limite)
    if not da_fare:
        tqdm.write("Posizioni: nessuna tavola da scaricare")
        return
    s = db.statistiche()
    # come SDF: la barra parte da quanto gia' fatto nei lanci precedenti
    # (con --limite conta solo la prova)
    totale = len(da_fare) if limite else s["tavole"] - s["tavole_bloccate"]
    gia = 0 if limite else s["tavole_scaricate"]
    sessioni = max(1, min(sessioni, len(da_fare)))
    tqdm.write(f"Posizioni: {len(da_fare)} tavole da scaricare, {sessioni} sessioni, "
               f"codici {paralleli} in parallelo per sessione, ritardo {client.delay}s "
               f"per sessione, al massimo {max_bloccate} tavole bloccanti"
               + (f" (gia' escluse: {s['tavole_bloccate']})" if s["tavole_bloccate"] else ""))

    # la prima sessione e' quella gia' aperta per le fasi 1-2
    clients = [client]
    try:
        for _ in range(1, sessioni):
            if stop.is_set():
                raise Interrotto()
            c = MaschioClient(delay=client.delay, pool=paralleli + 4)
            _con_freno_interrompibile(c, stop)
            clients.append(c)
        barra = tqdm(total=totale, initial=gia, desc="tavole Maschio", unit="tavole",
                     dynamic_ncols=True)
        lav = _Lavoro(db, da_fare, stop, barra, clients, max_bloccate)
        finito = threading.Event()
        threading.Thread(target=lav.sentinella, args=(finito,), name="sentinella",
                         daemon=True).start()
        lavoratori = [threading.Thread(target=lav.lavoratore, name=f"s{i}",
                                       args=(f"s{i}", c, paralleli), daemon=True)
                      for i, c in enumerate(clients, 1)]
        try:
            for th in lavoratori:
                th.start()
            for th in lavoratori:
                th.join()
        finally:
            finito.set()
            barra.close()
    finally:
        # logout delle sessioni aggiunte (la prima la chiude main)
        _chiudi_in_parallelo(clients[1:])


def _chiudi_in_parallelo(clients):
    ths = [threading.Thread(target=c.chiudi, daemon=True) for c in clients]
    for th in ths:
        th.start()
    for th in ths:
        th.join(20)


# ----------------------------------------------------------------------
# Fase 4: anagrafica
# ----------------------------------------------------------------------
def _prefisso(client, db, prefisso, stop):
    html = client.anagrafica_cerca(prefisso)
    _, pagine, record = parser.paginazione(html)
    letti = parser.anagrafica(html)
    db.salva_ricambi(letti)
    n, p = len(letti), 2
    barra = tqdm(total=pagine, initial=1, desc=f"  prefisso {prefisso}", unit="pagine",
                 position=1, leave=False, dynamic_ncols=True)
    try:
        while p <= pagine:
            if stop.is_set():
                raise Interrotto()
            try:
                html = client.anagrafica_pagina(p)
            except SessioneScaduta:
                # nuovo login = ricerca persa: la si rifa' e si riprende da p
                client.anagrafica_cerca(prefisso)
                continue
            corrente, _, _ = parser.paginazione(html)
            if corrente != p:
                log.warning("anagrafica %s: chiesta pagina %d, ricevuta %d: rifaccio "
                            "la ricerca", prefisso, p, corrente)
                client.anagrafica_cerca(prefisso)
                continue
            letti = parser.anagrafica(html)
            db.salva_ricambi(letti)
            n += len(letti)
            p += 1
            barra.update(1)
    finally:
        barra.close()
    if record and n != record:
        log.warning("anagrafica %s: attesi %d codici, letti %d", prefisso, record, n)
    return n, record


def fase_anagrafica(client, db, stop, aggiorna=False):
    gia = 0 if aggiorna else sum(db.fatto(f"ana:{p}") for p in PREFISSI)
    barra = tqdm(total=len(PREFISSI), initial=gia, desc="anagrafica", unit="prefissi",
                 position=0, dynamic_ncols=True)
    codici = 0
    try:
        for pre in PREFISSI:
            chiave = f"ana:{pre}"
            if db.fatto(chiave) and not aggiorna:
                continue
            n, record = _prefisso(client, db, pre, stop)
            db.segna_fatto(chiave)
            codici += n
            barra.set_postfix_str(f"{codici} codici letti", refresh=False)
            barra.update(1)
    finally:
        barra.close()


# ----------------------------------------------------------------------
def stampa_stato(db):
    s = db.statistiche()
    mb = os.path.getsize(db.path) / 1e6
    print("--- stato maschio.db ---")
    print(f"linee: {s['linee']}   modelli: {s['modelli']}")
    print(f"release: {s['release']} (tavole lette: {s['release_lette']}, "
          f"senza modello: {s['release_senza_modello']})")
    pct = 100 * s["tavole_scaricate"] / s["tavole"] if s["tavole"] else 0
    print(f"tavole: {s['tavole_scaricate']}/{s['tavole']} scaricate ({pct:.1f}%)")
    bloccate = db.tavole_bloccate()
    if bloccate:
        print(f"tavole bloccanti (escluse, --riprova-bloccanti per ritentarle): "
              f"{len(bloccate)}")
        for t in bloccate:
            print(f"   {t['id']} release {t['release']} {t['cartella']} "
                  f"{t['numero']} {t['titolo']} [{t['bloccata_il']}]")
    print(f"posizioni: {s['posizioni']} (senza codice: {s['posizioni_senza_codice']})")
    print(f"ricambi in anagrafica: {s['ricambi']} (con foto: {s['ricambi_con_foto']})")
    fatti = [p for p in PREFISSI if db.fatto(f"ana:{p}")]
    print(f"prefissi anagrafica completati: {len(fatti)}/{len(PREFISSI)}")
    print(f"DB: {mb:.0f} MB")


def _configura_log(debug=False):
    cartella = config.DATI / "log"
    cartella.mkdir(parents=True, exist_ok=True)
    h = logging.FileHandler(cartella / "scarica_maschio.log", encoding="utf-8")
    h.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s %(threadName)s %(name)s: %(message)s"))
    radice = logging.getLogger()
    # solo su file, come SDF: righe di log a schermo romperebbero le barre
    radice.handlers[:] = [h]
    radice.setLevel(logging.INFO)
    if debug:
        # una riga per ogni richiesta: metodo, URL, stato, ms, KB
        logging.getLogger("maschio.client").setLevel(logging.DEBUG)


def _esegui(a, db, stop, esito):
    """Tutto il lavoro di rete, in un thread a parte: il thread principale
    resta libero di ricevere Ctrl+C (su Windows una lettura di rete in
    corso nel thread principale non si lascia interrompere)."""
    client = None
    try:
        client = MaschioClient(delay=a.ritardo, pool=a.paralleli + 4)
        _con_freno_interrompibile(client, stop)
        esito["client"] = client
        if a.solo_anagrafica:
            fase_anagrafica(client, db, stop, a.aggiorna_anagrafica)
        else:
            fase_elenco(client, db, a.aggiorna_elenco)
            if not a.solo_elenco:
                fase_tavole(client, db, stop, a.limite)
                fase_posizioni(client, db, stop, a.paralleli, a.sessioni,
                               a.max_bloccate, a.limite)
    except Interrotto:
        pass
    except BaseException as e:  # noqa: BLE001 - riportato dal thread principale
        log.exception("errore")
        esito["errore"] = e
    finally:
        if client is not None:
            client.chiudi()


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sessioni", type=int, default=3,
                    help="login contemporanei (lavoratori in parallelo) nella fase 3")
    ap.add_argument("--paralleli", type=int, default=5,
                    help="richieste di codici in parallelo dentro una tavola, per sessione")
    ap.add_argument("--ritardo", type=float, default=0.15,
                    help="pausa minima fra due richieste, per sessione")
    ap.add_argument("--max-bloccate", type=int, default=3,
                    help="tavole bloccanti al massimo per lancio, poi ci si ferma")
    ap.add_argument("--riprova-bloccanti", action="store_true",
                    help="rimette in coda le tavole segnate come bloccanti")
    ap.add_argument("--limite", type=int,
                    help="prova: legge le tavole di sole N release e scarica solo N tavole")
    ap.add_argument("--solo-elenco", action="store_true")
    ap.add_argument("--solo-anagrafica", action="store_true")
    ap.add_argument("--aggiorna-elenco", action="store_true")
    ap.add_argument("--aggiorna-anagrafica", action="store_true")
    ap.add_argument("--stato", action="store_true")
    ap.add_argument("--debug", action="store_true",
                    help="nel log anche ogni singola richiesta con i suoi tempi")
    a = ap.parse_args()

    db = Db()
    if a.stato:
        stampa_stato(db)
        return
    _configura_log(a.debug)
    log.info("=== avvio: %s", vars(a))
    if a.riprova_bloccanti:
        print(f"Tavole bloccanti rimesse in coda: {db.sblocca_tavole()}")
    stop = threading.Event()
    esito = {}
    t0 = time.time()
    lavoro = threading.Thread(target=_esegui, args=(a, db, stop, esito),
                              name="download", daemon=True)
    lavoro.start()
    try:
        while lavoro.is_alive():
            lavoro.join(0.5)
    except KeyboardInterrupt:
        stop.set()
        log.info("Ctrl+C: interruzione richiesta")
        print("\nInterruzione: finisco le richieste in corso (al massimo ~1 min). "
              "Ctrl+C di nuovo per uscire subito.", flush=True)
        try:
            while lavoro.is_alive():
                lavoro.join(0.5)
        except KeyboardInterrupt:
            log.info("secondo Ctrl+C: uscita immediata")
            print("Uscita immediata (le tavole a meta' si riscaricano al prossimo "
                  "avvio).", flush=True)
            os._exit(1)
        print("Interrotto: riparte da qui al prossimo avvio.", flush=True)
    if "errore" in esito:
        e = esito["errore"]
        print(f"\nErrore: {type(e).__name__}: {e} (dettagli in dati/log/scarica_maschio.log)")
    client = esito.get("client")
    if client is not None:
        print(f"\n{client.n_richieste} richieste (sessione principale) in "
              f"{(time.time() - t0) / 60:.1f} min")
    stampa_stato(db)


if __name__ == "__main__":
    main()
