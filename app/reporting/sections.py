"""
Sections: parts of a report that belong to particular tests.

A section is anchored by TEXT, not by position, so a revised format with
reordered or renamed parts still resolves:

    {"id": "ratio", "tests": ["ratio"],
     "start": {"type": "t", "text": "Measurement of voltage ratio"},
     "end":   {"type": "t", "text": "Check of phase displacement"}}          # block range

    {"id": "wr_rows", "tests": ["winding_res"],
     "rows": {"table": "ROUTINE TESTS", "from": "Measurement of winding resistance", "to": "LV winding resistance"}}

At generation time a section is printed when ANY of its tests was requested. A sheet that
contains sections is printed when at least one of them is, or when it contains one of the
"keep" texts (e.g. the Conclusion). "Sheet X of N" is recomputed from the printed sheets.
"""
from rapidfuzz import fuzz

from .docxutil import norm


def _match(block, anchor):
    if anchor.get("type") and block["type"] != anchor["type"]:
        return 0
    a = norm(anchor["text"])
    fp = block["fp"] if block["type"] == "t" else norm(block["text"])
    if anchor.get("contains") and block["type"] == "t":
        fp = norm(block["text"])
        return 100 if a in fp else 0
    if not a or not fp:
        return 0
    if anchor.get("exact"):
        return 100 if fp == a else 0
    if fp.startswith(a) or a in fp[:len(a) + 40]:
        return 100
    if len(fp) < 0.8 * len(a):
        return 0
    return min(99, fuzz.partial_ratio(a, fp[:max(len(a) + 20, 40)]))   # fuzzy never counts as exact


def find_block(blocks, anchor, start=0, min_score=90):
    """First block (from `start`) matching the anchor; `nth` (1-based) picks a later exact match."""
    best, best_i = 0, None
    need = int(anchor.get("nth", 1))
    for i in range(start, len(blocks)):
        s = _match(blocks[i], anchor)
        if s >= 100:
            need -= 1
            if need == 0:
                return i
            continue
        if s > best:
            best, best_i = s, i
    return best_i if best >= min_score else None


def resolve(parsed, sections):
    """Return (resolved, warnings). resolved: [{id, tests, kind, start, end | t, from_row, to_row, sheet}]"""
    blocks = parsed["blocks"]
    out, warnings = [], []
    for sec in sections:
        if "rows" in sec:
            spec = sec["rows"]
            ti = find_block(blocks, {"type": "t", "text": spec["table"], "contains": True})
            if ti is None:
                warnings.append(f"Section '{sec['id']}': table '{spec['table']}' not found - section dropped")
                continue
            t = next(t for t in parsed["tables"] if t["block"] == blocks[ti]["i"])
            rows = [" ".join(r) for r in t["rows"]]
            fr = next((i for i, r in enumerate(rows) if norm(spec["from"]) in norm(r)), None)
            to = next((i for i, r in enumerate(rows) if i >= (fr or 0) and norm(spec["to"]) in norm(r)), None)
            if fr is None or to is None:
                warnings.append(f"Section '{sec['id']}': rows not found - section dropped")
                continue
            out.append({"id": sec["id"], "tests": sec["tests"], "kind": "rows", "t": t["t"], "from_row": fr,
                        "to_row": to, "sheet": blocks[ti]["sheet"], "schedule": sec.get("schedule", True)})
            continue
        si = find_block(blocks, sec["start"])
        if si is None:
            warnings.append(f"Section '{sec['id']}': start '{sec['start']['text']}' not found - section dropped")
            continue
        ei = find_block(blocks, sec["end"], si) if sec.get("end") else si
        if ei is None:
            warnings.append(f"Section '{sec['id']}': end '{sec['end']['text']}' not found - section dropped")
            continue
        if blocks[si]["sheet"] != blocks[ei]["sheet"]:
            warnings.append(f"Section '{sec['id']}' spans two sheets - check its anchors")
        out.append({"id": sec["id"], "tests": sec["tests"], "kind": "block", "start": si, "end": ei,
                    "sheet": blocks[si]["sheet"], "schedule": sec.get("schedule", True)})
    return out, warnings


def visibility(meta, tests):
    """Which sections/sheets print for the requested tests, and their printed numbers."""
    tests = set(tests)
    show = {s["id"]: any(t in tests for t in s["tests"]) for s in meta["sections"]}
    sheet_flags = {}
    for sh in meta["sheets"]:
        n = int(sh["n"])
        if sh.get("keep") or not sh["sections"]:
            sheet_flags[n] = True
        else:
            sheet_flags[n] = any(show.get(s) for s in sh["sections"])
    shown = sorted(n for n, v in sheet_flags.items() if v)
    numbers = {}
    for n in sorted(sheet_flags):
        if n in shown:
            numbers[n] = shown.index(n) + 1
        else:   # a reference to a dropped sheet points at the next printed one
            later = [x for x in shown if x > n]
            numbers[n] = shown.index(later[0]) + 1 if later else len(shown)
    test_sheets = {}
    for s in meta["sections"]:
        if show[s["id"]] and s.get("schedule", True):
            for t in s["tests"]:
                if t in tests:
                    test_sheets.setdefault(t, set()).add(numbers[int(s["sheet"])])
    return show, sheet_flags, numbers, len(shown), {t: sorted(v) for t, v in test_sheets.items()}
