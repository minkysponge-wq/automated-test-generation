"""
Calculations and rule checks - declarative.

    computed fields   field_registry rows with source=computed and a `formula`
    rule checks       mappings/rules.yaml

Both are evaluated with **simpleeval** (an AST-whitelisting evaluator - Python's
eval is never used). `evaluate()` is a pure function of
(readings, declared data, requested tests, limits) and returns

    results : [{station, test, rule_code, label, value, limit, formula, passed}]
              passed = True / False / None (None = calculated value, no limit)
    calc    : {computed field key: value}   (used by the report generator)

Order: computed fields with stage=pre -> rules -> computed fields with
stage=post (these may use rule outcomes, e.g. obs('tr_hv') -> "Within limits").
"""
import functools
import itertools
import math
import os

import yaml
from simpleeval import EvalWithCompoundTypes, NameNotDefined

import config
from . import fields as F

RULES_YAML = os.path.join(os.path.dirname(config.FIELDS_YAML), "rules.yaml")
WORDS = ["Nil", "One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight", "Nine", "Ten", "Eleven",
         "Twelve", "Thirteen", "Fourteen", "Fifteen", "Sixteen", "Seventeen", "Eighteen", "Nineteen", "Twenty"]
MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
          "November", "December"]


class Missing(Exception):
    """A value needed by an expression has not been entered yet."""


# =============================================================================
# namespace: dotted keys -> attribute access  (wr.hv.n.bt.uv)
# =============================================================================
class Node:
    __slots__ = ("_d", "_path", "_strict")

    def __init__(self, d, path="", strict=True):
        self._d, self._path, self._strict = d, path, strict

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        v = self._d.get(name, _ABSENT)
        if v is _ABSENT:
            if self._strict:
                raise Missing(f"{self._path}{name}")
            return None
        if isinstance(v, dict):
            return Node(v, f"{self._path}{name}.", self._strict)
        if v is None and self._strict:
            raise Missing(f"{self._path}{name}")
        return v


_ABSENT = object()


def _tree(flat):
    """{'a.b.c': 1} -> {'a': {'b': {'c': 1}}}. A key that is also a prefix keeps its value under '' ."""
    root = {}
    for key, val in flat.items():
        cur = root
        parts = key.split(".")
        for p in parts[:-1]:
            nxt = cur.get(p)
            if not isinstance(nxt, dict):
                nxt = {} if nxt is None else {"": nxt}
                cur[p] = nxt
            cur = nxt
        if isinstance(cur.get(parts[-1]), dict):
            cur[parts[-1]][""] = val
        else:
            cur[parts[-1]] = val
    return root


# =============================================================================
# helper functions available to formulas and rules
# =============================================================================
def _f(x):
    d = F.to_number(x)
    return None if d is None else float(d)


def make_functions(ctx):
    """Functions exposed to expressions. `ctx` gives access to readings, form, results."""
    form = ctx["form"]

    def num(x, default=None):
        v = _f(x)
        if v is None:
            if default is None:
                raise Missing("number")
            return float(default)
        return v

    def s(x, default=""):
        return default if x in (None, "") else str(x)

    def fmt(x, dp=None):
        if x is None or x == "":
            return "—"
        return F.fmt(x, dp)

    def avg(*xs):
        xs = [num(x) for x in xs]
        return sum(xs) / len(xs)

    def unbalance(*xs):
        xs = [num(x) for x in xs]
        return (max(xs) - min(xs)) / (sum(xs) / len(xs)) * 100

    def wtemp(side):
        t = ctx["values"].get(f"wr.temp.{side}")
        if t is not None:
            return t
        a, b = ctx["values"].get(f"wr.oil_top.{side}"), ctx["values"].get(f"wr.oil_bot.{side}")
        if a is None or b is None:
            raise Missing(f"wr.temp.{side}")
        return (a + b) / 2

    def r75(r, t):
        return num(r) * (ctx["K"] + 75) / (ctx["K"] + num(t))

    def rated_current():
        kva, lv = _f(form.get("pf_kva")), _f(form.get("pf_lv_v"))
        if not kva or not lv:
            raise Missing("pf_kva/pf_lv_v")
        phases = _f(form.get("pf_phases")) or 3
        return kva * 1000 / (math.sqrt(3) * lv) if phases == 3 else kva * 1000 / lv

    def tap_list():
        return tap_percentages(form)

    def n_taps():
        return len(tap_list())

    def tap_pct(i):
        return tap_list()[int(i) - 1]

    def pct_text(p):
        return f"{p:+.1f}".replace("+0.0", "0.0")

    def ratio_dev(i, side):
        nom = nominal_ratio(form, tap_pct(i))
        if nom is None:
            raise Missing("rated voltages")
        meas = [ctx["values"].get(f"ratio.t{int(i)}.{side}.{p}") for p in ("u", "v", "w")]
        if any(m is None for m in meas):
            raise Missing(f"ratio.t{i}.{side}")
        devs = [(m - nom) / nom * 100 for m in meas]
        return max(devs, key=abs)

    def same_vg(a, b):
        return str(a).replace(" ", "").lower() == str(b).replace(" ", "").lower()

    def tapkey(text):
        t = (str(text or "").upper().replace('"', " ").split() or ["NT"])[0]    # 'NT "2V"' -> NT
        return {"NT": "nt", "HT": "ht", "LT": "lt", "N": "nt", "H": "ht", "L": "lt"}.get(t, "nt")

    def req_pk(tap_text):
        v = ctx["values"].get(f"sc.req.{tapkey(tap_text)}.pk")
        if v is None:
            raise Missing("sc.req.pk")
        return v

    def tap_name(text):
        return {"nt": "Principal", "ht": "Highest", "lt": "Lowest"}[tapkey(text)]

    def contains(text, *words):
        t = str(text or "").lower()
        return any(w.lower() in t for w in words)

    def withstood_text(text):
        return contains(text, "withstood", "no disruptive", "no breakdown")

    def last_rise():
        for h in range(16, -1, -1):
            t, a = ctx["values"].get(f"tr.h{h:02d}.top"), ctx["values"].get(f"tr.h{h:02d}.aavg")
            if t is not None and a is not None:
                return t - a
        raise Missing("hourly readings")

    def max_defl(prefix):
        pts = []
        for i in range(1, 7):
            a, b = ctx["values"].get(f"{prefix}.p{i}.init"), ctx["values"].get(f"{prefix}.p{i}.final")
            if a is not None and b is not None:
                pts.append(abs(a - b))
        if not pts:
            raise Missing(f"{prefix} reference points")
        return max(pts)

    def words(n):
        n = int(n)
        return WORDS[n] if 0 <= n < len(WORDS) else str(n)

    def month_year(text):
        try:
            mm, yy = str(text).split("/")
            return f"{MONTHS[int(mm) - 1]} {yy}"
        except (ValueError, IndexError):
            return s(text)

    def phases_word():
        return "Three" if str(form.get("pf_phases", "3")).strip() == "3" else "Single"

    def sample_name():
        smp = str(form.get("samples") or "Distribution Transformer")
        return "Distribution Transformer" if "distribution" in smp.lower() else smp

    def tap_text():
        n = n_taps()
        plus, minus = _f(form.get("pf_tap_plus")) or 0, _f(form.get("pf_tap_minus")) or 0
        step = _f(form.get("pf_tap_step")) or 2.5
        return f"+{plus:g} % to -{minus:g} % in steps of {step:g} % ({n} positions), off-circuit"

    def join_nonempty(sep, *xs):
        return sep.join(str(x) for x in xs if x not in (None, ""))

    # ---- rule outcomes (stage=post formulas only)
    def rule_ok(rid):
        r = ctx["results_by_id"].get(rid)
        return True if r is None or r["passed"] is None else bool(r["passed"])

    def rules_ok(prefix):
        return all(r["passed"] is not False for k, r in ctx["results_by_id"].items() if k.startswith(prefix))

    def test_ok(code):
        return all(r["passed"] is not False for r in ctx["results"] if r["test"] == code)

    def all_rules_ok():
        return not any(r["passed"] is False for r in ctx["results"])

    def obs(rid):
        return "Within limits" if rule_ok(rid) else "Not within limits"

    def withstood(rid, fallback=""):
        return "Withstood" if rule_ok(rid) and rid in ctx["results_by_id"] else s(fallback, "—")

    return {
        "num": num, "s": s, "f": fmt, "fmt": fmt, "avg": avg, "unbalance": unbalance, "wtemp": wtemp, "r75": r75,
        "rated_current": rated_current, "n_taps": n_taps, "tap_pct": tap_pct, "pct_text": pct_text,
        "nominal_ratio": lambda p: nominal_ratio(form, p), "ratio_dev": ratio_dev, "same_vg": same_vg,
        "req_pk": req_pk, "tap_name": tap_name, "contains": contains, "withstood_text": withstood_text,
        "last_rise": last_rise, "max_defl": max_defl, "words": words, "month_year": month_year,
        "phases_word": phases_word, "sample_name": sample_name, "tap_text": tap_text, "join_nonempty": join_nonempty,
        "rule_ok": rule_ok, "rules_ok": rules_ok, "test_ok": test_ok, "all_rules_ok": all_rules_ok, "obs": obs,
        "withstood": withstood, "upper": lambda x: str(x).upper(), "abs": abs, "min": min, "max": max,
        "round": round, "range": lambda a, b=None: list(range(int(a)) if b is None else range(int(a), int(b))),
        "sqrt": math.sqrt, "len": len, "str": str,
    }


# =============================================================================
# declared-data helpers (also used by the report generator)
# =============================================================================
def material_constant(form):
    """235 for copper, 225 for aluminium (IS 2026-1)."""
    return 225.0 if str(form.get("pf_winding_material", "")).lower().startswith("al") else 235.0


def tap_percentages(form):
    """[+5.0, +2.5, 0.0, -2.5, ...] from the declared tapping range."""
    plus, minus = _f(form.get("pf_tap_plus")) or 0.0, _f(form.get("pf_tap_minus")) or 0.0
    step = _f(form.get("pf_tap_step")) or 2.5
    n = int(_f(form.get("pf_num_taps")) or (round((plus + minus) / step) + 1))
    return [round(plus - i * step, 3) for i in range(n)]


def nominal_ratio(form, pct):
    """Winding (phase) voltage ratio for a tap, honouring the vector group (D/Y/Z on each side)."""
    hv, lv = _f(form.get("pf_hv_v")), _f(form.get("pf_lv_v"))
    if not hv or not lv:
        return None
    vg = str(form.get("pf_vector_group", "Dyn11")).strip()
    hv_conn = vg[:1].upper() if vg else "D"
    lv_conn = next((c for c in vg[1:] if c.isalpha() and c.lower() in "dyz"), "y").lower()
    hv_w = hv * (1 + pct / 100.0) / (1 if hv_conn == "D" else math.sqrt(3))
    lv_w = lv / (1 if lv_conn == "d" else math.sqrt(3))
    return hv_w / lv_w


def rated_lv_current(form):
    kva, lv = _f(form.get("pf_kva")), _f(form.get("pf_lv_v"))
    if not kva or not lv:
        return None
    return kva * 1000 / (math.sqrt(3) * lv)


# =============================================================================
# rule definitions
# =============================================================================
@functools.lru_cache(maxsize=1)
def _load_rules():
    with open(RULES_YAML, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def reload_rules():
    _load_rules.cache_clear()


class _Fmt(dict):
    """str.format_map helper that leaves unknown {names} untouched."""
    def __missing__(self, k):
        return "{" + k + "}"


def _sub(text, mapping):
    if not isinstance(text, str):
        return text
    try:
        return text.format_map(_Fmt(mapping))
    except (ValueError, IndexError, KeyError):
        return text


def _safe_format(template, values):
    try:
        return template.format(**values)
    except (ValueError, KeyError, TypeError, IndexError):
        v = values.get("value")
        return "" if v is None else str(v)


# =============================================================================
# evaluation
# =============================================================================
def _typed_values(readings):
    out = {}
    for k, v in readings.items():
        if v in (None, ""):
            continue
        f = F.field(k)
        if f and f["dtype"] == "number":
            n = _f(v)
            out[k] = n if n is not None else v
        else:
            out[k] = v
    return out


def evaluate(readings, form, tests, limits):
    tests = set(tests)
    values = _typed_values(readings)
    ctx = {"form": form, "values": values, "K": material_constant(form), "results": [], "results_by_id": {}}
    funcs = make_functions(ctx)
    lim = {k: v for k, v in limits.items()}
    calc = {}

    def names():
        tree = _tree(values)
        ns = {k: (Node(v, k + ".") if isinstance(v, dict) else v) for k, v in tree.items()}
        ns.update({"form": Node(form, "form.", strict=False), "lim": Node(lim, "lim."), "K": ctx["K"],
                   "True": True, "False": False, "None": None})
        return ns

    def run(expr, extra=None):
        ev = EvalWithCompoundTypes(functions=funcs, names=dict(names(), **(extra or {})))
        return ev.eval(expr)

    def compute(stage):
        pending = [f for f in F.computed_fields() if (f.get("stage") or "pre") == stage and f.get("formula")]
        # several passes resolve formulas that depend on other computed fields
        for _ in range(4):
            progressed, still = False, []
            for f in pending:
                try:
                    v = run(f["formula"])
                except (Missing, NameNotDefined, ZeroDivisionError, TypeError, ValueError, AttributeError, KeyError,
                        IndexError):
                    still.append(f)
                    continue
                calc[f["key"]] = v
                if isinstance(v, (int, float)) or isinstance(v, str):
                    values[f["key"]] = v
                progressed = True
            pending = still
            if not progressed:
                break

    compute("pre")

    spec = _load_rules()
    labels = spec.get("labels", {})
    for rule in spec.get("rules", []):
        if rule["test"] not in tests:
            continue
        loops = rule.get("foreach") or {}
        keys = list(loops)
        lists = []
        for k in keys:
            src = loops[k]
            if isinstance(src, str):
                try:
                    src = run(src)
                except (Missing, NameNotDefined, Exception):
                    src = []
            lists.append(list(src))
        combos = list(itertools.product(*lists)) if keys else [()]
        for combo in combos:
            sub = dict(zip(keys, (str(c) for c in combo)))
            for k, v in list(sub.items()):
                if k in labels and v in labels[k]:
                    sub[f"{k}_label"] = labels[k][v]
            rid = _sub(rule["id"], sub)
            res = {"station": rule.get("station"), "test": rule["test"], "rule_code": rid,
                   "label": _sub(rule.get("label", rid), sub), "value": "", "limit": "",
                   "formula": _sub(rule.get("formula", ""), sub), "passed": None}
            try:
                local = {}
                local["value"] = run(_sub(rule["value"], sub))
                if rule.get("limit") is not None:
                    local["limit"] = run(_sub(str(rule["limit"]), sub), local)
                for vk, vexpr in (rule.get("vars") or {}).items():
                    local[vk] = run(_sub(str(vexpr), sub), local)
                fmtvals = dict(sub, **local)
                res["label"] = _safe_format(res["label"], fmtvals) if "{" in res["label"] else res["label"]
                res["value"] = _safe_format(rule.get("display", "{value}"), fmtvals)
                res["limit"] = _safe_format(rule.get("limit_text", ""), fmtvals) if rule.get("limit_text") else ""
                res["formula"] = _safe_format(res["formula"], fmtvals) if "{" in res["formula"] else res["formula"]
                if rule.get("check"):
                    res["passed"] = bool(run(_sub(rule["check"], sub), local))
            except (Missing, NameNotDefined, ZeroDivisionError, TypeError, ValueError, AttributeError, KeyError,
                    IndexError):
                if not rule.get("report_missing"):
                    continue
                res["value"] = "awaiting data"
                res["label"] = res["label"].split("{")[0].strip() if "{" in res["label"] else res["label"]
            ctx["results"].append(res)
            ctx["results_by_id"][rid] = res

    compute("post")
    return ctx["results"], calc


def summary(results):
    """(passed, failed, info) counts."""
    p = sum(1 for r in results if r["passed"] is True)
    f = sum(1 for r in results if r["passed"] is False)
    return p, f, len(results) - p - f


def test_ok(results, test_code):
    rs = [r for r in results if r["test"] == test_code and r["passed"] is not None]
    return all(r["passed"] for r in rs) if rs else True
