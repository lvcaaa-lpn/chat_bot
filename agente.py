"""
Motore di conversazione: schemi degli strumenti, prompt e ciclo di
tool calling.

Sta qui una volta sola: bot.py (terminale) e server.py (web) lo usano
entrambi. Prima la stessa logica era duplicata nei due file, e un bug
corretto in uno restava nell'altro.
"""

import json

from openai import OpenAI
import traceback
import re, unicodedata

import config
from fornitori import carica_tutti

# -------------------------------------------------------------------
# STRUMENTI — sempre quattro, qualunque sia il numero di marche
# -------------------------------------------------------------------
TOOLS = [
    {"type": "function", "function": {
        "name": "elenca_marche",
        "description": ("Elenca le marche gestite e se sono utilizzabili ora. "
                        "Usalo se il cliente chiede cosa trattiamo, o se una "
                        "ricerca fallisce e vuoi sapere cosa e' disponibile."),
        "parameters": {"type": "object", "properties": {}}}},

    {"type": "function", "function": {
        "name": "trova_macchina",
        "description": ("Individua la macchina del cliente. Indica la marca se "
                        "il cliente l'ha detta ('goldoni', 'sdf');"),
        "parameters": {"type": "object", "properties": {
            "marca": {"type": "string"},
            "modello": {"type": "string",
                        "description": ("Il nome del modello COSI' COME LO HA "
                                    "SCRITTO il cliente, per intero: 'Lampo 55 W', "
                                    "'Krypton F 100'. Non togliere la famiglia o "
                                    "la serie, non lasciare solo il numero.")},
            "matricola": {"type": "string",
                        "description": "Numero sulla targhetta, es. 'C570000'"},
            "citazione_marca": {"type": "string",
                        "description": ("Le parole ESATTE con cui il cliente ha "
                                    "nominato la marca in QUESTO messaggio, es. "
                                    "'goldoni', 'same'. Lascia vuoto se non "
                                    "l'ha nominata ora: non dedurla dal modello "
                                    "e non riprenderla dai messaggi precedenti.")},
        }}}},

    {"type": "function", "function": {
        "name": "scegli_macchina",
        "description": ("Fissa la macchina quando trova_macchina ha restituito "
                        "piu' candidate e il cliente ha scelto."),
        "parameters": {"type": "object", "properties": {
            "marca": {"type": "string"},
            "id_macchina": {"type": "string"},
        }, "required": ["marca", "id_macchina"]}}},

    {"type": "function", "function": {
        "name": "cerca_ricambio",
        "description": ("Cerca un ricambio nel catalogo della macchina gia' "
                        "individuata. Usa termini del catalogo, italiano e "
                        "singolare: 'tubo mandata', 'filtro olio', 'cofano'. "
                        "Passa sempre la matricola se il cliente l'ha fornita."),
        "parameters": {"type": "object", "properties": {
            "marca": {"type": "string"},
            "testo": {"type": "string"},
            "modello": {"type": "string"},
            "matricola": {"type": "string"},
        }, "required": ["marca", "testo"]}}},
]

SYSTEM = """Sei l'assistente ricambi di un rivenditore di macchine agricole.
Parli con clienti che spesso non conoscono i termini tecnici.

REGOLE FONDAMENTALI
- Non inventare MAI codici, prezzi o disponibilita'. Riporta solo i codici
  restituiti dagli strumenti, copiati carattere per carattere.
- Se non trovi il pezzo, dillo. Non proporre alternative inventate.

ORDINE DELLE OPERAZIONI
1. Serve la MARCA, detta dal cliente. Non dedurla mai dal nome del modello
   e non riprenderla dai messaggi precedenti: lo stesso cliente puo'
   chiederti di due macchine diverse nella stessa conversazione. Se
   nomina una macchina nuova senza marca, chiedi la marca prima di tutto.
   Se dice "un'altra macchina" o nomina un modello diverso da quello
   selezionato, considera azzerata la macchina corrente: marca, modello e
   matricola vanno richiesti da capo.
   Chiedi sempre anche la matricola: e' il numero sulla targhetta
   metallica e riduce molto i risultati.
2. Compila "citazione_marca" solo con le parole che il cliente ha scritto
   in QUESTO messaggio. Se non ha nominato la marca ora, lascialo vuoto e
   non passare "marca": il sistema cerchera' su tutte le marche.
3. Se una marca non e' disponibile, dillo con onesta'. Non inventare.

QUANDO CI SONO MOLTI RISULTATI
- E' normale: un trattore ha decine di tubi, filtri e supporti simili.
- Il campo "contesto" (gruppo, tavola, titolo) e' cio' che distingue
  davvero i pezzi. Usa quei titoli come opzioni per chiedere al cliente
  in quale zona o impianto si trova il pezzo.
- Se la risposta di trova_macchina contiene "richiede_conferma": true,
  ripeti al cliente quale macchina hai selezionato e chiedi conferma
  prima di cercare il ricambio.
- Se la risposta contiene "interpretazione" (il modello scritto dal
  cliente non esiste esattamente e le candidate proposte sono le piu'
  simili per nome, es. refuso di battitura), spiegalo al cliente in modo
  esplicito ("non trovo esattamente 'X', forse intendevi uno di questi?")
  prima di procedere: non e' un'identificazione certa.
- Se trova_macchina restituisce "in_preparazione": true, spiega al cliente
  che il catalogo di quel modello si sta preparando e servono alcuni
  minuti. Intanto chiedigli la matricola e quale ricambio gli serve, cosi'
  la ricerca sara' immediata al secondo tentativo.

SE IL CLIENTE USA UN TERMINE IMPRECISO, DIALETTALE O SBAGLIATO
- I clienti spesso non conoscono i nomi tecnici e usano parole gergali,
  regionali o approssimative (es. "bulbo" per un sensore). Usa la tua
  conoscenza generale di meccanica agricola per capire cosa intende, e
  cerca con il termine tecnico piu' plausibile invece che con la parola
  esatta del cliente, se pensi che dia risultati migliori.
- Quando interpreti le sue parole, dillo: "ho cercato 'sensore pressione
  olio', intendevi questo?". Cosi' il cliente puo' correggerti se hai
  capito male. Questo vale solo per COSA CERCHI, non per i risultati: sui
  codici resta valida la regola sopra, non inventare mai nulla.
- Se la ricerca con la tua interpretazione non trova nulla, o il termine e'
  troppo vago per azzardare un'interpretazione, non dire subito che il
  pezzo non esiste: chiedi al cliente di descriverlo (a cosa serve, dove
  si trova sulla macchina, a cosa e' collegato, che forma o colore ha) e
  usa la descrizione per tentare una nuova ricerca con termini diversi.

COME RISPONDERE
- Risposte brevi: sei in una finestra di chat stretta.
- Per ogni pezzo: codice, descrizione e contesto (gruppo e tavola).
- Ricorda che prezzo e disponibilita' vanno confermati dal magazzino.
- Invita il cliente a controllare sul sito le specifiche del pezzo scelto
  tramite il codice. Ricorda che essendo un'AI puoi commettere errori.
"""

MAX_GIRI = 6          # quante volte il modello puo' concatenare strumenti

RE_NUOVA_MACCHINA = re.compile(
    r"\b(un'altra macchina|altra macchina|un altro trattore|altro trattore|"
    r"cambiamo macchina|nuova macchina|un'altra domanda su|"
    r"passiamo a(?:d)? un'?altra?|passiamo al(?:la)? (?:macchina|trattore))\b", re.I)


def _norm(s):
    s = unicodedata.normalize("NFKD", (s or "").lower())
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def _costruisci_alias(registro):
    """Alias con cui i clienti nominano ogni marca ('same', 'lambo' -> 'sdf').

    Ogni fornitore dichiara i propri alias (attributo di classe 'alias'):
    aggiungere una marca richiede solo la sua cartella in fornitori/, senza
    toccare questo file.
    """
    alias_di, canonica_di = {}, {}
    for marca, f in registro.fornitori.items():
        tutti = sorted({_norm(a) for a in [*getattr(f, "alias", ()), marca]} - {""})
        for a in tutti:
            alias_di[a] = tutti        # ogni alias punta all'intero gruppo
            canonica_di[a] = marca     # ogni alias punta al nome del fornitore
    return alias_di, canonica_di

class Conversazione:
    """Una conversazione con il proprio stato (macchina scelta, cronologia)."""

    def __init__(self, registro=None):
        self.registro = registro or carica_tutti()
        self.alias, self.canonica = _costruisci_alias(self.registro)
        self.messaggi = [{"role": "system", "content": SYSTEM}]
        self.ultimo_utente = ""
        self.marca_confermata = None
        self.attesa_conferma = False
        self.dispatch = {
            "elenca_marche": self.registro.elenca,
            "trova_macchina": self.registro.trova_macchina,
            "scegli_macchina": self.registro.scegli_macchina,
            "cerca_ricambio": self.registro.cerca_ricambio,
        }

    def _marca_canonica(self, marca):
        """'same', 'Deutz-Fahr', 'lambo' -> 'sdf'. None se sconosciuta."""
        return self.canonica.get(_norm(marca))

    def _marca_dichiarata(self, citazione, marca, messaggio_utente):
        """
        True solo se il cliente ha davvero nominato la marca in questo turno.
        Serve perche' il modello, nei turni successivi, tende a riportare la
        marca della macchina precedente anche quando il cliente cambia
        argomento ("un'altra macchina, ho una Lampo 55 W").
        """
        testo = _norm(messaggio_utente)
        parole = set(testo.split())
        cit = _norm(citazione)
        if cit and cit in testo:
            return True
        return any(a in parole if " " not in a else a in testo
                   for a in self.alias.get(_norm(marca), []))

    def tronca(self, massimo=40):
        """
        Le conversazioni lunghe costano: tengo il prompt e la coda.

        Il taglio non puo' cadere a meta' di un turno (messaggio 'assistant'
        con tool_calls + le risposte 'tool' corrispondenti): un tool_call_id
        senza risposta, o viceversa, fa rifiutare l'intera richiesta
        dall'LLM. Percio' si scorre dal punto di taglio "grezzo" fino al
        prossimo messaggio 'user' di questa stessa conversazione, che e'
        sempre l'inizio di un turno intero.
        """
        if len(self.messaggi) <= massimo:
            return
        taglio = len(self.messaggi) - (massimo - 1)
        while taglio < len(self.messaggi) and self.messaggi[taglio]["role"] != "user":
            taglio += 1
        self.messaggi = [self.messaggi[0]] + self.messaggi[taglio:]

    def turno(self, llm, modello, testo, su_strumento=None):
        """
        Elabora un messaggio dell'utente e restituisce:
            {"risposta": str, "errore": bool, "strumenti": [...]}
        su_strumento: callback opzionale (nome, argomenti) per il debug.
        """
        self.ultimo_utente = testo
        self.messaggi.append({"role": "user", "content": testo})
        self.tronca()
        usati = []
        self.attesa_conferma = False

        for _ in range(MAX_GIRI):
            try:
                risposta = llm.chat.completions.create(
                    model=modello, messages=self.messaggi,
                    tools=TOOLS, temperature=0.2)
            except Exception as e:
                print("ERRORE LLM:", repr(e)) 
                return {"risposta": "Il servizio non risponde. Riprova tra poco.",
                        "errore": True, "dettaglio": str(e), "strumenti": usati}

            msg = risposta.choices[0].message
            self.messaggi.append(msg.model_dump(exclude_none=True))

            if not msg.tool_calls:
                return {"risposta": msg.content or "", "errore": False,
                        "strumenti": usati}

            for tc in msg.tool_calls:
                nome = tc.function.name
                usati.append(nome)
                try:
                    args = json.loads(tc.function.arguments or "{}")

                    # La marca puo' arrivare come alias ('same', 'lambo'):
                    # il registro conosce solo i nomi canonici. Vale per
                    # tutti gli strumenti, non solo per trova_macchina.
                    marca_grezza = args.get("marca")
                    canonica = self._marca_canonica(marca_grezza) if marca_grezza else None
                    if canonica:
                        args["marca"] = canonica

                    if nome == "trova_macchina":
                        cit = args.pop("citazione_marca", None)
                        testo_turno = self.ultimo_utente

                        # cambio macchina: la marca precedente non vale piu'
                        if RE_NUOVA_MACCHINA.search(testo_turno or ""):
                            self.marca_confermata = None

                        if marca_grezza:
                            if canonica is None:
                                # marca che non gestiamo: lascia cercare su tutti
                                args["marca"] = None
                            elif self._marca_dichiarata(cit, canonica, testo_turno):
                                self.marca_confermata = canonica
                                args["marca"] = canonica
                            elif canonica == self.marca_confermata:
                                args["marca"] = canonica
                            else:
                                args["marca"] = None
                        args["marca_certa"] = bool(args.get("marca"))

                    if su_strumento:
                        su_strumento(nome, args)

                    # trova_macchina puo' segnalare che l'identificazione non
                    # e' certa (piu' candidate, richiede_conferma). Il modello
                    # a volte ignora quel segnale e prova a fissare comunque
                    # la macchina nello stesso turno: qui glielo impediamo a
                    # livello di codice invece di fidarci che rispetti la
                    # regola scritta nel prompt.
                    if nome in ("scegli_macchina", "cerca_ricambio") and self.attesa_conferma:
                        out = {"bloccato": True,
                               "istruzione": (
                                   "Il cliente non ha ancora scelto tra le "
                                   "alternative restituite da trova_macchina in "
                                   "questo stesso turno: non e' un'identificazione "
                                   "certa. Non chiamare scegli_macchina ne' "
                                   "cerca_ricambio da solo. Elenca al cliente le "
                                   "alternative gia' trovate (marca, nome, "
                                   "matricola se nota) e fermati aspettando la sua "
                                   "risposta. Non richiedere dati che il cliente ha "
                                   "gia' fornito in questo messaggio (marca o "
                                   "modello inclusi): usa quelli, chiedi solo cio' "
                                   "che manca per scegliere tra le alternative.")}
                    else:
                        out = self.dispatch[nome](**args)

                    if nome == "trova_macchina":
                        self.attesa_conferma = bool(
                            out.get("richiede_conferma") or out.get("piu_marche")
                            or (out.get("candidate") and not out.get("selezionata")))
                except Exception as e:
                    traceback.print_exc()
                    out = {"errore_di_sistema": True,
                        "tipo": type(e).__name__, "dettaglio": str(e),
                        "istruzione": ("Guasto interno dello strumento, NON un catalogo "
                                        "mancante. Non inventare spiegazioni tecniche al "
                                        "cliente e non chiedergli il codice del pezzo. "
                                        "Riprova una sola volta con gli stessi parametri; "
                                        "se fallisce ancora, di' che c'e' un problema "
                                        "tecnico e che serve l'intervento di un operatore.")}
                self.messaggi.append({"role": "tool", "tool_call_id": tc.id,
                                        "content": json.dumps(out, ensure_ascii=False)})

        return {"risposta": "Non riesco a completare la ricerca. Prova a "
                            "descrivere il pezzo in modo diverso.",
                "errore": False, "strumenti": usati}


def crea_llm():
    cfg = config.LLM[config.PROVIDER]
    if not cfg["api_key"]:
        raise RuntimeError(f"Manca la chiave API per '{config.PROVIDER}'.")
    return OpenAI(base_url=cfg["base_url"], api_key=cfg["api_key"]), cfg["model"]
