"""
Auto-mapper: proposes a binding for every slot found by the DOCX parser.

Strategies, in order (the first that applies wins):
  1. placeholder  - the slot already holds {{field.key}} and the key exists in the registry
  2. fingerprint  - the previous (active) template version had a slot with the same label context;
                    its binding is reused (exact fingerprint 1.0, near match 0.9). This is what keeps a
                    revised format (reordered sections, renamed labels) almost fully mapped.
  3. fuzzy        - rapidfuzz comparison of the slot's row label / column header / heading with each
                    registry field's `report_label` ("row || column"), assigned greedily by score
  4. ai           - optional: Gemini proposes bindings for the remaining slots (app/ai/template_mapper.py)

Template-level settings (sections, manual rules, QR picture, sheet marker) are inherited from the
previous version and re-anchored by text, so they survive reordering.
"""
import re

from rapidfuzz import fuzz

from .. import fields as F
from .docxutil import norm
from .registry import LISTS

FUZZY_MIN = 74          # required slots
FUZZY_MIN_OPTIONAL = 88  # optional slots (inline numbers, "Label :", observation defaults)
GROUP_NAMES = {"special": "special tests", "type": "type tests", "routine": "routine tests"}


def _n(t):
    t = norm(t)
    t = re.sub(r"\((x|\d+)\)", " ", t)
    t = t.replace("º", "°").replace("( )", " ").replace("()", " ")
    return re.sub(r"\s+", " ", t).strip()


def _candidates():
    out = []
    for f in F.all_fields():
        rl = f.get("report_label")
        if not rl:
            continue
        row, _, col = rl.partition("||")
        out.append({"key": f["key"], "row": _n(row), "col": _n(col), "multi": f.get("multi", False),
                    "label": f["label"]})
    return out


def _score(slot, cand):
    ctx = slot["context"]
    srow = _n(ctx.get("row", ""))
    scol = _n(" ".join(x for x in (ctx.get("col", ""), ctx.get("title", ""), ctx.get("heading", "")) if x))
    r = fuzz.token_sort_ratio(cand["row"], srow) if cand["row"] and srow else None
    if cand["row"] and not srow:
        r = 0
    c = None
    if cand["col"] and scol:
        c = fuzz.token_set_ratio(cand["col"], scol)
    elif cand["col"] and r is not None:
        r = r * 0.97          # the slot has no column context to confirm the field's column hint
    if r is not None and c is not None:
        return 0.65 * r + 0.35 * c
    if r is not None:
        # field has no column hint: penalise slightly if the slot sits in a multi-column grid
        return r - (4 if ctx.get("col") and slot["kind"] in ("cell_empty",) else 0)
    if c is not None:
        return c - (0 if not srow else 10)
    return 0


def _binding(kind, **kw):
    b = {"kind": kind}
    b.update(kw)
    return b


def map_loops(parsed):
    loops = []
    for t in parsed["tables"]:
        lp = t.get("loop")
        if not lp:
            continue
        best = None
        for name, spec in LISTS.items():
            cols, total = [], 0
            for h in lp["headers"]:
                hn = _n(h)
                scored = [(max(fuzz.token_set_ratio(_n(a), hn) for a in aliases), attr)
                          for attr, aliases in spec["columns"].items()]
                s, attr = max(scored)
                cols.append(attr if s >= 60 else None)
                total += s
            avg = total / max(len(lp["headers"]), 1)
            if best is None or avg > best[0]:
                best = (avg, name, cols)
        if best and best[0] >= 60:
            loops.append({"id": f"loop_t{t['t']}", "t": t["t"], "table_fp": t["fp"], "first_row": lp["first_row"],
                          "last_row": lp["last_row"], "list": best[1], "columns": best[2],
                          "confidence": round(best[0] / 100, 2)})
    return loops


def map_schedule(parsed):
    tests = F.tests()
    by_clause = {re.sub(r"\s+", "", (t["clause"] or "")).lower(): code for code, t in tests.items()}
    out = []
    for t in parsed["tables"]:
        sch = t.get("schedule")
        if not sch:
            continue
        rows = {}
        for r, cells in enumerate(t["rows"]):
            hit = None
            for c in cells:
                key = re.sub(r"\s+", "", c).lower()
                if key in by_clause:
                    hit = by_clause[key]
            if hit:
                # several tests can share a clause in theory; resolve by name similarity
                rows[str(r)] = {"kind": "test", "code": hit}
                continue
            uniq = set(x for x in cells if x)
            if len(uniq) == 1:
                txt = _n(next(iter(uniq)))
                for g, name in GROUP_NAMES.items():
                    if fuzz.token_set_ratio(name, txt) >= 85:
                        rows[str(r)] = {"kind": "group", "group": g}
        # two catalogue tests can have the same clause text in different groups (pressure test 21.3 d / 21.2 h):
        # by_clause is exact, so duplicates do not collide.
        out.append({"t": t["t"], "table_fp": t["fp"], "rows": rows, "sheet_col": sch["sheet_col"]})
    return out


def automap(parsed, previous=None, use_ai=False, ai_fn=None):
    """
    parsed   : docx_parser.ParsedDoc.to_json()
    previous : mapping dict of the template version being replaced (or None)
    Returns a mapping dict ready for the editor / compiler, including statistics.
    """
    slots = parsed["slots"]
    mapping = {
        "schema": 1,
        "sheet_marker": (previous or {}).get("sheet_marker", r"^TEST REPORT$"),
        "source_sheet_count": len(parsed["sheets"]),
        "slots": {},
        "loops": map_loops(parsed),
        "schedule": map_schedule(parsed),
        "sections": list((previous or {}).get("sections", [])),
        "replacements": list((previous or {}).get("replacements", [])),
        "keep_sheets_with": list((previous or {}).get("keep_sheets_with", ["Conclusion:"])),
        "qr_media": (previous or {}).get("qr_media"),
        "footer_seal": True,
    }
    registry = F.load()["by_key"]

    # 1. placeholders
    for s in slots:
        if s["kind"] == "placeholder" and s.get("placeholder") in registry:
            mapping["slots"][s["id"]] = dict(_binding("field", key=s["placeholder"]), confidence=1.0,
                                             method="placeholder", fp=s["fp"])

    # 2. previous version fingerprints
    if previous:
        prev = {v["fp"]: v for v in previous.get("slots", {}).values() if v.get("fp")}
        prev_items = list(prev.items())
        for s in slots:
            if s["id"] in mapping["slots"]:
                continue
            hit = prev.get(s["fp"])
            conf = 1.0
            if hit is None:
                kind_prefix = s["fp"].split("|", 1)[0]
                best, best_score = None, 0
                for fp, v in prev_items:
                    if fp.split("|", 1)[0] != kind_prefix:
                        continue
                    sc = fuzz.ratio(fp.rsplit("#", 1)[0], s["fp"].rsplit("#", 1)[0])
                    if sc > best_score:
                        best, best_score = v, sc
                if best is not None and best_score >= 93:
                    hit, conf = best, 0.9
            if hit is not None:
                b = {k: v for k, v in hit.items() if k not in ("confidence", "method", "fp")}
                mapping["slots"][s["id"]] = dict(b, confidence=conf, method="fingerprint", fp=s["fp"])

    # 3. fuzzy against registry report labels
    cands = _candidates()
    used = {v["key"] for v in mapping["slots"].values() if v.get("kind") == "field"}
    multi = {c["key"] for c in cands if c["multi"]}
    pairs = []
    for s in slots:
        if s["id"] in mapping["slots"]:
            continue
        floor = FUZZY_MIN if s["required"] else FUZZY_MIN_OPTIONAL
        for c in cands:
            sc = _score(s, c)
            if sc >= floor:
                pairs.append((sc, s["id"], c["key"], s))
    pairs.sort(key=lambda x: -x[0])
    for sc, sid, key, s in pairs:
        if sid in mapping["slots"]:
            continue
        if key in used and key not in multi:
            continue
        mapping["slots"][sid] = dict(_binding("field", key=key), confidence=round(sc / 100, 2), method="fuzzy",
                                     fp=s["fp"])
        used.add(key)

    # 4. optional AI proposals for what is left
    if use_ai and ai_fn:
        left = [s for s in slots if s["id"] not in mapping["slots"] and s["required"]]
        if left:
            try:
                for p in ai_fn(left, cands) or []:
                    sid, key = p.get("slot_id"), p.get("field_key")
                    s = next((x for x in left if x["id"] == sid), None)
                    if s and key in registry and float(p.get("confidence", 0)) >= 0.6:
                        mapping["slots"][sid] = dict(_binding("field", key=key), confidence=float(p["confidence"]),
                                                     method="ai", fp=s["fp"])
            except Exception as exc:  # AI is optional - never break mapping
                mapping["ai_error"] = str(exc)

    mapping["stats"] = stats(parsed, mapping)
    return mapping


def stats(parsed, mapping):
    req = [s for s in parsed["slots"] if s["required"]]
    mapped_req = [s for s in req if s["id"] in mapping["slots"]]
    methods = {}
    for v in mapping["slots"].values():
        methods[v.get("method", "manual")] = methods.get(v.get("method", "manual"), 0) + 1
    return {"slots": len(parsed["slots"]), "required": len(req), "mapped_required": len(mapped_req),
            "coverage": round(len(mapped_req) / len(req), 3) if req else 1.0, "methods": methods,
            "unmapped_required": [s["id"] for s in req if s["id"] not in mapping["slots"]]}
