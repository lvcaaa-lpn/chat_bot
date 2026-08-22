"""Wrapper tipizzato sugli endpoint CRIC.

L'API mescola due convenzioni di naming (ROW_ID/DISPLAY_NAME in maiuscolo per
groups/families/models, rowId/description in camelCase altrove). Qui viene
normalizzato tutto a snake_case, cosi' il resto del codice non se ne accorge.
"""
from .client import SEARCH_CODE, SEARCH_DESCR


def _pick(d, *keys, cast=None, default=None):
    for k in keys:
        v = d.get(k)
        if v not in (None, ""):
            return cast(v) if cast else v
    return default


def _as_list(x):
    if x is None:
        return []
    return x if isinstance(x, list) else [x]


class SdfApi:
    def __init__(self, client):
        self.c = client

    # ---------------------------------------------------------------- albero
    def families(self, brand=None):
        raw = _as_list(self.c.get("cric/families", brand=brand))
        return [{
            "row_id": _pick(f, "ROW_ID", "rowId", cast=int),
            "code": _pick(f, "CODE", "code"),
            "name": _pick(f, "DISPLAY_NAME", "description"),
            "macro": _pick(f, "MACROFAMIGLIA"),
            "image_url": _pick(f, "IMAGE_URL", "imageUrl"),
        } for f in raw]

    def models(self, family_id, brand=None):
        raw = _as_list(self.c.get("cric/models", brand=brand, familyRowId=family_id))
        out = []
        for m in raw:
            name = _pick(m, "DISPLAY_NAME", "description", default="")
            # "ARGON 65 -> NNZJY002W0BS00001": dopo la freccia c'e' il VIN iniziale
            vin = name.split("->")[-1].strip() if "->" in name else None
            out.append({
                "row_id": _pick(m, "ROW_ID", "rowId", cast=int),
                "code": _pick(m, "CODE", "code"),
                "name": name,
                "sort_key": _pick(m, "SORT_KEY", "sortKey"),
                "family_id": family_id,
                "start_vin": vin,
            })
        return out

    def groups(self, family_id, model_id, brand=None):
        raw = _as_list(self.c.get("cric/groups", brand=brand,
                                  familyRowId=family_id, modelRowId=model_id))
        return [{
            "row_id": _pick(g, "ROW_ID", "rowId", cast=int),
            "code": _pick(g, "CODE", "code"),
            "name": _pick(g, "DISPLAY_NAME", "description"),
            "sort_key": _pick(g, "SORT_KEY", "sortKey"),
            "image_url": _pick(g, "IMAGE_URL", "imageUrl"),
            "subgroups": [{
                "row_id": _pick(s, "ROW_ID", "rowId", cast=int),
                "code": _pick(s, "CODE", "code"),
                "name": _pick(s, "DISPLAY_NAME", "description"),
                "sort_key": _pick(s, "SORT_KEY", "sortKey"),
            } for s in _as_list(g.get("subGroupList"))],
        } for g in raw]

    # --------------------------------------------------------------- tavole
    def drawing_index(self, family_id, model_id, group_id, vals="", brand=None):
        """Una sola chiamata: elenca TUTTE le tavole del gruppo (tutti i subgroup)."""
        res = self.c.post("cric/drawing", brand=brand, vals=vals, allSheets=True,
                          rowIds=[family_id, model_id, group_id]) or {}
        return [self._drawing_meta(d) for d in _as_list(res.get("drawingList"))]

    def drawing(self, family_id, model_id, group_id, subgroup_id, revision_id,
                vals="", brand=None):
        """Contenuto di UNA tavola: metadati + ricambi + hotspot."""
        res = self.c.post("cric/drawing", brand=brand, vals=vals, allSheets=False,
                          rowIds=[family_id, model_id, group_id,
                                  subgroup_id, revision_id]) or {}
        cur = res.get("currentRevision") or {}
        pl = res.get("partList") or {}
        return {
            "meta": self._drawing_meta(cur),
            "requested_revision": revision_id,
            "got_revision": cur.get("revisionRowId"),
            "parts": [self._part(p) for p in _as_list(pl.get("parts"))],
            "hotspots": [{
                "position": h.get("position"), "code": h.get("code"),
                "x1": int(h["x1"]), "y1": int(h["y1"]),
                "x2": int(h["x2"]), "y2": int(h["y2"]),
            } for h in _as_list(pl.get("hotSpots")) if h.get("x1")],
            "emaps_url": res.get("emapsUrl"),
            "pdf_url": res.get("pdfPageUrl"),
            "other_drawings": [self._drawing_meta(d)
                               for d in _as_list(res.get("drawingList"))],
        }

    @staticmethod
    def _drawing_meta(d):
        notes = d.get("notes")
        return {
            "revision_id": d.get("revisionRowId"),
            "name": d.get("drawingName"),
            "rif": d.get("rif"),
            "nove_punto": d.get("novePunto"),
            "image_name": d.get("imageName"),
            "preview_url": d.get("previewUrl"),
            "tractor_sn_range": d.get("tractorSNRange"),
            "engine_sn": d.get("engineSN"),
            "notes": " | ".join(notes) if isinstance(notes, list) else notes,
            "family_id": d.get("familyRowId"),
            "model_id": d.get("modelRowId"),
            "group_id": d.get("groupRowId"),
            "subgroup_id": d.get("subGroupRowId"),
        }

    @staticmethod
    def _part(p):
        return {
            "code": p.get("code"),
            "description": p.get("description"),
            "quantity": p.get("quantity"),
            "position": p.get("position"),
            "sellable": p.get("sellable") == "Y",
            "replaced": str(p.get("replaced")).lower() in ("true", "1"),
            "abolished": bool(p.get("abolished")),
            "valid": bool(p.get("valid", True)),
            "price": p.get("price"),
            "note": p.get("note"),
        }

    # -------------------------------------------------------------- ricerca
    def suggest(self, text, search_type=None, brand=None):
        """Autocomplete. search_type None = lascia decidere al server."""
        if search_type:
            raw = self.c.post("cric/search/searchParts", brand=brand,
                              familyRowId=None, modelRowId=None, groupRowId=None,
                              searchReplaced=None, searchType=search_type,
                              crossBrandSearch=None, searchString=text)
        else:
            raw = self.c.post("cric/search/autocompleteSearch", brand=brand,
                              familyRowId=None, modelRowId=None, groupRowId=None,
                              searchReplaced=None, searchType=None,
                              crossBrandSearch=None, searchString=text)
        return [{"label": x.get("label"), "type": x.get("type"), "value": x.get("value")}
                for x in _as_list(raw)]

    def suggest_codes(self, text, brand=None):
        return self.suggest(text, SEARCH_CODE, brand)

    def suggest_descriptions(self, text, brand=None):
        return self.suggest(text, SEARCH_DESCR, brand)

    def part_description(self, code, brand=None):
        res = self.c.post("cric/search/searchPartCode", brand=brand,
                          familyRowId=None, modelRowId=None, groupRowId=None,
                          searchReplaced=None, searchType=SEARCH_CODE,
                          crossBrandSearch=None, searchString=code) or {}
        return res.get("partDescription")

    def applicabilities(self, code, family_id=None, model_id=None, group_id=None,
                        cross_brand=False, brand=None):
        """Dove viene usato un codice. Richiede searchType=PART_REF."""
        raw = _as_list(self.c.post(
            "cric/search/searchPartApplicabilities", brand=brand,
            familyRowId=family_id, modelRowId=model_id, groupRowId=group_id,
            searchReplaced=None, searchType=SEARCH_CODE,
            crossBrandSearch=cross_brand or None, searchString=code))
        return [{
            "brand": n.get("brand"),
            "level": n.get("page"),            # family | model | group | ...
            "node": n.get("nodeName"),
            "row_id": n.get("rowId"),
            "description": n.get("description"),
            "code": n.get("code"),
            "family_id": n.get("familyRowId") or None,
            "model_id": n.get("modelRowId") or None,
            "group_id": n.get("groupRowId") or None,
            "subgroup_id": n.get("subGroupRowId") or None,
            "drawing_id": n.get("drawingRowId") or None,
            "path": n.get("path"),
            "abolished": bool(n.get("abolished")),
            "replaced": n.get("replaced"),
            "new_code": n.get("newCode"),
            "old_code": n.get("oldCode"),
        } for n in raw]

    def substitutions(self, code, brand=None):
        res = self.c.get("cric/part/substitutions", brand=brand, partCode=code)
        if not res:
            return []
        return [{
            "code": s.get("code"),
            "description": s.get("description"),
            "quantity": s.get("quantityString") or s.get("quantity"),
        } for s in _as_list(res.get("finalSubstitutions"))]

    # ------------------------------------------------------------- macchina
    def by_vin(self, vin, brand=None):
        """VIN completo -> famiglia/modello. Non accetta VIN parziali."""
        return _as_list(self.c.get("cric/chassy/searchByVin", brand=brand, vin=vin))

    def by_serial(self, family_id, model_id, serial, brand=None):
        """Matricola -> configurazione della singola macchina (seiPuntoList)."""
        return self._machine(self.c.get(
            "cric/chassy/searchBySerialNumber", brand=brand,
            familyRowId=family_id, modelRowId=model_id, serialNumber=serial))

    def by_engine(self, family_id, model_id, engine_number, brand=None):
        return self._machine(self.c.get(
            "cric/chassy/searchByEngineNumber", brand=brand,
            familyRowId=family_id, modelRowId=model_id, engineNumber=engine_number))

    @staticmethod
    def _machine(res):
        if not res:
            return None
        return {
            "row_id": res.get("rowId"),
            "serial": res.get("serialNumber"),
            "engine": res.get("engineNumber"),
            "variant_code": res.get("variantCode"),
            "model_code": res.get("modelCode"),
            "object_id": res.get("objectId"),
            "brand": res.get("brand"),
            # questa lista e' il 'vals' da passare a drawing per filtrare
            # le tavole sulla configurazione reale della macchina
            "vals": res.get("seiPuntoList") or [],
        }
