"""
Registro dei fornitori.

Scopre da solo le sottocartelle di 'fornitori/' che espongono una
funzione crea(). Per aggiungere una marca basta creare la cartella:
nessun altro file da modificare.
"""

import importlib
import pkgutil
from pathlib import Path

from .base import Fornitore

__all__ = ["Fornitore", "Registro", "carica_tutti"]


def carica_tutti(verbose=False):
    """Importa fornitori/<marca>/__init__.py e ne chiama crea()."""
    registro = Registro()
    radice = Path(__file__).parent

    for info in pkgutil.iter_modules([str(radice)]):
        if not info.ispkg:
            continue
        try:
            modulo = importlib.import_module(f"{__name__}.{info.name}")
            crea = getattr(modulo, "crea", None)
            if crea is None:
                continue
            f = crea()
            if f is not None:
                registro.aggiungi(f)
                if verbose:
                    stato = "pronto" if f.disponibile() else "non disponibile"
                    print(f"  {f.etichetta}: {stato}")
        except Exception as e:
            if verbose:
                print(f"  {info.name}: non caricato ({e})")
    return registro


class Registro:
    def __init__(self):
        # --- #
        self.codici_visti = set()
        # --- #
        self.fornitori = {}

    def aggiungi(self, fornitore):
        self.fornitori[fornitore.marca] = fornitore

    def get(self, marca):
        return self.fornitori.get((marca or "").lower())

    def _non_gestita(self, marca):
        return {"trovato": False, "marca_non_gestita": True,
                "marca_richiesta": marca,
                "disponibili": sorted(self.fornitori),
                "messaggio": ("Questa marca non e' tra quelle gestite. Di' al "
                              "cliente quali marche trattiamo e chiedi conferma.")}

    # --- metodi esposti all'LLM come strumenti ---------------------
    def elenca(self):
        out = []
        for f in self.fornitori.values():
            try:
                ok = f.disponibile()
            except Exception:
                ok = False
            out.append({"marca": f.marca, "etichetta": f.etichetta,
                        "disponibile": ok})
        return {"marche": out}

    def trova_macchina(self, marca=None, modello=None, matricola=None, marca_certa=False, **extra):
        """Senza marca, interroga tutti i fornitori disponibili."""
        if marca:
            f = self.get(marca)
            if f is None:
                return self._non_gestita(marca)
            return f.trova_macchina(modello=modello, matricola=matricola,
                                    marca_certa=marca_certa)

        risultati = []
        for f in self.fornitori.values():
            try:
                if f.disponibile():
                    r = f.trova_macchina(modello=modello, matricola=matricola, marca_certa=False) # ! ! ! 
                    if r.get("trovato"):
                        risultati.append(r)
            except Exception:
                continue

        if not risultati:
            return {"trovato": False,
                    "messaggio": "Nessuna marca corrisponde. Chiedi al cliente "
                                 "la marca del trattore."}
        if len(risultati) > 1:
            return {"trovato": True, "piu_marche": True, "risultati": risultati,
                    "messaggio": "Piu' marche corrispondono: chiedi al cliente "
                                 "quale trattore possiede."}
        return risultati[0]

    def scegli_macchina(self, marca, id_macchina):
        f = self.get(marca)
        if f is None:
            return self._non_gestita(marca)
        return f.scegli(id_macchina)

    # --- stato dell'interfaccia (non e' uno strumento dell'LLM) --------
    def stato_preparazione(self):
        """Il primo fornitore che ha lavoro in corso, altrimenti 'pronto'.

        Serve all'interfaccia per mostrare al cliente una riga di stato
        mentre un catalogo si scarica. Nessuna marca e' cablata qui: ogni
        fornitore dichiara il proprio stato, e chi risponde subito eredita
        il "pronto" predefinito da Fornitore.
        """
        for f in self.fornitori.values():
            try:
                st = f.stato_preparazione()
            except Exception:
                continue
            if st.get("stato") == "in_corso":
                return {**st, "marca": f.marca, "etichetta_marca": f.etichetta}
        return {"stato": "pronto", "percentuale": 100}

    def qualcuno_sta_lavorando(self):
        return self.stato_preparazione().get("stato") == "in_corso"

    """
    def cerca_ricambio(self, marca, testo, modello=None, matricola=None, **extra):
        return self.get(marca).cerca_ricambio(testo, modello=modello,
                                              matricola=matricola)
    """
    def cerca_ricambio(self, marca, testo, modello=None, matricola=None, **extra):
        ris = self.get(marca).cerca_ricambio(testo, modello=modello,
                                            matricola=matricola)
        self.codici_visti.update(
            a["codice"]
            for b in ris.get("blocchi", [])
            for a in b["articoli"]
            if a.get("codice")
        )
        return ris