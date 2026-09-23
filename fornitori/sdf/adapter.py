"""Adatta il portale SDF (eparts CRIC) all'interfaccia comune dei fornitori.

Differenza rispetto alla versione precedente: SDF non espone un endpoint che
restituisca il catalogo completo di una macchina. I ricambi si ottengono
tavola per tavola, quindi la strategia e':

  1. trova_macchina  -> solo lookup, nessun download (economico)
  2. scegli          -> fissa la macchina, ancora nessun download
  3. cerca_ricambio  -> se il modello non e' in cache lo scarica una volta
                        (~300 chiamate, 2-3 min), poi interroga il DB locale

La cache su SQLite e' permanente e condivisa tra modelli: le tavole sono
largamente riusate tra trattori diversi, quindi il secondo modello della
stessa famiglia scarica molto meno del primo.
"""

import logging
import re
import threading

import config
from ..base import Fornitore
from .client import BRANDS, SdfClient, SessionExpired, SdfNonRaggiungibile
from .api import SdfApi
from .db import Db
from .crawler import Crawler
from .risoluzione import risolvi_variante, telaio_completo
from ..testo import parole_chiave, piu_simili

log = logging.getLogger("sdf")

# quante varianti proporre al cliente. ARGON 65 ne ha 7: con un tetto piu'
# basso il modello giusto sparisce dall'elenco senza che nessuno se ne accorga
MAX_CANDIDATE = 12

# crawl in corso, condivisi tra tutte le conversazioni: chiave "BRAND:model_id"
_IN_CORSO = {}
_PROGRESSO = {}
_IN_CORSO_LK = threading.Lock()

# elenco completo modelli per marca, per il fallback fuzzy quando la
# ricerca sul portale (per refuso di battitura) non trova nulla. E'
# anagrafica che cambia raramente: si scarica una volta per marca e si
# condivide fra tutte le conversazioni, come il crawl dei cataloghi.
_MODELLI_CACHE = {}
_MODELLI_LK = threading.Lock()

# ordine di ricerca quando la marca non e' nota
MARCHE = ["SAME", "DEUTZ-FAHR", "HURLIMANN", "LAMBORGHINI"]

# La ricerca 'cric/models/search' di SDF e' sensibile alla spaziatura tra
# numero e sigla: "5105 DF" trova il modello, "5105DF"/"5105df" (attaccati,
# come spesso li scrive il cliente e come il modello li ripete tal quali)
# non trovano NULLA - non e' un problema di maiuscole/minuscole (verificato:
# "5105 df" funziona) ne' di refuso, quindi non serve il fallback fuzzy
# (lento: scarica l'intero elenco modelli via API). Si separa cifre e
# lettere attaccate e si riprova, prima di arrendersi al fuzzy.
_SEPARA_NUM_LETT = re.compile(r"(?<=[0-9])(?=[A-Za-z])|(?<=[A-Za-z])(?=[0-9])")


def _normalizza_spaziatura(testo):
    return _SEPARA_NUM_LETT.sub(" ", testo or "")


def _id_macchina(brand, family_id, model_id):
    return f"{brand}:{family_id}:{model_id}"


def _parse_id(id_macchina):
    brand, fam, mod = id_macchina.split(":")
    return brand, int(fam), int(mod)


def _vin_da_nome(nome):
    """'ARGON 65 -> NNZJY002W0BS00001' -> 'NNZJY002W0BS00001'"""
    return nome.split("->")[-1].strip() if "->" in (nome or "") else None


class FornitoreSdf(Fornitore):
    marca = "sdf"
    etichetta = "SAME / Deutz-Fahr / Hurlimann / Lamborghini (SDF)"
    alias = ("same", "deutz", "deutz-fahr", "deutzfahr",
              "lamborghini", "lambo", "hurlimann", "hürlimann")

    def __init__(self, db_path=None):
        self.client = SdfClient(
            cookie=config.leggi_credenziale("cookie.txt", "SDF_COOKIE"),
            username=config.leggi_credenziale("sdf_username.txt", "SDF_USERNAME"),
            password=config.leggi_credenziale("sdf_password.txt", "SDF_PASSWORD"))
        self.api = SdfApi(self.client)
        self.db = Db(db_path) if db_path else Db()
        self.crawler = Crawler(self.api, self.db, verbose=False)
        self._lock = threading.Lock()

        self.brand = None
        self.family_id = None
        self.model_id = None
        self.nome_macchina = None
        self.machine = None      # configurazione da matricola (filtro 'vals')

    # ------------------------------------------------------------- stato
    def disponibile(self):
        try:
            return bool(self.api.families(brand="SAME"))
        except Exception:
            log.exception("SDF: controllo disponibilita' fallito")
            return False

    def _reset(self):
        self.brand = self.family_id = self.model_id = None
        self.nome_macchina = self.machine = None

    def _non_raggiungibile(self, e):
        log.error("SDF non raggiungibile: %s", e)
        return {"marca": self.marca, "trovato": False,
                "errore": "sdf_non_raggiungibile",
                "messaggio": "Il catalogo SDF non e' raggiungibile in questo "
                            "momento. Dillo al cliente e invitalo a riprovare "
                            "piu' tardi: NON dire che il modello non esiste."}

    # ------------------------------------------------- ricerca macchina
    def trova_macchina(self, marca=None, modello=None, matricola=None,
                       marca_certa=False, **kwargs):
        self._reset()

        # telaio completo: identificazione esatta, niente euristiche
        if matricola and telaio_completo(matricola):
            esito = self._per_vin(str(matricola).strip(), marca)
            if esito:
                return esito

        if not modello:
            return {"marca": self.marca, "trovato": False,
                    "messaggio": "Per SDF serve il nome del modello "
                                 "(es. 'Argon 65') oppure il numero di telaio completo."}

        marche = [marca.upper()] if marca else MARCHE
        candidate = []
        for label in marche:
            if label not in BRANDS:
                continue
            try:
                candidate += self._cerca_modello(label, modello)
            except SessionExpired:
                return {"marca": self.marca, "trovato": False,
                        "messaggio": "Sessione SDF scaduta: serve un nuovo login."}
            except SdfNonRaggiungibile as e:
                return self._non_raggiungibile(e)
            except Exception:
                log.exception("SDF: ricerca modello su %s fallita", label)
                continue

        if not candidate:
            variante = _normalizza_spaziatura(modello)
            if variante != modello:
                for label in marche:
                    if label not in BRANDS:
                        continue
                    try:
                        candidate += self._cerca_modello(label, variante)
                    except SessionExpired:
                        return {"marca": self.marca, "trovato": False,
                                "messaggio": "Sessione SDF scaduta: serve un nuovo login."}
                    except SdfNonRaggiungibile as e:
                        return self._non_raggiungibile(e)
                    except Exception:
                        log.exception("SDF: ricerca modello su %s fallita", label)
                        continue

        if not candidate:
            try:
                simili = self._cerca_fuzzy(marche, modello)
            except SdfNonRaggiungibile as e:
                return self._non_raggiungibile(e)
            if not simili:
                return {"marca": self.marca, "trovato": False,
                        "messaggio": f"Nessun modello SDF per '{modello}'."}
            return {"marca": self.marca, "trovato": True,
                    "richiede_conferma": True,
                    "interpretazione": (
                        f"Nessun modello SDF corrisponde esattamente a "
                        f"'{modello}': le candidate proposte sono le piu' "
                        f"simili per nome (possibile refuso di battitura). "
                        f"Dillo al cliente e chiedi conferma prima di "
                        f"proseguire."),
                    "candidate": [{"id": _id_macchina(v["brand"], v["family_id"],
                                                      v["model_id"]),
                                   "nome": f"{v['brand']} {v['nome']}",
                                   "matricola_da": _vin_da_nome(v["nome"]) or ""}
                                  for v in simili],
                    "totale_candidate": len(simili)}

        # progressivo: la scelta dell'intervallo la fa il codice, non l'LLM
        ambiguita = None
        if matricola:
            esito = risolvi_variante(matricola, candidate)
            log.info("SDF: matricola %s -> %s", matricola, esito["esito"])
            if esito["esito"] == "trovato":
                candidate = [esito["variante"]]
            elif esito["esito"] == "ambiguo":
                indici = {o["indice"] for o in esito["opzioni"]}
                candidate = [c for i, c in enumerate(candidate) if i in indici]
                ambiguita = esito["motivo"]
            else:
                ambiguita = esito.get("motivo")

        if len(candidate) == 1 and (marca_certa or marca):
            v = candidate[0]
            self._apri(v["brand"], v["family_id"], v["model_id"], v["nome"])
            return {"marca": self.marca, "trovato": True,
                    "selezionata": self.nome_macchina,
                    "richiede_conferma": False}

        risposta_multipla = {"marca": self.marca, "trovato": True,
                             "richiede_conferma": True}
        if ambiguita:
            risposta_multipla["nota_matricola"] = ambiguita
        return {**risposta_multipla,
                "candidate": [{"id": _id_macchina(v["brand"], v["family_id"],
                                                  v["model_id"]),
                               "nome": f"{v['brand']} {v['nome']}",
                               "matricola_da": _vin_da_nome(v["nome"]) or ""}
                              for v in candidate[:MAX_CANDIDATE]],
                "totale_candidate": len(candidate)}

    def _cerca_modello(self, brand_label, testo):
        """models/search restituisce famiglie con dentro i cataloghi (modelli)."""
        code = BRANDS[brand_label]
        raw = self.client.post("cric/models/search", brand=code,
                               searchString=testo) or []
        out = []
        for fam in raw:
            fam_id = fam.get("rowId")
            for cat in fam.get("catalogList") or []:
                nome = cat.get("description")
                out.append({
                    "brand": brand_label,
                    "family_id": fam_id,
                    "family_name": fam.get("description"),
                    "model_id": cat.get("rowId"),
                    "nome": nome,
                    "code": cat.get("code"),
                    # telaio di partenza: serve a risolvi_variante
                    "matricola": _vin_da_nome(nome) or "",
                })
        return out

    def _tutti_i_modelli(self, brand_label):
        """Tutti i modelli di una marca, con cache in memoria (vedi
        _MODELLI_CACHE sopra): serve solo per il fallback fuzzy, non per la
        ricerca normale, quindi non e' un problema se resta un po' stantia
        finche' il processo non riparte."""
        with _MODELLI_LK:
            if brand_label in _MODELLI_CACHE:
                return _MODELLI_CACHE[brand_label]
        code = BRANDS[brand_label]
        out = []
        try:
            for fam in self.api.families(brand=code):
                for m in self.api.models(fam["row_id"], brand=code):
                    out.append({
                        "brand": brand_label, "family_id": fam["row_id"],
                        "model_id": m["row_id"], "nome": m["name"],
                        # il confronto fuzzy va fatto senza il VIN iniziale
                        # ("ARGON 65 -> NNZJY...") o diluirebbe la somiglianza
                        "nome_confronto": m["name"].split("->")[0].strip(),
                    })
        except SdfNonRaggiungibile:
            raise
        except Exception:
            log.exception("SDF: elenco modelli %s non recuperato", brand_label)
            return []
        with _MODELLI_LK:
            _MODELLI_CACHE[brand_label] = out
        return out

    def _cerca_fuzzy(self, marche, modello):
        """Fallback quando la ricerca esatta sul portale non trova nulla:
        prova a correggere un refuso di battitura confrontando 'modello'
        con l'elenco completo dei modelli della marca."""
        pool = []
        for label in marche:
            if label not in BRANDS:
                continue
            pool += self._tutti_i_modelli(label)
        if not pool:
            return []
        return piu_simili(modello, pool, soglia=0.6, massimo=MAX_CANDIDATE,
                          chiave=lambda v: v["nome_confronto"])

    def _per_vin(self, vin, marca=None):
        marche = [marca.upper()] if marca and marca.upper() in BRANDS else MARCHE
        for label in marche:
            try:
                res = self.api.by_vin(vin, brand=BRANDS[label])
            except Exception:
                continue
            if not res:
                continue
            r = res[0]
            self._apri(label, r["familyRowId"], r["modelRowId"],
                       r["modelDescription"])
            # la matricola sblocca il filtro di validita' sulle tavole
            try:
                self.machine = self.api.by_serial(
                    r["familyRowId"], r["modelRowId"], r["serialNumber"],
                    brand=BRANDS[label])
            except Exception:
                self.machine = None
            return {"marca": self.marca, "trovato": True,
                    "selezionata": f"{label} {r['modelDescription']}",
                    "richiede_conferma": False,
                    # identificativo interno SDF, NON la matricola del cliente
                    # (quella resta in args["matricola"], invariata) - nomi
                    # diversi apposta, per non farli confondere a un modello
                    # che copia campi dal risultato del tool precedente
                    "numero_serie_interno": r.get("serialNumber"),
                    "motore": r.get("engineNumber")}
        return None

    def scegli(self, id_macchina):
        try:
            brand, fam, mod = _parse_id(id_macchina)
        except Exception:
            return {"errore": f"id macchina non valido: {id_macchina}"}
        try:
            nome = self._nome_modello(brand, fam, mod)
            self._apri(brand, fam, mod, nome)
            return {"marca": self.marca, "selezionata": self.nome_macchina}
        except SessionExpired:
            return {"errore": "Sessione SDF scaduta: serve un nuovo login."}
        except SdfNonRaggiungibile as e:
            return self._non_raggiungibile(e)
        except Exception as e:
            log.exception("SDF: scelta macchina %s fallita", id_macchina)
            return {"errore": str(e)}

    def _nome_modello(self, brand, fam, mod):
        row = self.db.one("SELECT name FROM model WHERE brand=? AND row_id=?",
                          (BRANDS[brand], mod))
        if row:
            return row["name"]
        for m in self.api.models(fam, brand=BRANDS[brand]):
            if m["row_id"] == mod:
                return m["name"]
        return f"modello {mod}"

    def _apri(self, brand_label, family_id, model_id, nome):
        self.brand = brand_label
        self.family_id = int(family_id)
        self.model_id = int(model_id)
        self.nome_macchina = f"{brand_label} {nome}"
        self.machine = None
        # scalda la cache subito: quando il cliente chiedera' il pezzo,
        # spesso il catalogo sara' gia' pronto
        try:
            self._assicura_catalogo()
        except Exception:
            log.exception("SDF: avvio download fallito")

    # ------------------------------------------------- ricerca ricambio
    def cerca_ricambio(self, testo, modello=None, matricola=None, **kwargs):
        if not self.model_id:
            return {"errore": "Prima individua la macchina con trova_macchina."}

        stato = self._assicura_catalogo()
        if stato != "pronto":
            av = self.stato_preparazione()
            pct = av.get("percentuale", 0)
            return {"marca": self.marca, "macchina": self.nome_macchina,
                    "blocchi": [], "in_preparazione": True,
                    "percentuale": pct,
                    "messaggio": (f"Sto preparando il catalogo di "
                                  f"{self.nome_macchina}: {pct}% completato. "
                                  f"Richiedimi il pezzo tra un minuto.")}

        righe = self._query(testo)
        if not righe:
            return {"marca": self.marca, "macchina": self.nome_macchina,
                    "blocchi": [],
                    "messaggio": f"Nessun ricambio SDF per '{testo}' "
                                 f"su {self.nome_macchina}."}

        per_tavola = {}
        for r in righe:
            per_tavola.setdefault(r["revision_id"], []).append(r)

        # Piu' revisioni della stessa tavola con gli stessi pezzi trovati
        # (cambia solo il range di telaio) valgono un blocco solo: si
        # tiene la prima e se ne uniscono i range di telaio nella nota.
        unite = {}
        for rev, elenco in per_tavola.items():
            p = elenco[0]
            chiave = (p["group_name"], p["subgroup_name"], p["drawing_name"], p["notes"],
                      tuple(sorted((r["code"], r["position"], r["quantity"]) for r in elenco)))
            if chiave in unite:
                unite[chiave]["range"].append(p["tractor_sn_range"])
            else:
                unite[chiave] = {"rev": rev, "elenco": elenco,
                                 "range": [p["tractor_sn_range"]]}

        # A pari rilevanza/pertinenza (frequente: sono punteggi grezzi),
        # l'ordine tra tavole NON puo' dipendere dall'id interno SDF della
        # tavola (revision_id): non ha alcun legame con quanto la tavola
        # sia pertinente, ed e' quello che faceva restare fuori dal tetto
        # MAX_BLOCCHI tavole importanti per puro caso (es. FRENI ANTERIORI
        # scartata a favore di una valvola dell'impianto frenatura, a
        # parita' di punteggio). Si aggiunge un terzo criterio, quante
        # righe di QUESTA tavola la ricerca ha agganciato: una tavola dove
        # 2 pezzi diversi rispondono alla ricerca e' piu' centrale di una
        # dove ne risponde 1 solo. Ultimo pareggio: nome tavola, cosi'
        # l'ordine e' sempre riproducibile e spiegabile, mai arbitrario.
        def punteggio_tavola(u):
            el = u["elenco"]
            return (max(r["rilevanza"] for r in el),
                    max(r["pertinenza_tavola"] for r in el),
                    len(el),
                    el[0]["drawing_name"] or "")

        ordinate = sorted(unite.values(), key=punteggio_tavola, reverse=True)

        blocchi = []
        for u in ordinate[:self.MAX_BLOCCHI]:
            rev, elenco = u["rev"], u["elenco"]
            p = elenco[0]
            contesto = " / ".join(x for x in (p["group_name"],
                                              p["subgroup_name"],
                                              p["drawing_name"]) if x)
            range_telaio = ", ".join(x for x in u["range"] if x)
            nota = " - ".join(x for x in (p["notes"], range_telaio) if x)

            # Il testo cercato puo' agganciare solo alcune righe della
            # tavola: le altre righe vicine per posizione sono spesso
            # altri componenti dello stesso gruppo meccanico (es. le
            # altre tenute di uno stesso gruppo mozzo-disco-pistone), utili
            # se il cliente sta revisionando l'intero gruppo. Si e' visto
            # in sessione che lasciare all'LLM il compito di NOTARE da
            # solo questi pezzi dentro una lista JSON e' inaffidabile
            # (stesso identico input, a volte li usa a volte no) - quindi
            # li segnaliamo gia' pronti in una frase, e lasciamo al
            # modello solo la decisione se e come usarla nella risposta.
            trovati = elenco[:10]
            codici_trovati = {r["code"] for r in trovati}
            extra = self._pezzi_vicini(rev, trovati, codici_trovati)

            nota_correlati = [
                {"codice": a["code"],
                 "descrizione": a["description"] or "",
                 "posizione": a["position"] or ""}
                for a in extra]

            blocchi.append({
                "contesto": contesto,
                "nota": nota or "",
                "articoli": [self._articolo(a) for a in trovati],
                "nota_correlati": nota_correlati,
            })

        return {"marca": self.marca, "macchina": self.nome_macchina,
                "ambiguo": False, "blocchi": blocchi,
                "totale_codici": len({r["code"] for r in righe})}

    # A differenza del numero di righe extra per blocco (dove tagliare
    # aiuta, si e' visto che riduce solo rumore), tagliare il NUMERO di
    # blocchi e' rischioso: il punteggio di _query mette spesso a pari
    # merito una tavola pertinente e una che ha agganciato la ricerca solo
    # di striscio (stesso motivo descritto li' - vedi commento su
    # 'rilevanza'), quindi l'ordine tra i blocchi non è affidabile quanto
    # sembra. Si e' visto in sessione un caso reale con la tavola giusta
    # ottava su nove: un tetto a 8 l'avrebbe esclusa. Restava gia' a 8
    # nella versione precedente di questo codice - lo teniamo alto.
    MAX_BLOCCHI = 8
    MAX_EXTRA_PER_BLOCCO = 15

    @staticmethod
    def _numero_posizione(position):
        m = re.match(r"\d+", position or "")
        return int(m.group()) if m else None

    def _pezzi_vicini(self, revision_id, trovati, codici_trovati):
        """Righe della stessa tavola non agganciate dalla ricerca
        testuale, ordinate per vicinanza di posizione ai pezzi trovati
        (numero di posizione principale, es. '11.2' -> 11) e limitate a
        MAX_EXTRA_PER_BLOCCO: una via di mezzo tra 'solo la stessa
        posizione esatta' (troppo stretto) e 'tutta la tavola' (troppo
        rumore su tavole grandi)."""
        posizioni_trovate = {n for n in
                              (self._numero_posizione(r["position"]) for r in trovati)
                              if n is not None}
        tavola = self._pezzi_tavola(revision_id)
        extra = [r for r in tavola if r["code"] not in codici_trovati]
        if posizioni_trovate:
            def distanza(r):
                n = self._numero_posizione(r["position"])
                if n is None:
                    return 10 ** 9
                return min(abs(n - pt) for pt in posizioni_trovate)
            extra.sort(key=distanza)
        return extra[:self.MAX_EXTRA_PER_BLOCCO]

    def _pezzi_tavola(self, revision_id):
        """Tutte le righe della tavola, per calcolare i vicini in
        _pezzi_vicini - non usata direttamente nella risposta."""
        return self.db.query(
            """SELECT code, description, position, quantity, price,
                      sellable, replaced, abolished
               FROM part WHERE revision_id = ? ORDER BY position""",
            (revision_id,))

    def _articolo(self, r):
        art = {
            "posizione": r["position"] or "",
            "codice": r["code"],
            "descrizione": r["description"] or "",
            "prezzo": r["price"] if r["price"] else None,
            "quantita": r["quantity"],
            "applicabile_a": [],
        }
        if r["abolished"]:
            art["stato"] = "abolito"
        elif not r["sellable"]:
            art["stato"] = "non vendibile"
        if r["replaced"]:
            art["sostituito_da"] = [
                {"codice": s["new_code"], "descrizione": s["description"]}
                for s in self.db.query(
                    "SELECT new_code, description FROM substitution WHERE old_code=?",
                    (r["code"],))]
        return art

    # ------------------------------------------------------------ cache
    def _assicura_catalogo(self, attendi=False):
        """Garantisce che il modello sia in cache.

        Il download richiede alcuni minuti: farlo dentro la richiesta HTTP
        significherebbe tenere il cliente appeso. Quindi parte in un thread
        separato e la ricerca risponde 'in_preparazione'. Il crawl e'
        riprendibile, quindi un'interruzione non fa perdere lavoro.

        Ritorna: "pronto" | "in_corso" | "avviato"
        """
        code = BRANDS[self.brand]
        key = f"{code}:{self.model_id}"
        if self.db.is_done(f"mod:{key}"):
            return "pronto"

        with _IN_CORSO_LK:
            t = _IN_CORSO.get(key)
            if t and t.is_alive():
                return "in_corso"
            t = threading.Thread(target=self._scarica,
                                 args=(code, self.family_id, self.model_id, key),
                                 name=f"crawl-{key}", daemon=True)
            _IN_CORSO[key] = t
            t.start()

        if attendi:
            t.join()
            return "pronto" if self.db.is_done(f"mod:{key}") else "in_corso"
        return "avviato"

    def _scarica(self, code, family_id, model_id, key):
        log.info("SDF: inizio download catalogo %s modello %s", code, model_id)
        ultimo = [0]

        def progress(gruppi_fatti=0, gruppi_totali=0, gruppo="",
                     tavole_fatte=0, tavole_gruppo=0, etichetta=""):
            frazione = 0.0
            if gruppi_totali:
                frazione = gruppi_fatti / gruppi_totali
                if tavole_gruppo:
                    frazione += (tavole_fatte / tavole_gruppo) / gruppi_totali
            pct = int(min(frazione, 1.0) * 100)
            _PROGRESSO[key] = {
                "percentuale": pct, "gruppo": gruppo, "tavola": etichetta,
                "gruppi_fatti": gruppi_fatti, "gruppi_totali": gruppi_totali,
            }
            if pct >= ultimo[0] + 10:
                ultimo[0] = pct
                log.info("SDF: %s %s%% (%s)", key, pct, gruppo)

        try:
            n = self.crawler.crawl_model(code, family_id, model_id,
                                         progress=progress)
            _PROGRESSO[key] = {"percentuale": 100, "gruppo": "", "tavola": "",
                               "righe": n}
            log.info("SDF: catalogo %s PRONTO, %s righe ricambio", key, n)
        except SessionExpired:
            log.error("SDF: sessione scaduta durante il download di %s", key)
        except Exception:
            log.exception("SDF: download di %s fallito", key)
        finally:
            with _IN_CORSO_LK:
                _IN_CORSO.pop(key, None)

    def catalogo_pronto(self):
        if not self.model_id:
            return False
        return self.db.is_done(f"mod:{BRANDS[self.brand]}:{self.model_id}")

    def stato_preparazione(self):
        """Avanzamento del download per la macchina corrente.

        stato: "nessuna_macchina" | "pronto" | "in_corso" | "da_avviare"
        """
        if not self.model_id:
            return {"stato": "nessuna_macchina", "percentuale": 0}
        key = f"{BRANDS[self.brand]}:{self.model_id}"
        if self.db.is_done(f"mod:{key}"):
            return {"stato": "pronto", "percentuale": 100,
                    "macchina": self.nome_macchina}
        with _IN_CORSO_LK:
            attivo = key in _IN_CORSO and _IN_CORSO[key].is_alive()
        info = dict(_PROGRESSO.get(key) or {})
        pct = info.get("percentuale", 0)
        info.update({
            "stato": "in_corso" if attivo else "da_avviare",
            "macchina": self.nome_macchina,
            "percentuale": pct,
            # testo pronto da mostrare: lo compone il fornitore, che sa
            # cosa sta facendo, non l'interfaccia
            "etichetta": f"Scarico il catalogo di {self.nome_macchina}",
        })
        return info

    # campi su cui cercare. Il nome della tavola e del gruppo NON sono
    # decorazione: portano l'informazione che alla descrizione del pezzo
    # manca. Nella tavola "FRENI POSTERIORI" i ricambi si chiamano solo
    # "disco freno" o "guarnizione": cercando le sole descrizioni, la
    # richiesta "freni posteriori" non troverebbe nulla.
    CAMPI_RICERCA = ("p.description", "p.code", "d.name", "g.name", "s.name")

    # Nome di tavola, gruppo e sottogruppo: a parita' di rilevanza sul
    # pezzo, viene prima la tavola che nomina la parola cercata in piu' di
    # questi campi (es. "FRENI POSTERIORI" / "SCATOLE FRENO POSTERIORE"
    # prima di una valvola dell'impianto frenatura).
    CAMPI_CONTESTO = ("d.name", "g.name", "s.name")

    def _query(self, testo, limit=60):
        """Cerca nelle tavole del modello corrente.

        Ogni parola della richiesta deve comparire da qualche parte nel
        contesto (pezzo, tavola, gruppo, sottogruppo). L'ordinamento premia
        i pezzi che corrispondono di per se': prima quelli con le parole
        nella propria descrizione o nel codice, poi la bulloneria che si
        trova solo in una tavola pertinente.
        """
        parole = parole_chiave(testo)
        if not parole:
            return []

        blocco = "(" + " OR ".join(f"{c} LIKE ?" for c in self.CAMPI_RICERCA) + ")"
        cond = " AND ".join(blocco for _ in parole)
        punteggio = " + ".join(
            "(CASE WHEN p.description LIKE ? OR p.code LIKE ? THEN 1 ELSE 0 END)"
            for _ in parole)
        pertinenza = " + ".join(
            f"(CASE WHEN {c} LIKE ? THEN 1 ELSE 0 END)"
            for _ in parole for c in self.CAMPI_CONTESTO)

        # ATTENZIONE ALL'ORDINE: SQLite lega i '?' nell'ordine in cui
        # compaiono nella query, e il SELECT precede il WHERE. Gli argomenti
        # del punteggio e della pertinenza vanno quindi PRIMA di quelli
        # della condizione, nello stesso ordine delle colonne del SELECT.
        args = []
        for w in parole:
            args += [f"%{w}%", f"%{w}%"]
        for w in parole:
            args += [f"%{w}%"] * len(self.CAMPI_CONTESTO)
        for w in parole:
            args += [f"%{w}%"] * len(self.CAMPI_RICERCA)
        args += [BRANDS[self.brand], self.model_id, limit]

        return self.db.query(f"""
            SELECT p.code, p.description, p.position, p.quantity, p.price,
                   p.sellable, p.replaced, p.abolished,
                   d.revision_id, d.name AS drawing_name, d.notes,
                   d.tractor_sn_range, d.preview_url,
                   g.name AS group_name, s.name AS subgroup_name,
                   ({punteggio}) AS rilevanza,
                   ({pertinenza}) AS pertinenza_tavola
            FROM part p
            JOIN drawing d        ON d.revision_id = p.revision_id
            JOIN model_drawing md ON md.revision_id = p.revision_id
            LEFT JOIN grp g       ON g.row_id = md.group_id
            LEFT JOIN subgroup s  ON s.row_id = md.subgroup_id
            WHERE {cond} AND md.brand = ? AND md.model_id = ?
            ORDER BY rilevanza DESC, pertinenza_tavola DESC, d.revision_id, p.position
            LIMIT ?
        """, args)

    # ------------------------------------------------- extra (fuori base)
    def dove_si_usa(self, codice):
        """Su quali famiglie/modelli monta un codice. Interroga SDF, non il DB."""
        try:
            apps = self.api.applicabilities(
                codice, brand=BRANDS[self.brand] if self.brand else None)
        except Exception as e:
            return {"errore": str(e)}
        return {"codice": codice,
                "descrizione": self.api.part_description(codice),
                "applicabilita": [{"marca": a["brand"], "livello": a["level"],
                                   "descrizione": a["description"]}
                                  for a in apps]}

    def sostituti(self, codice):
        cached = self.db.query(
            "SELECT new_code, description, quantity FROM substitution WHERE old_code=?",
            (codice,))
        if cached:
            return [{"codice": r["new_code"], "descrizione": r["description"],
                     "quantita": r["quantity"]} for r in cached]
        subs = self.api.substitutions(codice)
        self.db.save_substitutions(codice, subs)
        return [{"codice": s["code"], "descrizione": s["description"],
                 "quantita": s["quantity"]} for s in subs]


def crea():
    try:
        return FornitoreSdf()
    except Exception:
        return None