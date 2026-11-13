"""
Diagnosi lentezza/blocchi del download Maschio (fase 3, posizioni).

Fa UN login e scarica gli SVG delle tavole indicate (default: le prime N
in coda in maschio.db) in STREAMING, registrando:
  - tempo al primo byte (= quanto il server "pensa" prima di rispondere,
    compresa l'attesa del session lock ASP)
  - avanzamento ogni MB e velocita'
  - tempo totale, dimensione, numero di posizioni trovate
  - tempo di una getPartNumber subito dopo
Ogni richiesta ha un TETTO di tempo totale (--tetto, default 120 s): oltre,
si abbandona e si passa alla successiva (requests da solo ha solo il
timeout "fra un byte e l'altro", non sul totale).

Tutto va a schermo e in dati/log/diagnosi_maschio.log.

Uso (dalla radice del progetto):
  ./venv/Scripts/python.exe debug/diagnosi_maschio.py              # prime 5 in coda
  ./venv/Scripts/python.exe debug/diagnosi_maschio.py -n 10
  ./venv/Scripts/python.exe debug/diagnosi_maschio.py --tavole 161501 174858
NON lanciarlo mentre gira scarica_tutto (sarebbe un secondo login).
"""

import argparse
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
from fornitori.maschio import parser  # noqa: E402
from fornitori.maschio.client import AJAX_ESPLOSI, BASE, MaschioClient  # noqa: E402
from fornitori.maschio.db import Db  # noqa: E402

log = logging.getLogger("diagnosi")


def _configura_log():
    cartella = config.DATI / "log"
    cartella.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(message)s", "%H:%M:%S")
    f = logging.FileHandler(cartella / "diagnosi_maschio.log", encoding="utf-8")
    s = logging.StreamHandler(sys.stdout)
    for h in (f, s):
        h.setFormatter(fmt)
    log.handlers[:] = [f, s]
    log.setLevel(logging.INFO)
    log.propagate = False


def svg_streaming(client, cartella, table_id, tetto):
    """(testo o None, dettagli). Abbandona oltre `tetto` secondi totali."""
    t0 = time.time()
    r = client.s.get(BASE + AJAX_ESPLOSI,
                     params={"act": "load", "dir": cartella, "tableId": table_id},
                     stream=True, timeout=(15, 30), allow_redirects=False)
    t_header = time.time() - t0
    log.info("   header dopo %.1fs: HTTP %s, Content-Type=%s, Content-Length=%s, "
             "Transfer-Encoding=%s", t_header, r.status_code,
             r.headers.get("Content-Type"), r.headers.get("Content-Length"),
             r.headers.get("Transfer-Encoding"))
    if r.status_code != 200:
        log.info("   redirect/errore -> Location=%s", r.headers.get("Location"))
        return None, dict(header=t_header)
    pezzi, n, primo, prossimo_mb = [], 0, None, 1
    for chunk in r.iter_content(64 * 1024):
        if primo is None:
            primo = time.time() - t0
            log.info("   primo byte dopo %.1fs", primo)
        pezzi.append(chunk)
        n += len(chunk)
        trascorso = time.time() - t0
        if n >= prossimo_mb * 1e6:
            log.info("   %.0f MB dopo %.1fs (%.0f KB/s)", n / 1e6, trascorso,
                     n / 1e3 / max(trascorso - (primo or 0), 1e-3))
            prossimo_mb += 1
        if trascorso > tetto:
            log.info("   !!! TETTO di %ds superato a %.1f MB: abbandono", tetto, n / 1e6)
            r.close()
            return None, dict(header=t_header, primo=primo, byte=n, totale=trascorso)
    tot = time.time() - t0
    testo = b"".join(pezzi).decode("utf-8", errors="replace")
    return testo, dict(header=t_header, primo=primo, byte=n, totale=tot)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-n", type=int, default=5, help="prime N tavole in coda")
    ap.add_argument("--tavole", type=int, nargs="*", help="id tavole specifiche")
    ap.add_argument("--tetto", type=int, default=120,
                    help="secondi massimi per un singolo SVG")
    a = ap.parse_args()
    _configura_log()

    db = Db()
    if a.tavole:
        segni = ",".join("?" * len(a.tavole))
        tavole = db.query(f"""SELECT t.*, r.cartella FROM tavole t JOIN release r
                              ON r.id=t.release WHERE t.id IN ({segni})""", a.tavole)
    else:
        tavole = db.tavole_da_scaricare(a.n)

    log.info("=== diagnosi Maschio: %d tavole, tetto %ds ===", len(tavole), a.tetto)
    t0 = time.time()
    client = MaschioClient(delay=0)
    log.info("login: %.1fs", time.time() - t0)

    for t in tavole:
        log.info("tavola %s (release %s, %s %s) dir=%s", t["id"], t["release"],
                 t["numero"], t["titolo"], t["cartella"])
        try:
            svg, d = svg_streaming(client, t["cartella"], t["id"], a.tetto)
        except Exception as e:  # noqa: BLE001 - diagnosi: si registra tutto
            log.info("   ERRORE %s: %s (dopo %.1fs)", type(e).__name__, e,
                     time.time() - t0)
            continue
        if svg is None:
            continue
        pos = parser.posizioni_svg(svg)
        log.info("   SVG %.2f MB in %.1fs (primo byte %.1fs), %d posizioni",
                 d["byte"] / 1e6, d["totale"], d["primo"] or 0, len(pos))
        if pos:
            p = pos[0]
            t1 = time.time()
            try:
                codice = client.codice_posizione(p[0], p[1], p[2])
                log.info("   getPartNumber riga %s -> %s in %.2fs", p[2], codice,
                         time.time() - t1)
            except Exception as e:  # noqa: BLE001
                log.info("   getPartNumber ERRORE %s: %s dopo %.1fs",
                         type(e).__name__, e, time.time() - t1)
    log.info("=== fine, %.1f min ===", (time.time() - t0) / 60)


if __name__ == "__main__":
    main()
