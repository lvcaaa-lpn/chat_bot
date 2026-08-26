"""Popolamento incrementale del DB. Riprendibile dopo interruzione."""
from concurrent.futures import ThreadPoolExecutor, as_completed

from .client import SessionExpired

# quante tavole scaricare in parallelo dentro un gruppo. Misurato a mano col
# comando 'bench' del CLI SDF (vedi CLAUDE.md): su un campione di 34 tavole,
# 1 worker = 182s, 8 worker = 18.5s, 16 worker = 19.3s (nessun guadagno
# ulteriore, il portale stesso e' il collo di bottiglia oltre questa soglia,
# probabile sincronizzazione lato server sulla stessa sessione). 8 e' quindi
# il punto di massimo beneficio senza sovraccaricare inutilmente il portale
# del fornitore.
#
# ATTENZIONE: se il crawl parallelo dovesse dare problemi (es. il portale
# reagisce diversamente su una marca/famiglia diversa da quella testata),
# mettere MAX_WORKER = 1: crawl_group torna a chiamare
# _crawl_group_sequenziale, la versione precedente lasciata intatta sotto,
# senza dover toccare altro codice.
MAX_WORKER = 8


class Crawler:
    def __init__(self, api, db, verbose=True):
        self.api = api
        self.db = db
        self.v = verbose

    def log(self, msg, level=0):
        if self.v:
            print("  " * level + msg, flush=True)

    # ------------------------------------------------------------------
    def crawl_group(self, brand, family_id, model_id, group_id, with_subs=True,
                    progress=None):
        if MAX_WORKER <= 1:
            return self._crawl_group_sequenziale(brand, family_id, model_id,
                                                  group_id, with_subs, progress)

        key = f"grp:{brand}:{model_id}:{group_id}"
        if self.db.is_done(key):
            return 0

        index = self.api.drawing_index(family_id, model_id, group_id, brand=brand)

        # dedup mantenendo l'ordine, e separo subito cio' che non richiede
        # nessuna chiamata http (tavola gia' nota, basta il link) da cio'
        # che va davvero scaricato in parallelo
        seen = set()
        da_linkare, da_scaricare = [], []
        for meta in index:
            rev = meta["revision_id"]
            if rev in seen:
                continue
            seen.add(rev)
            if self.db.has_drawing(rev):
                da_linkare.append(meta)
            else:
                da_scaricare.append(meta)

        n_tot = len(da_linkare) + len(da_scaricare)
        n_fatte = 0
        n_parts = 0

        for meta in da_linkare:
            rev = meta["revision_id"]
            self.db.link_drawing(brand, family_id, model_id,
                                 meta["group_id"], meta["subgroup_id"], rev)
            self.log(f"[{rev}] {meta['name']} (gia' nota)", 3)
            n_fatte += 1
            if progress:
                progress(tavole_fatte=n_fatte, tavole_gruppo=n_tot,
                         etichetta=meta.get("name") or "")

        def scarica(meta):
            rev = meta["revision_id"]
            d = self.api.drawing(family_id, model_id, group_id,
                                 meta["subgroup_id"], rev, brand=brand)
            return meta, d

        with ThreadPoolExecutor(max_workers=MAX_WORKER) as ex:
            futures = [ex.submit(scarica, meta) for meta in da_scaricare]
            for fut in as_completed(futures):
                meta, d = fut.result()
                rev = meta["revision_id"]
                if d["got_revision"] != rev:
                    self.log(f"[{rev}] ATTENZIONE: ricevuta {d['got_revision']}", 3)

                self.db.save_drawing(d["meta"] if d["meta"]["revision_id"] else meta,
                                     d["parts"], d["hotspots"], brand, family_id, model_id)
                n_parts += len(d["parts"])
                self.log(f"[{rev}] {meta['name']} -> {len(d['parts'])} ricambi", 3)

                if with_subs:
                    for p in d["parts"]:
                        if p["replaced"] and not self.db.sub_checked(p["code"]):
                            # una singola sostituzione lenta/irraggiungibile non
                            # deve buttare via tutto il gruppo (mark_done non
                            # verrebbe mai chiamato, e il gruppo ripartirebbe
                            # da capo ad ogni tentativo, ritentando la stessa
                            # chiamata problematica in loop). Le sostituzioni
                            # sono un arricchimento, non essenziali quanto le
                            # tavole/ricambi stessi.
                            try:
                                subs = self.api.substitutions(p["code"], brand=brand)
                                self.db.save_substitutions(p["code"], subs)
                                if subs:
                                    self.log(f"{p['code']} -> " +
                                             ", ".join(s["code"] for s in subs), 4)
                            except SessionExpired:
                                raise
                            except Exception as e:
                                self.log(f"[{p['code']}] sostituzioni non "
                                        f"recuperate: {e!r}", 4)

                n_fatte += 1
                if progress:
                    progress(tavole_fatte=n_fatte, tavole_gruppo=n_tot,
                             etichetta=meta.get("name") or "")

        self.db.mark_done(key)
        return n_parts

    def _crawl_group_sequenziale(self, brand, family_id, model_id, group_id,
                                 with_subs=True, progress=None):
        """Versione precedente, una tavola alla volta. Tenuta come fallback:
        vedi MAX_WORKER sopra per come riattivarla."""
        key = f"grp:{brand}:{model_id}:{group_id}"
        if self.db.is_done(key):
            return 0

        index = self.api.drawing_index(family_id, model_id, group_id, brand=brand)
        n_parts = 0
        seen = set()
        n_tot = len(index)

        for n_fatte, meta in enumerate(index, 1):
            if progress:
                progress(tavole_fatte=n_fatte, tavole_gruppo=n_tot,
                         etichetta=meta.get("name") or "")
            rev = meta["revision_id"]
            if rev in seen:
                continue
            seen.add(rev)

            # tavola gia' scaricata da un altro modello: registra solo il link
            if self.db.has_drawing(rev):
                self.db.link_drawing(brand, family_id, model_id,
                                     meta["group_id"], meta["subgroup_id"], rev)
                self.log(f"[{rev}] {meta['name']} (gia' nota)", 3)
                continue

            d = self.api.drawing(family_id, model_id, group_id,
                                 meta["subgroup_id"], rev, brand=brand)
            if d["got_revision"] != rev:
                self.log(f"[{rev}] ATTENZIONE: ricevuta {d['got_revision']}", 3)

            self.db.save_drawing(d["meta"] if d["meta"]["revision_id"] else meta,
                                 d["parts"], d["hotspots"], brand, family_id, model_id)
            n_parts += len(d["parts"])
            self.log(f"[{rev}] {meta['name']} -> {len(d['parts'])} ricambi", 3)

            if with_subs:
                for p in d["parts"]:
                    if p["replaced"] and not self.db.sub_checked(p["code"]):
                        try:
                            subs = self.api.substitutions(p["code"], brand=brand)
                            self.db.save_substitutions(p["code"], subs)
                            if subs:
                                self.log(f"{p['code']} -> " +
                                         ", ".join(s["code"] for s in subs), 4)
                        except SessionExpired:
                            raise
                        except Exception as e:
                            self.log(f"[{p['code']}] sostituzioni non "
                                    f"recuperate: {e!r}", 4)

        self.db.mark_done(key)
        return n_parts

    def crawl_model(self, brand, family_id, model_id, with_subs=True,
                    progress=None):
        key = f"mod:{brand}:{model_id}"
        if self.db.is_done(key):
            self.log(f"modello {model_id} gia' fatto, salto", 1)
            return 0

        groups = self.api.groups(family_id, model_id, brand=brand)
        self.db.save_groups(groups)
        tot = 0
        n_gruppi = len(groups)

        def inoltra(i, nome_gruppo):
            def cb(**kw):
                if progress:
                    progress(gruppi_fatti=i, gruppi_totali=n_gruppi,
                             gruppo=nome_gruppo, **kw)
            return cb

        for i, g in enumerate(groups):
            self.log(f"{g['code']} - {g['name']}", 2)
            tot += self.crawl_group(brand, family_id, model_id, g["row_id"],
                                    with_subs, progress=inoltra(i, g["name"]))
        if progress:
            progress(gruppi_fatti=n_gruppi, gruppi_totali=n_gruppi,
                     gruppo="", tavole_fatte=0, tavole_gruppo=0,
                     etichetta="completato")
        self.db.mark_done(key)
        return tot

    def crawl_family(self, brand, family_id, with_subs=True):
        models = self.api.models(family_id, brand=brand)
        self.db.save_models(brand, models)
        tot = 0
        for m in models:
            self.log(f"{m['name']}", 1)
            tot += self.crawl_model(brand, family_id, m["row_id"], with_subs)
        return tot

    def crawl_brand(self, brand, with_subs=True, limit_families=None):
        families = self.api.families(brand=brand)
        self.db.save_families(brand, families)
        self.log(f"{brand}: {len(families)} famiglie")
        if limit_families:
            families = families[:limit_families]
        tot = 0
        for f in families:
            self.log(f"=== {f['name']} ===")
            try:
                tot += self.crawl_family(brand, f["row_id"], with_subs)
            except SessionExpired:
                raise
            except Exception as e:
                self.log(f"! errore su {f['name']}: {e}")
        return tot

    # ------------------------------------------------------------------
    def estimate(self, brand, sample=3):
        """Stima grossolana del lavoro totale su un campione di famiglie."""
        families = self.api.families(brand=brand)
        n_models = n_groups = n_draw = 0
        for f in families[:sample]:
            models = self.api.models(f["row_id"], brand=brand)
            n_models += len(models)
            if not models:
                continue
            m = models[0]
            groups = self.api.groups(f["row_id"], m["row_id"], brand=brand)
            n_groups += len(groups)
            for g in groups[:2]:
                n_draw += len(self.api.drawing_index(
                    f["row_id"], m["row_id"], g["row_id"], brand=brand))
        avg_models = n_models / max(sample, 1)
        avg_groups = n_groups / max(sample, 1)
        avg_draw = n_draw / max(1, min(2 * sample, n_groups))
        total_models = len(families) * avg_models
        total_calls = total_models * avg_groups * (1 + avg_draw)
        return {
            "famiglie": len(families),
            "modelli_medi_per_famiglia": round(avg_models, 1),
            "gruppi_medi_per_modello": round(avg_groups, 1),
            "tavole_medie_per_gruppo": round(avg_draw, 1),
            "modelli_totali_stimati": int(total_models),
            "chiamate_stimate": int(total_calls),
            "ore_stimate_a_0.4s": round(total_calls * 0.4 / 3600, 1),
        }