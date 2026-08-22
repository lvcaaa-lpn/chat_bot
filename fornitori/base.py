"""
Interfaccia che ogni marca deve implementare.

Il formato di risposta e' identico per tutti i fornitori, cosi' il bot
espone sempre gli stessi strumenti indipendentemente da quante marche
sono collegate:

    {
      "marca": "goldoni",
      "macchina": "Star Quadrifoglio",
      "ambiguo": false,
      "blocchi": [
        {"contesto": "GRUPPO 21 TAVOLA 640 - TUBI DISTRIBUTORI",
         "nota": "valido da C556211",
         "articoli": [{"posizione", "codice", "descrizione",
                       "prezzo", "applicabile_a"}]}
      ]
    }
"""


class Fornitore:
    marca = "?"          # identificativo breve, usato dall'LLM
    etichetta = "?"      # nome leggibile, mostrato al cliente
    alias = ()           # altri nomi con cui il cliente puo' chiamare la marca

    def disponibile(self):
        """False se il fornitore non e' utilizzabile ora (sessione scaduta,
        catalogo non ancora scaricato). Il bot lo comunica invece di fallire."""
        raise NotImplementedError

    def trova_macchina(self, modello=None, matricola=None, marca_certa=False):
        raise NotImplementedError

    def scegli(self, id_macchina):
        raise NotImplementedError

    def cerca_ricambio(self, testo, modello=None, matricola=None):
        raise NotImplementedError

    # ------------------------------------------------------------------
    def stato_preparazione(self):
        """Lavoro in corso che ritarda le risposte, se ce n'e'.

        Alcuni portali non permettono di scaricare il catalogo di una
        macchina in un colpo solo: va costruito tavola per tavola, e ci
        vogliono minuti. Invece di lasciare il cliente davanti a una chat
        ferma, il fornitore dichiara qui a che punto e', e l'interfaccia
        lo mostra come riga di stato.

        Deve restituire un dict con:
            stato       "pronto" | "in_corso" | "nessuna_macchina"
            percentuale 0-100
            etichetta   testo breve da mostrare al cliente (facoltativo)

        L'implementazione predefinita dice "pronto": un fornitore che
        risponde subito non deve scrivere nulla.
        """
        return {"stato": "pronto", "percentuale": 100}

    def lavoro_in_corso(self):
        """Scorciatoia usata dall'interfaccia."""
        try:
            return self.stato_preparazione().get("stato") == "in_corso"
        except Exception:
            return False