"""CLI interattiva per esplorare il catalogo SDF.

Uso (login manuale, cookie gia' valido):
    set SDF_COOKIE=JSESSIONID=...      (Windows cmd)
    $env:SDF_COOKIE="JSESSIONID=..."   (PowerShell)
    python -m sdf.cli

Uso (login automatico):
    $env:SDF_USERNAME="..."
    $env:SDF_PASSWORD="..."
    python -m sdf.cli
"""
import sys
import traceback
import config

from .client import BRANDS, SdfClient, SessionExpired
from .api import SdfApi
from .db import Db
from .crawler import Crawler

HELP = """
Comandi disponibili
-------------------
  brand [nome]        cambia marca (SAME, DEUTZ-FAHR, HURLIMANN, LAMBORGHINI)
  fam [testo]         elenca/filtra le famiglie della marca corrente
  mod [testo]         elenca/filtra i modelli della famiglia selezionata
  vin <vin>           risale a famiglia/modello dal numero di telaio
  serial <n>          configurazione macchina dalla matricola (richiede modello)
  engine <n>          idem dal numero motore
  grp                 gruppi del modello selezionato
  sub [n]             sottogruppi del gruppo n (o del gruppo selezionato)
  tav [n]             tavole del sottogruppo n
  parts <n>           ricambi della tavola n
  find <testo|codice> cerca un ricambio per codice o descrizione
  where <codice>      su quali modelli/tavole viene usato un codice
  subs <codice>       codici sostitutivi
  crawl model         scarica nel DB il modello selezionato
  crawl family        scarica nel DB tutta la famiglia selezionata
  estimate            stima il lavoro totale per la marca corrente
  stats               righe presenti nel DB
  state               mostra la selezione corrente
  help / quit
"""


def ask(prompt, options, labeler=str):
    """Menu numerato con filtro testuale."""
    if not options:
        print("  (nessun risultato)")
        return None
    for i, o in enumerate(options):
        print(f"  {i:>4}  {labeler(o)}")
    raw = input("numero (invio = annulla) > ").strip()
    if not raw.isdigit():
        return None
    i = int(raw)
    return options[i] if 0 <= i < len(options) else None


class Cli:
    def __init__(self, cookie, username=None, password=None, db_path="sdf.db"):
        self.client = SdfClient(cookie, username=username, password=password)
        self.api = SdfApi(self.client)
        self.db = Db(db_path)
        self.crawler = Crawler(self.api, self.db)
        self.brand_label = "SAME"
        self.family = None
        self.model = None
        self.groups = []
        self.group = None
        self.subgroup = None
        self.drawings = []
        self.machine = None      # macchina identificata da matricola/VIN

    # ------------------------------------------------------------- helpers
    @property
    def brand(self):
        return self.client.brand

    @property
    def vals(self):
        """Filtro di validita': la configurazione della macchina, se nota."""
        return self.machine["vals"] if self.machine else ""

    def need(self, *what):
        for w in what:
            if getattr(self, w) is None:
                print(f"  Seleziona prima: {w}")
                return False
        return True

    def show_state(self):
        print(f"  marca      : {self.brand_label} ({self.brand})")
        print(f"  famiglia   : {self.family['name'] if self.family else '-'}")
        print(f"  modello    : {self.model['name'] if self.model else '-'}")
        print(f"  gruppo     : {self.group['name'] if self.group else '-'}")
        print(f"  sottogruppo: {self.subgroup['name'] if self.subgroup else '-'}")
        if self.machine:
            print(f"  macchina   : matr. {self.machine['serial']} "
                  f"variante {self.machine['variant_code']} "
                  f"({len(self.machine['vals'])} filtri attivi)")

    # ------------------------------------------------------------ comandi
    def cmd_brand(self, arg):
        if not arg:
            print("  " + ", ".join(BRANDS))
            return
        match = [b for b in BRANDS if arg.upper() in b]
        if not match:
            print(f"  marca sconosciuta: {arg}")
            return
        self.brand_label = match[0]
        self.client.set_brand(match[0])
        self.family = self.model = self.group = self.subgroup = self.machine = None
        print(f"  marca -> {self.brand_label} ({self.brand})")

    def cmd_fam(self, arg):
        fams = self.api.families()
        if arg:
            fams = [f for f in fams if arg.lower() in (f["name"] or "").lower()]
        self.db.save_families(self.brand, fams)
        sel = ask("famiglia", fams, lambda f: f"{f['name']}  [{f['row_id']}]")
        if sel:
            self.family = sel
            self.model = self.group = self.subgroup = self.machine = None
            print(f"  famiglia -> {sel['name']}")

    def cmd_mod(self, arg):
        if not self.need("family"):
            return
        mods = self.api.models(self.family["row_id"])
        if arg:
            mods = [m for m in mods if arg.lower() in (m["name"] or "").lower()]
        self.db.save_models(self.brand, mods)
        sel = ask("modello", mods, lambda m: f"{m['name']}  [{m['row_id']}]")
        if sel:
            self.model = sel
            self.group = self.subgroup = self.machine = None
            print(f"  modello -> {sel['name']}")

    def cmd_vin(self, arg):
        if not arg:
            print("  uso: vin <numero telaio completo>")
            return
        res = self.api.by_vin(arg)
        if not res:
            print("  VIN non trovato (serve il numero completo, non parziale)")
            return
        for r in res:
            print(f"  {r['familyDescription']} / {r['modelDescription']}")
            print(f"    familyRowId={r['familyRowId']} modelRowId={r['modelRowId']}")
            print(f"    matricola={r['serialNumber']} motore={r['engineNumber']}")
            print(f"    pattern={r['vinpattern']}")
        r = res[0]
        self.family = {"row_id": r["familyRowId"], "name": r["familyDescription"]}
        self.model = {"row_id": r["modelRowId"], "name": r["modelDescription"]}
        self.group = self.subgroup = None
        # carica anche la configurazione della macchina
        self.machine = self.api.by_serial(r["familyRowId"], r["modelRowId"],
                                          r["serialNumber"])
        if self.machine:
            print(f"  configurazione caricata: {len(self.machine['vals'])} filtri")

    def cmd_serial(self, arg):
        if not self.need("family", "model"):
            return
        m = self.api.by_serial(self.family["row_id"], self.model["row_id"], arg)
        self._show_machine(m)

    def cmd_engine(self, arg):
        if not self.need("family", "model"):
            return
        m = self.api.by_engine(self.family["row_id"], self.model["row_id"], arg)
        self._show_machine(m)

    def _show_machine(self, m):
        if not m:
            print("  non trovata")
            return
        self.machine = m
        print(f"  matricola {m['serial']}  motore {m['engine']}")
        print(f"  variante {m['variant_code']}  modello {m['model_code']}")
        print(f"  configurazione ({len(m['vals'])} voci):")
        for v in m["vals"][:15]:
            print(f"    {v}")
        if len(m["vals"]) > 15:
            print(f"    ... e altre {len(m['vals']) - 15}")
        print("  -> le tavole saranno ora filtrate su questa configurazione")

    def cmd_grp(self, arg):
        if not self.need("family", "model"):
            return
        self.groups = self.api.groups(self.family["row_id"], self.model["row_id"])
        self.db.save_groups(self.groups)
        sel = ask("gruppo", self.groups,
                  lambda g: f"{g['name']:<45} ({len(g['subgroups'])} sottogruppi)")
        if sel:
            self.group = sel
            self.subgroup = None
            print(f"  gruppo -> {sel['name']}")

    def cmd_sub(self, arg):
        if not self.need("group"):
            return
        sel = ask("sottogruppo", self.group["subgroups"],
                  lambda s: f"{s['code']} - {s['name']}")
        if sel:
            self.subgroup = sel
            print(f"  sottogruppo -> {sel['name']}")

    def cmd_tav(self, arg):
        if not self.need("family", "model", "group"):
            return
        idx = self.api.drawing_index(self.family["row_id"], self.model["row_id"],
                                     self.group["row_id"], vals=self.vals)
        if self.subgroup:
            idx = [d for d in idx if d["subgroup_id"] == self.subgroup["row_id"]]
        # dedup mantenendo l'ordine
        seen, uniq = set(), []
        for d in idx:
            if d["revision_id"] not in seen:
                seen.add(d["revision_id"])
                uniq.append(d)
        self.drawings = uniq
        print(f"  {len(uniq)} tavole" + (" (filtrate sulla macchina)" if self.vals else ""))
        for i, d in enumerate(uniq):
            print(f"  {i:>4}  {d['rif']:<22} {d['name']}")

    def cmd_parts(self, arg):
        if not self.drawings:
            print("  esegui prima 'tav'")
            return
        if not arg.isdigit():
            print("  uso: parts <numero tavola>")
            return
        d = self.drawings[int(arg)]
        full = self.api.drawing(self.family["row_id"], self.model["row_id"],
                                self.group["row_id"], d["subgroup_id"],
                                d["revision_id"], vals=self.vals)
        meta = full["meta"]
        print(f"\n  {meta['name']}  (rif {meta['rif']}, rev {meta['revision_id']})")
        if meta["notes"]:
            print(f"  note: {meta['notes']}")
        if meta["tractor_sn_range"]:
            print(f"  matricole: {meta['tractor_sn_range']}")
        print()
        for p in full["parts"]:
            flags = ""
            if p["abolished"]:
                flags += " [ABOLITO]"
            if not p["sellable"]:
                flags += " [NON VENDIBILE]"
            if p["replaced"]:
                flags += " [SOSTITUITO]"
            print(f"    {p['position']:>6}  {p['code']:<18} x{str(p['quantity']):<4} "
                  f"{p['description']}{flags}")
            if p["replaced"]:
                for s in self.api.substitutions(p["code"]):
                    print(f"            -> {s['code']:<18} x{str(s['quantity']):<4} "
                          f"{s['description']}")
        self.db.save_drawing(meta, full["parts"], full["hotspots"],
                             self.brand, self.family["row_id"], self.model["row_id"])

    def cmd_find(self, arg):
        if not arg:
            print("  uso: find <codice o descrizione>")
            return
        codes = self.api.suggest_codes(arg)
        descr = self.api.suggest_descriptions(arg)
        if codes:
            print("  codici:")
            for c in codes[:20]:
                print(f"    {c['value']:<18} {c['label']}")
            if len(codes) > 20:
                print(f"    ... e altri {len(codes) - 20}")
        if descr:
            print("  descrizioni:")
            for d in descr[:20]:
                print(f"    {d['value']}")
            if len(descr) > 20:
                print(f"    ... e altre {len(descr) - 20}")
        if not codes and not descr:
            print("  nessun risultato")

    def cmd_where(self, arg):
        if not arg:
            print("  uso: where <codice>")
            return
        desc = self.api.part_description(arg)
        print(f"  {arg}: {desc or '(codice non trovato)'}")
        apps = self.api.applicabilities(arg)
        if not apps:
            print("  nessuna applicabilita' trovata")
            return
        by_level = {}
        for a in apps:
            by_level.setdefault(a["level"] or "?", []).append(a)
        for level, items in by_level.items():
            print(f"\n  --- {level} ({len(items)}) ---")
            for a in items[:25]:
                extra = f" [{a['code']}]" if a["code"] else ""
                print(f"    {a['brand']:<12} {a['description']}{extra}")
            if len(items) > 25:
                print(f"    ... e altri {len(items) - 25}")

    def cmd_subs(self, arg):
        if not arg:
            print("  uso: subs <codice>")
            return
        subs = self.api.substitutions(arg)
        self.db.save_substitutions(arg, subs)
        if not subs:
            print("  nessun sostituto")
            return
        for s in subs:
            print(f"    {s['code']:<18} x{str(s['quantity']):<4} {s['description']}")

    def cmd_crawl(self, arg):
        if arg == "model":
            if not self.need("family", "model"):
                return
            n = self.crawler.crawl_model(self.brand, self.family["row_id"],
                                         self.model["row_id"])
            print(f"  {n} righe ricambio salvate")
        elif arg == "family":
            if not self.need("family"):
                return
            n = self.crawler.crawl_family(self.brand, self.family["row_id"])
            print(f"  {n} righe ricambio salvate")
        else:
            print("  uso: crawl model | crawl family")

    def cmd_estimate(self, arg):
        for k, v in self.crawler.estimate(self.brand).items():
            print(f"  {k:<32} {v}")

    def cmd_stats(self, arg):
        for k, v in self.db.stats().items():
            print(f"  {k:<16} {v}")

    # ------------------------------------------------------------- loop
    def run(self):
        print(HELP)
        self.show_state()
        handlers = {
            "brand": self.cmd_brand, "fam": self.cmd_fam, "mod": self.cmd_mod,
            "vin": self.cmd_vin, "serial": self.cmd_serial, "engine": self.cmd_engine,
            "grp": self.cmd_grp, "sub": self.cmd_sub, "tav": self.cmd_tav,
            "parts": self.cmd_parts, "find": self.cmd_find, "where": self.cmd_where,
            "subs": self.cmd_subs, "crawl": self.cmd_crawl,
            "estimate": self.cmd_estimate, "stats": self.cmd_stats,
            "state": lambda a: self.show_state(),
            "help": lambda a: print(HELP),
        }
        while True:
            try:
                raw = input(f"\n[{self.brand_label}] > ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if not raw:
                continue
            cmd, _, arg = raw.partition(" ")
            cmd, arg = cmd.lower(), arg.strip()
            if cmd in ("quit", "exit", "q"):
                break
            fn = handlers.get(cmd)
            if not fn:
                print(f"  comando sconosciuto: {cmd}  (help per l'elenco)")
                continue
            try:
                fn(arg)
            except SessionExpired as e:
                print(f"\n  !! SESSIONE SCADUTA: {e}")
                print("  Rifai il login nel browser, aggiorna SDF_COOKIE e riavvia.")
                break
            except Exception:
                traceback.print_exc()
        self.db.close()
        print("ciao")


def main():
    cookie = config.leggi_credenziale("cookie.txt", "SDF_COOKIE")
    username = config.leggi_credenziale("sdf_username.txt", "SDF_USERNAME")
    password = config.leggi_credenziale("sdf_password.txt", "SDF_PASSWORD")
    Cli(cookie, username=username, password=password).run()


if __name__ == "__main__":
    main()
