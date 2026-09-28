"""
Scarica in locale l'intero catalogo SDF (SAME, Deutz-Fahr, Hurlimann,
Lamborghini) in fornitori/sdf/sdf.db, piu' modelli in parallelo.

Script separato dal bot: NON va usato mentre il server e' acceso (il
server scarica i modelli per conto suo e i due processi non si
coordinano: stessa sessione SDF, stessi modelli). Non modifica nulla del
codice del bot: usa lo stesso Crawler, quindi il risultato e' identico a
quello dei download fatti dal bot.

Cosa fa:
  1. elenco: scarica famiglie e modelli di ogni marca (tabelle family e
     model). Serve a sapere quanti modelli ci sono in tutto.
  2. download: mette in coda i modelli non ancora completi (prima quelli
     iniziati e interrotti) e ne scarica N alla volta con
     Crawler.crawl_model.

Interruzione e ripresa: Ctrl+C ferma tutto in pochi secondi (le richieste
in coda non partono piu'). Al riavvio riparte da dove era: il Crawler segna
nel DB gruppi e modelli finiti (crawl_state) e non riscarica le tavole gia'
presenti. Secondo Ctrl+C = uscita immediata (si perdono al massimo le
tavole che stavano arrivando, che verranno riscaricate).

Velocita': tutte le richieste passano da UN solo SdfClient, che ne lascia
partire al massimo una ogni --ritardo secondi (in totale, non per modello).
E' questo, non il numero di modelli in parallelo, a decidere la velocita':
i modelli in parallelo servono a tenere piena la coda mentre si aspettano
le risposte. Il portale e' del fornitore e l'account e' quello
dell'azienda: si scende col ritardo un passo alla volta, guardando il
riepilogo finale (richieste al secondo ed errori). Se il portale risponde
con errori di sovraccarico (429, 5xx) o non risponde, lo script aumenta da
solo il ritardo e riprova il modello piu' tardi.

Uso (dalla radice del progetto, con il server SPENTO):
  ./venv/Scripts/python.exe -m fornitori.sdf.scarica_tutto                      # 10 in parallelo, ritardo 0.4 s
  ./venv/Scripts/python.exe -m fornitori.sdf.scarica_tutto --ritardo 0.3        # un passo piu' veloce
  ./venv/Scripts/python.exe -m fornitori.sdf.scarica_tutto --limite 5           # prova: solo 5 modelli
  ./venv/Scripts/python.exe -m fornitori.sdf.scarica_tutto --marche SAME,LAMBORGHINI
  ./venv/Scripts/python.exe -m fornitori.sdf.scarica_tutto --aggiorna-elenco    # riscarica l'elenco modelli
  ./venv/Scripts/python.exe -m fornitori.sdf.scarica_tutto --stato              # solo il punto della situazione, dal DB

Errori dettagliati in dati/log/scarica_sdf.log.
"""
import argparse
import logging
import os
import queue
import sys
import threading
import time
from collections import Counter, deque
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED

import requests
from tqdm import tqdm

import config
from .api import SdfApi
from .client import BRANDS, SdfClient, SessionExpired, SdfNonRaggiungibile, _AdapterSdf
from .crawler import Crawler, MAX_WORKER
from .db import Db

log = logging.getLogger("sdf.scarica_tutto")

RITARDO_MINIMO = 0.05       # sotto non si scende nemmeno a mano
RITARDO_MASSIMO = 3.0       # tetto del rallentamento automatico
TENTATIVI_PER_MODELLO = 3   # poi il modello si lascia al prossimo avvio
ERRORI_DI_FILA_PER_FERMARSI = 5
STATUS_SOVRACCARICO = {429, 500, 502, 503, 504}


class Interrotto(BaseException):
    """Ctrl+C: BaseException e non Exception, cosi' il Crawler non la
    scambia per un errore qualsiasi (es. "sostituzioni non recuperate") e
    la lascia risalire fino a qui."""


# ------------------------------------------------------------------ stato
def stampa_stato(db, codici):
    """Punto della situazione letto solo dal DB: niente rete, niente login."""
    fatti = {r["key"] for r in db.query("SELECT key FROM crawl_state WHERE key LIKE 'mod:%'")}
    incompleti = Counter(r["brand"] for r in db.modelli_incompleti())
    tot_fatti = tot_modelli = 0
    print(f"{'marca':12} {'elenco':>8} {'scaricati':>10} {'totale':>7} {'a meta':>7}")
    for code in codici:
        modelli = [r["row_id"] for r in db.query("SELECT row_id FROM model WHERE brand=?", (code,))]
        n_fatti = sum(1 for m in modelli if f"mod:{code}:{m}" in fatti)
        elenco = "completo" if db.is_done(f"elenco:{code}") else "parziale"
        print(f"{code:12} {elenco:>8} {n_fatti:10d} {len(modelli):7d} {incompleti[code]:7d}")
        tot_fatti += n_fatti
        tot_modelli += len(modelli)
    s = db.stats()
    mb = os.path.getsize(db.path) / 1e6
    pct = 100 * tot_fatti / tot_modelli if tot_modelli else 0
    print(f"\nmodelli scaricati: {tot_fatti}/{tot_modelli} ({pct:.1f}%)")
    print(f"tavole: {s['drawing']}   righe ricambio: {s['part']}   "
          f"sostituzioni: {s['substitution']}   DB: {mb:.0f} MB")


# ------------------------------------------------------------ scaricatore
class Scaricatore:
    def __init__(self, paralleli, ritardo):
        self.stop = threading.Event()
        self.client = SdfClient(
            cookie=config.leggi_credenziale("cookie.txt", "SDF_COOKIE"),
            username=config.leggi_credenziale("sdf_username.txt", "SDF_USERNAME"),
            password=config.leggi_credenziale("sdf_password.txt", "SDF_PASSWORD"),
            delay=ritardo)
        # un solo pool di connessioni per tutti i thread (paralleli x
        # MAX_WORKER del Crawler): con quello di default (10) requests
        # butterebbe via connessioni in continuazione
        self.client.s.mount("https://", _AdapterSdf(pool_connections=4,
                                                    pool_maxsize=paralleli * MAX_WORKER + 4))
        self._conta_richieste()
        self.api = SdfApi(self.client)
        self.db = Db()
        self.crawler = Crawler(self.api, self.db, verbose=False)
        self.paralleli = paralleli
        self.errori = Counter()
        self._lk = threading.Lock()
        self._richieste = deque()   # istanti delle richieste dell'ultimo minuto
        self.n_richieste = 0

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

    # ------------------------------------------------------------ elenco
    def scarica_elenco(self, codici, aggiorna):
        for code in codici:
            if self.stop.is_set():
                return
            if self.db.is_done(f"elenco:{code}") and not aggiorna:
                continue
            famiglie = self.api.families(brand=code)
            self.db.save_families(code, famiglie)
            for f in tqdm(famiglie, desc=f"elenco {code}", unit="famiglie", leave=False):
                chiave = f"elenco:{code}:{f['row_id']}"
                if self.db.is_done(chiave) and not aggiorna:
                    continue
                self.db.save_models(code, self.api.models(f["row_id"], brand=code))
                self.db.mark_done(chiave)
            self.db.mark_done(f"elenco:{code}")

    def da_scaricare(self, codici):
        """Modelli non completi: prima quelli gia' iniziati (hanno dati a meta'),
        poi gli altri per marca e famiglia."""
        fatti = {r["key"] for r in self.db.query("SELECT key FROM crawl_state WHERE key LIKE 'mod:%'")}
        iniziati = {(r["brand"], r["model_id"]) for r in self.db.modelli_incompleti()}
        coda = []
        for code in codici:
            for m in self.db.query("SELECT row_id, family_id, name FROM model WHERE brand=? "
                                   "ORDER BY family_id, row_id", (code,)):
                if f"mod:{code}:{m['row_id']}" not in fatti:
                    coda.append((code, m["family_id"], m["row_id"], m["name"] or ""))
        # (marca, modello) gia' iniziati prima: sort stabile, il resto non cambia ordine
        coda.sort(key=lambda m: (m[0], m[2]) not in iniziati)
        return coda

    # ----------------------------------------------------------- modello
    def scarica_modello(self, modello, posti):
        code, fam, mid, nome = modello
        posto = posti.get()
        # dynamic_ncols: la barra si adatta alla larghezza della finestra a ogni
        # aggiornamento. Con larghezza fissa, se la finestra e' piu' stretta
        # (o viene ridimensionata) le righe vanno a capo e tqdm ridisegna
        # le barre una sotto l'altra all'infinito.
        barra = tqdm(total=100, position=posto, leave=False, unit="%", dynamic_ncols=True,
                     desc=f"{code[:4]} {nome.split('->')[0].strip()[:24]:24}",
                     bar_format="{desc} {bar} {n:3.0f}%{postfix}")   # postfix inizia gia' con ", "

        def avanzamento(gruppi_fatti=0, gruppi_totali=0, gruppo="",
                        tavole_fatte=0, tavole_gruppo=0, etichetta=""):
            # stessa formula del bot (FornitoreSdf._scarica)
            frazione = 0.0
            if gruppi_totali:
                frazione = gruppi_fatti / gruppi_totali
                if tavole_gruppo:
                    frazione += (tavole_fatte / tavole_gruppo) / gruppi_totali
            barra.n = min(frazione, 1.0) * 100
            barra.set_postfix_str(f"{gruppi_fatti}/{gruppi_totali} gruppi {gruppo[:25]}", refresh=True)

        try:
            return self.crawler.crawl_model(code, fam, mid, progress=avanzamento)
        finally:
            barra.close()
            posti.put(posto)

    # --------------------------------------------------------------- via
    def esegui(self, codici, limite=None, aggiorna_elenco=False):
        inizio = time.time()
        self.scarica_elenco(codici, aggiorna_elenco)
        coda = self.da_scaricare(codici)
        if limite:
            coda = coda[:limite]
        tot = self.db.one("SELECT COUNT(*) AS n FROM model WHERE brand IN (%s)"
                          % ",".join("?" * len(codici)), codici)["n"]
        gia = tot - len(self.da_scaricare(codici))
        generale = tqdm(total=tot, initial=gia, position=0, unit="modelli",
                        desc="modelli SDF", dynamic_ncols=True)
        if not coda:
            generale.close()
            print("Niente da scaricare: catalogo completo per le marche scelte.")
            return

        posti = queue.Queue()
        for p in range(1, self.paralleli + 1):
            posti.put(p)
        tentativi = Counter()
        errori_di_fila = 0
        in_attesa = deque(coda)
        scaricate_righe = 0

        with ThreadPoolExecutor(max_workers=self.paralleli) as ex:
            attivi = {}

            def riempi():
                while in_attesa and len(attivi) < self.paralleli and not self.stop.is_set():
                    m = in_attesa.popleft()
                    attivi[ex.submit(self.scarica_modello, m, posti)] = m

            riempi()
            try:
                while attivi:
                    fatti, _ = wait(list(attivi), timeout=0.5, return_when=FIRST_COMPLETED)
                    generale.set_postfix_str(
                        f"{self.richieste_al_secondo():.1f} rich/s, ritardo {self.client.delay:.2f}s, "
                        f"errori {sum(self.errori.values())}", refresh=False)
                    generale.refresh()
                    for fut in fatti:
                        m = attivi.pop(fut)
                        try:
                            scaricate_righe += fut.result() or 0
                            generale.update(1)
                            errori_di_fila = 0
                        except Interrotto:
                            pass
                        except Exception as e:
                            errori_di_fila += 1
                            self._errore(m, e, tentativi, in_attesa)
                            if errori_di_fila >= ERRORI_DI_FILA_PER_FERMARSI:
                                tqdm.write(f"! {errori_di_fila} modelli falliti di fila: mi fermo. "
                                           "Controlla login e portale (dati/log/scarica_sdf.log).")
                                self.stop.set()
                    riempi()
            except KeyboardInterrupt:
                self.stop.set()
                tqdm.write("Interruzione: chiudo i download in corso "
                           "(di nuovo Ctrl+C per uscire subito)...")
                try:
                    wait(list(attivi))
                except KeyboardInterrupt:
                    os._exit(1)
        generale.close()
        self._riepilogo(inizio, scaricate_righe, codici)

    def _errore(self, m, e, tentativi, in_attesa):
        code, _, mid, nome = m
        tipo = type(e).__name__
        if isinstance(e, requests.HTTPError) and e.response is not None:
            tipo = f"HTTP {e.response.status_code}"
        self.errori[tipo] += 1
        log.error("modello %s %s (%s): %r", code, mid, nome, e, exc_info=e)

        if isinstance(e, SessionExpired):
            tqdm.write(f"! sessione SDF non rinnovabile: mi fermo ({e})")
            self.stop.set()
            return
        sovraccarico = isinstance(e, SdfNonRaggiungibile) or (
            isinstance(e, requests.HTTPError) and e.response is not None
            and e.response.status_code in STATUS_SOVRACCARICO)
        if sovraccarico:
            self.rallenta(f"{tipo} su {nome[:30]}")
        tentativi[m] += 1
        if tentativi[m] < TENTATIVI_PER_MODELLO:
            in_attesa.append(m)          # riprova in fondo alla coda
        else:
            tqdm.write(f"! {nome[:40]}: {tipo} per {TENTATIVI_PER_MODELLO} volte, "
                       "lo lascio al prossimo avvio")

    def _riepilogo(self, inizio, righe, codici):
        durata = time.time() - inizio
        print(f"\ndurata {durata / 60:.1f} min, {self.n_richieste} richieste "
              f"({self.n_richieste / max(durata, 1):.2f} al secondo), {righe} righe ricambio nuove")
        print(f"ritardo finale {self.client.delay:.2f}s")
        if self.errori:
            print("errori: " + ", ".join(f"{k} x{v}" for k, v in self.errori.most_common()))
        print()
        stampa_stato(self.db, codici)


# -------------------------------------------------------------------- main
def _configura_log():
    """Solo su file: a schermo ci sono le barre, un log a video le romperebbe."""
    cartella = config.DATI / "log"
    cartella.mkdir(exist_ok=True)
    h = logging.FileHandler(cartella / "scarica_sdf.log", encoding="utf-8")
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    radice = logging.getLogger()
    radice.handlers[:] = [h]
    radice.setLevel(logging.INFO)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--paralleli", type=int, default=10, help="modelli scaricati insieme (default 10)")
    ap.add_argument("--ritardo", type=float, default=0.4,
                    help="secondi minimi tra due richieste al portale, in totale (default 0.4)")
    ap.add_argument("--marche", default=",".join(BRANDS),
                    help="es. SAME,LAMBORGHINI (default: tutte)")
    ap.add_argument("--limite", type=int, help="scarica al massimo N modelli (per le prove)")
    ap.add_argument("--aggiorna-elenco", action="store_true",
                    help="riscarica l'elenco famiglie/modelli (nuovi modelli sul portale)")
    ap.add_argument("--stato", action="store_true", help="mostra solo a che punto e' il download")
    a = ap.parse_args()

    sys.stdout.reconfigure(encoding="utf-8")
    marche = [m.strip().upper() for m in a.marche.split(",") if m.strip()]
    sconosciute = [m for m in marche if m not in BRANDS]
    if sconosciute:
        ap.error(f"marche sconosciute: {sconosciute}; possibili: {list(BRANDS)}")
    codici = [BRANDS[m] for m in marche]

    if a.stato:
        stampa_stato(Db(), codici)
        return
    if a.ritardo < RITARDO_MINIMO:
        ap.error(f"--ritardo sotto {RITARDO_MINIMO}s non e' consentito")
    _configura_log()
    try:
        Scaricatore(a.paralleli, a.ritardo).esegui(codici, a.limite, a.aggiorna_elenco)
    except KeyboardInterrupt:
        # Ctrl+C durante l'elenco modelli (quello dei download e' gestito in esegui)
        print("\nInterrotto: al prossimo avvio riparte da qui.")


if __name__ == "__main__":
    main()
