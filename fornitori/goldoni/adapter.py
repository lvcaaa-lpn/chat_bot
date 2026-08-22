"""Adatta il catalogo Goldoni all'interfaccia comune."""

from ..base import Fornitore
from . import db

import threading

# Cataloghi in corso di scaricamento, condivisi fra tutte le conversazioni:
# due clienti che chiedono lo stesso modello non devono crawlare due volte.
_IN_CORSO = {}
_LOCK = threading.Lock()


def _scarica_in_background(codice):
    """Avvia il crawl in un thread. Restituisce True se e' stato avviato ora."""
    with _LOCK:
        if codice in _IN_CORSO:
            return False
        _IN_CORSO[codice] = True

    def lavoro():
        try:
            from .client import GoldoniClient
            from . import db as _db
            con = _db.apri()               # connessione propria: SQLite non
            GoldoniClient(delay=0.4).scarica_catalogo(  # condivide fra thread
                con, codice, verbose=False)
            con.commit()
            con.close()
        except Exception as e:
            print(f"[scarico {codice}] fallito: {e}")
        finally:
            with _LOCK:
                _IN_CORSO.pop(codice, None)

    threading.Thread(target=lavoro, daemon=True).start()
    return True


class FornitoreGoldoni(Fornitore):
    marca = "goldoni"
    etichetta = "Goldoni"

    def __init__(self):
        self.con = db.apri()
        self.catalogo = None
        self.nome_macchina = None

    def disponibile(self):
        return db.statistiche(self.con)["righe"] > 0

    def trova_macchina(self, modello=None, matricola=None, marca_certa=False, **kwargs):
        tutti = db.trova_cataloghi(self.con, modello=modello, matricola=matricola)
        if not tutti:
            return {"marca": self.marca, "trovato": False,
                    "messaggio": "Nessun catalogo Goldoni corrisponde."}

        scaricati = [c for c in tutti if c["scaricato_il"]]
        mancanti = [c for c in tutti if not c["scaricato_il"]]

        # Il candidato migliore non e' in archivio: lo preparo in background
        # invece di fingere che non esista.
        if mancanti and tutti[0] is mancanti[0]:
            codice = mancanti[0]["codice"]
            avviato = _scarica_in_background(codice)
            return {"marca": self.marca, "trovato": False,
                    "in_preparazione": True,
                    "catalogo": codice,
                    "nome": mancanti[0]["serie"],
                    "messaggio": (
                        f"Il catalogo '{mancanti[0]['serie']}' corrisponde alla "
                        f"richiesta ma non e' ancora nel nostro archivio. "
                        f"{'Lo sto scaricando ora' if avviato else 'E gia in preparazione'}: "
                        f"servono alcuni minuti. Avvisa il cliente e invitalo a "
                        f"riprovare fra poco, oppure chiedigli intanto la matricola "
                        f"e quale ricambio gli serve.")}

        if not scaricati:
            return {"marca": self.marca, "trovato": False,
                    "messaggio": "Nessun catalogo corrispondente e' disponibile ora."}

        # Serie diverse possono condividere lo stesso numero di modello (es.
        # "3050" compare sia nella serie "3000" che in "Star 3000 V" che in
        # "Star 3000"): se il punteggio migliore e' in pareggio, la scelta
        # non e' certa anche quando in questo momento solo una di quelle
        # serie e' gia' in cache. Meglio chiedere conferma che indovinare
        # in silenzio con quella che si trova gia' scaricata.
        migliore = tutti[0]["punteggio"]
        pareggio = sum(1 for c in tutti if c["punteggio"] == migliore) > 1

        self.catalogo = None
        self.nome_macchina = None
        if len(scaricati) == 1 and not pareggio:
            self.catalogo = scaricati[0]["codice"]
            self.nome_macchina = scaricati[0]["serie"]

        return {"marca": self.marca, "trovato": True,
                "selezionata": self.nome_macchina,
                "richiede_conferma": bool(self.nome_macchina),
                "candidate": [{"id": c["codice"], "nome": c["serie"],
                            "modelli": c["modelli"],
                            "validita": f"{c['validita_da'] or '...'} -> "
                                        f"{c['validita_a'] or '...'}"}
                            for c in scaricati[:6]]}

    def scegli(self, id_macchina):
        r = self.con.execute("SELECT serie FROM cataloghi WHERE codice=?",
                             (id_macchina,)).fetchone()
        if not r:
            return {"errore": f"Catalogo '{id_macchina}' inesistente"}
        self.catalogo = id_macchina
        self.nome_macchina = r["serie"]
        return {"marca": self.marca, "selezionata": self.nome_macchina}

    def cerca_ricambio(self, testo, modello=None, matricola=None):
        if not self.catalogo:
            return {"errore": "Prima individua la macchina con trova_macchina."}

        grezzi = db.cerca_ricambi(self.con, testo, catalogo=self.catalogo,
                                  matricola=matricola, modello=modello,
                                  limite=60)
        if not grezzi:
            return {"marca": self.marca, "blocchi": [],
                    "messaggio": f"Nessun ricambio Goldoni per '{testo}'."}

        ris = db.raggruppa(grezzi)

        blocchi = []
        for t in ris["tavole"][:10]:
            nota = ""
            if t["validita_da"] or t["validita_a"]:
                nota = (f"valido {t['validita_da'] or '...'} -> "
                        f"{t['validita_a'] or '...'}")
            blocchi.append({
                "contesto": f"GRUPPO {t['gruppo']} TAVOLA {t['numero']} - {t['titolo']}",
                "nota": nota,
                #"immagine": t["esploso"],
                "articoli": [{"posizione": r["posizione"], "codice": r["codice"],
                              "descrizione": r["descrizione"],
                              "applicabile_a": r["applicabile_a"]}
                             for r in t["righe"][:12]],
            })

        out = {"marca": self.marca, "macchina": self.nome_macchina,
               "ambiguo": ris["ambiguo"], "blocchi": blocchi,
               "totale_codici": ris["totale_codici"]}
        if ris["ambiguo"]:
            out["varianti"] = ris["varianti"]
            out["avviso"] = (
                "Piu' tavole con lo stesso titolo sono compatibili: sono "
                "varianti (allestimento, misura, cabina) che il catalogo non "
                "distingue nel testo. Elenca le alternative, mostra i link "
                "agli esplosi e chiedi conferma al cliente. Non scegliere tu.")
        return out
