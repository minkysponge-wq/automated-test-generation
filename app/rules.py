"""
Calculations and rule checks (IS 1180 (Part 1): 2014 based).

`evaluate()` is a pure function of (readings, declared data, requested tests,
limits) and returns
    results : list of dicts  {station, rule_code, test, label, value, limit, formula, passed}
              passed = True / False / None (None = calculated value, no limit)
    calc    : derived numbers used by the report (R75, NLC %, ratio rows, ...)

Every limit comes from the admin Config page (db.get_settings), so the lab can
tune them without touching code. The report's "Observations" columns are
filled from these results - never typed by hand.
"""
import math

from .fields import to_number

TAP_NAMES = {"n": "Principal", "h": "Highest", "l": "Lowest"}
TAP_LT = {"nt": "Principal", "ht": "Highest", "lt": "Lowest"}
SIDES = {"bt": "before SCW", "at": "after SCW"}


def _f(x):
    d = to_number(x)
    return None if d is None else float(d)


def _r(x, dp):
    return None if x is None else round(x, dp)


class _Ctx:
    def __init__(self, readings, form, tests, limits):
        self.rd, self.form, self.tests, self.lim = readings, form, set(tests), limits
        self.results, self.calc = [], {}

    def n(self, key):
        return _f(self.rd.get(key))

    def t(self, key):
        return (self.rd.get(key) or "").strip()

    def p(self, name, default=None):
        v = _f(self.form.get(name))
        return default if v is None else v

    def add(self, station, test, code, label, value, limit, formula, passed):
        self.results.append({"station": station, "test": test, "rule_code": code, "label": label,
                             "value": "" if value is None else str(value), "limit": limit or "",
                             "formula": formula or "", "passed": passed})

    def missing(self, station, test, code, label, formula=""):
        self.add(station, test, code, label, "awaiting data", "", formula, None)


def material_constant(form):
    """235 for copper, 225 for aluminium (IS 2026-1)."""
    return 225.0 if str(form.get("pf_winding_material", "")).lower().startswith("al") else 235.0


def rated_lv_current(form):
    kva, lv = _f(form.get("pf_kva")), _f(form.get("pf_lv_v"))
    if not kva or not lv:
        return None
    phases = _f(form.get("pf_phases")) or 3
    return kva * 1000 / (math.sqrt(3) * lv) if phases == 3 else kva * 1000 / lv


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


def _contains_any(text, words):
    t = text.lower()
    return any(w in t for w in words)


# ---------------------------------------------------------------------------
def evaluate(readings, form, tests, limits):
    c = _Ctx(readings, form, tests, limits)
    K = material_constant(form)
    c.calc["K"] = K
    i_rated = rated_lv_current(form)
    c.calc["i_rated"] = i_rated
    tol_x = float(limits["loss_crosscheck_tolerance_pct"])

    # ---------------- 21.2 a) winding resistance -> corrected to 75 degC
    if "winding_res" in c.tests:
        st = "losses"
        c.calc["hv_r75"], c.calc["lv_r75"] = {}, {}
        for side in ("bt", "at"):
            t = c.n(f"wr.temp.{side}")
            if t is None:
                tops = [c.n(f"wr.oil_top.{side}"), c.n(f"wr.oil_bot.{side}")]
                t = sum(tops) / 2 if None not in tops else None
            for tap in ("n", "h", "l"):
                vals = [c.n(f"wr.hv.{tap}.{side}.{p}") for p in ("uv", "vw", "wu")]
                if None in vals or t is None:
                    continue
                avg = sum(vals) / 3
                r75 = avg * (K + 75) / (K + t)
                c.calc["hv_r75"].setdefault(side, {})[tap] = r75
                c.add(st, "winding_res", f"hv_r75_{tap}_{side}", f"HV resistance at 75 °C, {TAP_NAMES[tap]} tap, {SIDES[side]}",
                      _r(r75, 4), "", f"R75 = Ravg x ({K:g}+75)/({K:g}+{t:g}) = {avg:.4f} x {(K + 75) / (K + t):.5f}", None)
            vals = [c.n(f"wr.lv.{side}.{p}") for p in ("uv", "vw", "wu")]
            if None not in vals and t is not None:
                avg = sum(vals) / 3
                r75 = avg * (K + 75) / (K + t)
                c.calc["lv_r75"][side] = r75
                c.add(st, "winding_res", f"lv_r75_{side}", f"LV resistance at 75 °C, {SIDES[side]} (mΩ)", _r(r75, 4), "",
                      f"R75 = Ravg x ({K:g}+75)/({K:g}+{t:g}) = {avg:.4f} x {(K + 75) / (K + t):.5f}", None)
            # phase unbalance (informative check, 2 %)
            hv_n = [c.n(f"wr.hv.n.{side}.{p}") for p in ("uv", "vw", "wu")]
            if None not in hv_n:
                avg = sum(hv_n) / 3
                unb = (max(hv_n) - min(hv_n)) / avg * 100
                c.add(st, "winding_res", f"hv_unbalance_{side}", f"HV phase resistance unbalance, principal tap, {SIDES[side]}",
                      f"{unb:.2f} %", "<= 2.00 %", "(Rmax - Rmin) / Ravg x 100", unb <= 2.0)

    # ---------------- 21.2 b) voltage ratio + vector group
    if "ratio" in c.tests:
        st = "routine"
        tol = float(limits["ratio_tolerance_pct"])
        rows = []
        for i, pct in enumerate(tap_percentages(form), start=1):
            nom = nominal_ratio(form, pct)
            row = {"tap": i, "pct": f"{pct:+.1f}".replace("+0.0", "0.0"), "nominal": nom}
            for side in ("bt", "at"):
                meas = [c.n(f"ratio.t{i}.{side}.{p}") for p in ("u", "v", "w")]
                row[side] = meas
                if None in meas or nom is None:
                    continue
                devs = [(m - nom) / nom * 100 for m in meas]
                worst = max(devs, key=abs)
                c.add(st, "ratio", f"ratio_t{i}_{side}", f"Voltage ratio tap {i} ({row['pct']} %), {SIDES[side]}",
                      f"{worst:+.2f} %", f"± {tol:.2f} % of {nom:.3f}",
                      f"(measured - nominal)/nominal x 100; worst of U/V/W = {meas[devs.index(worst)]}", abs(worst) <= tol)
            rows.append(row)
        c.calc["ratio_rows"] = rows
        declared = str(form.get("pf_vector_group", "")).replace(" ", "").lower()
        for side in ("bt", "at"):
            vg = c.t(f"vg.{side}")
            if vg:
                c.add(st, "ratio", f"vector_group_{side}", f"Vector group, {SIDES[side]}", vg, f"declared {form.get('pf_vector_group')}",
                      "measured == declared", vg.replace(" ", "").lower() == declared)

    # ---------------- 21.2 d) no-load loss and current / 21.4 c) at 112.5 %
    if "nll" in c.tests and i_rated:
        lim = float(limits["nlc_limit_100_pct"])
        for side in ("bt", "at"):
            i0 = c.n(f"nll.{side}.i")
            if i0 is None:
                c.missing("losses", "nll", f"nlc100_{side}", f"No-load current at 100 % V, {SIDES[side]}")
                continue
            pct = i0 / i_rated * 100
            c.calc[f"nlc100_pct_{side}"] = pct
            c.add("losses", "nll", f"nlc100_{side}", f"No-load current at 100 % V, {SIDES[side]}", f"{pct:.2f} %",
                  f"<= {lim:.1f} % ({i_rated * lim / 100:.3f} A)", f"I0/Irated x 100 = {i0} / {i_rated:.2f} x 100", pct <= lim)
    if "nlc_112" in c.tests and i_rated:
        lim = float(limits["nlc_limit_112_pct"])
        i0 = c.n("nll.o112.i")
        if i0 is None:
            c.missing("losses", "nlc_112", "nlc112", "No-load current at 112.5 % V")
        else:
            pct = i0 / i_rated * 100
            c.calc["nlc112_pct"] = pct
            c.add("losses", "nlc_112", "nlc112", "No-load current at 112.5 % V", f"{pct:.2f} %",
                  f"<= {lim:.1f} % ({i_rated * lim / 100:.3f} A)", f"I0/Irated x 100 = {i0} / {i_rated:.2f} x 100", pct <= lim)
        if "nll" not in c.tests and c.n("nll.bt.i") is not None:
            c.calc["nlc100_pct_bt"] = c.n("nll.bt.i") / i_rated * 100

    # ---------------- 21.2 c) impedance / load loss  +  energy efficiency (total losses)
    if "impedance" in c.tests:
        st = "losses"
        z_decl = c.p("pf_impedance")
        tol = float(limits["impedance_tolerance_pct"])
        for side in ("bt", "at"):
            z = c.n(f"calc.nt.{side}.z75")
            if z is None or z_decl is None:
                continue
            dev = (z - z_decl) / z_decl * 100
            c.add(st, "impedance", f"impedance_{side}", f"% impedance at 75 °C, principal tap, {SIDES[side]}",
                  f"{z:.3f} % ({dev:+.2f} %)", f"{z_decl:.2f} % ± {tol:.0f} %", "(Zmeas - Zdecl)/Zdecl x 100", abs(dev) <= tol)
        for side in ("bt", "at"):
            nll_p = c.n(f"nll.{side}.p")
            for tap in ("nt", "ht", "lt"):
                ll100, tl100 = c.n(f"calc.{tap}.{side}.ll100"), c.n(f"calc.{tap}.{side}.tl100")
                if None not in (nll_p, ll100, tl100):
                    calc_tl = nll_p + ll100
                    diff = abs(calc_tl - tl100) / calc_tl * 100
                    c.add(st, "impedance", f"xcheck_tl100_{tap}_{side}", f"Cross-check 100 % total loss, {TAP_LT[tap]} tap, {SIDES[side]}",
                          f"{tl100:.2f} W (recomputed {calc_tl:.2f} W)", f"difference <= {tol_x} %", "NLL + LL100", diff <= tol_x)
        max50, max100 = c.p("pf_max_tl50"), c.p("pf_max_tl100")
        for side in ("bt", "at"):
            tl50, tl100 = c.n(f"calc.nt.{side}.tl50"), c.n(f"calc.nt.{side}.tl100")
            if tl50 is not None and max50:
                c.add(st, "impedance", f"eel_tl50_{side}", f"Total loss at 50 % load, {SIDES[side]}", f"{tl50:.2f} W",
                      f"<= {max50:g} W ({form.get('pf_eel', '')})", "NLL + LL at 50 % load (75 °C)", tl50 <= max50)
            if tl100 is not None and max100:
                c.add(st, "impedance", f"eel_tl100_{side}", f"Total loss at 100 % load, {SIDES[side]}", f"{tl100:.2f} W",
                      f"<= {max100:g} W ({form.get('pf_eel', '')})", "NLL + LL at 100 % load (75 °C)", tl100 <= max100)

    # ---------------- 21.2 e/f/g) dielectric tests
    if "ir" in c.tests:
        for side in ("bt", "at"):
            for k, lab in (("hv_e", "HV-E"), ("lv_e", "LV-E"), ("hv_lv", "HV-LV")):
                v = c.n(f"ir.{side}.{k}")
                if v is not None:
                    c.add("routine", "ir", f"ir_{k}_{side}", f"Insulation resistance {lab}, {SIDES[side]}", f"{v:g} GΩ", "", "measured at 5 kV", None)
    if "induced" in c.tests:
        lv = c.p("pf_lv_v")
        for side in ("bt", "at"):
            v, obs = c.n(f"iov.{side}.v"), c.t(f"iov.{side}.obs")
            if v is None:
                continue
            ok_v = lv is None or v >= 2 * lv * 0.99
            ok_o = _contains_any(obs, ("withstood", "no disruptive", "no breakdown"))
            c.add("routine", "induced", f"induced_{side}", f"Induced over-voltage withstand, {SIDES[side]}",
                  f"{v:g} V, {obs}", f">= 2 x {lv:g} V and withstood" if lv else "withstood",
                  "applied >= 2 x rated LV; observation contains 'withstood'", ok_v and ok_o)
    if "sep_source" in c.tests:
        for w, decl in (("hvpf", "pf_hv_ac"), ("lvpf", "pf_lv_ac")):
            need = c.p(decl)
            for side in ("bt", "at"):
                kv, obs = c.n(f"{w}.{side}.kv"), c.t(f"{w}.{side}.obs")
                if kv is None:
                    continue
                ok = (need is None or kv >= need) and _contains_any(obs, ("withstood", "no disruptive", "no breakdown"))
                c.add("routine", "sep_source", f"{w}_{side}", f"{'HV' if w == 'hvpf' else 'LV'} separate source withstand, {SIDES[side]}",
                      f"{kv:g} kV, {obs}", f">= {need:g} kV and withstood" if need else "withstood",
                      "applied >= declared AC level; observation contains 'withstood'", ok)

    # ---------------- 21.4 b) short-circuit withstand
    if "sc_withstand" in c.tests:
        st = "sc"
        lim = float(limits["reactance_change_limit_pct"])
        c.calc["xchg"] = {}
        for tap in ("nt", "ht", "lt"):
            xb, xa = c.n(f"calc.{tap}.bt.x50"), c.n(f"calc.{tap}.at.x50")
            if None in (xb, xa):
                c.missing("losses", "sc_withstand", f"xchg_{tap}", f"% change in reactance, {TAP_LT[tap]} tap")
                continue
            ch = (xb - xa) / xb * 100
            c.calc["xchg"][tap] = ch
            c.add("losses", "sc_withstand", f"xchg_{tap}", f"% change in reactance after SC test, {TAP_LT[tap]} tap",
                  f"{ch:+.3f} %", f"|ΔX| <= {lim:.1f} %", f"(Xbefore - Xafter)/Xbefore x 100 = ({xb} - {xa})/{xb} x 100", abs(ch) <= lim)
        ptol = float(limits["sc_peak_tolerance_pct"])
        tapmap = {"NT": "nt", "HT": "ht", "LT": "lt", "N": "nt", "H": "ht", "L": "lt"}
        for n in range(2, 11):
            sid = f"s{n:03d}"
            pk = c.n(f"sc.{sid}.pk")
            tap = tapmap.get(c.t(f"sc.{sid}.tap").upper().replace("\"", "").strip(), "nt")
            req = c.n(f"sc.req.{tap}.pk")
            if pk is None or req is None:
                continue
            dev = (pk - req) / req * 100
            c.add(st, "sc_withstand", f"sc_peak_{sid}", f"Oscillogram {sid.upper()} peak current ({TAP_LT[tap]} tap)",
                  f"{pk:.2f} kA ({dev:+.1f} %)", f"{req:.2f} kA ± {ptol:.0f} %", "(Ipk - Ipk,req)/Ipk,req x 100", abs(dev) <= ptol)
        rms, dur, req = c.n("sc.s011.rms_avg"), c.n("sc.s011.dur"), c.n("sc.req.nt.rms")
        if rms is not None and dur is not None and req is not None:
            rtol, tmin = float(limits["sc_rms_tolerance_pct"]), float(limits["sc_thermal_min_duration_s"])
            dev = (rms - req) / req * 100
            c.add(st, "sc_withstand", "sc_thermal", "Thermal withstand shot S011", f"{rms:.2f} kA for {dur:.2f} s ({dev:+.1f} %)",
                  f"{req:.2f} kA ± {rtol:.0f} %, >= {tmin:.1f} s", "rms within tolerance and duration >= minimum",
                  abs(dev) <= rtol and dur >= tmin)
        for k, lab in (("obs.during", "Observation during SC test"), ("obs.after", "Observation after SC test")):
            obs = c.t(f"sc.{k}")
            if obs:
                c.add(st, "sc_withstand", k.replace(".", "_"), lab, obs, "no abnormality", "text check",
                      _contains_any(obs, ("no abnormal", "nil", "no damage")))

    # ---------------- 21.3 b) temperature rise
    if "temp_rise" in c.tests:
        st = "temprise"
        std_oil, std_w = float(limits["std_top_oil_rise"]), float(limits["std_winding_rise"])
        g_oil, g_w = c.p("pf_gtr_top_oil", std_oil), c.p("pf_gtr_winding", std_w)
        c.calc["tr"] = {"g_oil": g_oil, "g_w": g_w, "std_oil": std_oil, "std_w": std_w}
        top = c.n("tr.top_oil_rise")
        if top is not None:
            c.calc["tr"]["top"] = top
            c.add(st, "temp_rise", "tr_top_oil", "Top-oil temperature rise", f"{top:.1f} °C",
                  f"<= {min(g_oil, std_oil):g} °C (guaranteed {g_oil:g}, standard {std_oil:g})", "top oil - average ambient at steady state",
                  top <= min(g_oil, std_oil))
            last = None
            for h in range(16, -1, -1):
                t_, a_ = c.n(f"tr.h{h:02d}.top"), c.n(f"tr.h{h:02d}.aavg")
                if t_ is not None and a_ is not None:
                    last = (h, t_ - a_)
                    break
            if last:
                c.add(st, "temp_rise", "tr_top_oil_xcheck", "Cross-check top-oil rise with last hourly reading",
                      f"hour {last[0]}: {last[1]:.2f} °C", "within 0.3 °C of result", "top oil - avg ambient (logsheet)",
                      abs(last[1] - top) <= 0.3)
        amb = c.n("tr.amb_shutdown")
        tcold = c.n("tr.cold.amb")
        for w, lab in (("hv", "HV"), ("lv", "LV")):
            rh, rc, corr = c.n(f"tr.hot.{w}"), c.n(f"tr.cold.{w}"), c.n(f"tr.corr.{w}") or 0.0
            if None in (rh, rc, amb, tcold):
                c.missing(st, "temp_rise", f"tr_{w}", f"{lab} winding temperature rise")
                continue
            rise = rh / rc * (K + tcold) - K - amb + corr
            c.calc["tr"][w] = rise
            c.add(st, "temp_rise", f"tr_{w}", f"{lab} winding temperature rise", f"{rise:.1f} °C",
                  f"<= {min(g_w, std_w):g} °C (guaranteed {g_w:g}, standard {std_w:g})",
                  f"Δθ = (Rh/Rc)(K+tc) - K - ta + corr = ({rh}/{rc})({K:g}+{tcold}) - {K:g} - {amb} + {corr}",
                  round(rise, 1) <= min(g_w, std_w))
            typed = c.n(f"tr.{w}_rise")
            if typed is not None:
                c.add(st, "temp_rise", f"tr_{w}_xcheck", f"Cross-check {lab} rise typed on logsheet", f"{typed:.1f} °C (calc {rise:.2f})",
                      "within 0.2 °C", "logsheet vs recomputed", abs(typed - rise) <= 0.2)
        tl, nl, fl = c.n("tr.total_loss"), c.n("tr.nll"), c.n("tr.fll")
        if None not in (tl, nl, fl):
            diff = abs(tl - (nl + fl)) / (nl + fl) * 100
            c.add(st, "temp_rise", "tr_losses", "Total losses injected = NLL + FLL", f"{tl:.2f} W (NLL+FLL {nl + fl:.2f} W)",
                  f"difference <= {tol_x} %", "Ptotal = P0 + Pk(75 °C, lowest tap)", diff <= tol_x)

    # ---------------- 21.3 d) pressure (type) / 21.2 h) pressure (routine) / 21.2 j) oil leakage
    if "pressure_type" in c.tests:
        st = "pressure"
        lim = float(limits["pressure_perm_defl_max_mm"])
        for pre, lab in (("prt", "air pressure"), ("prv", "vacuum")):
            perm = c.n(f"{pre}.perm_defl")
            if perm is None:
                c.missing(st, "pressure_type", f"{pre}_defl", f"Permanent deflection after {lab}")
                continue
            c.add(st, "pressure_type", f"{pre}_defl", f"Permanent deflection of flat plate after release of {lab}", f"{perm:.2f} mm",
                  f"<= {lim:.1f} mm", "max |final - initial| over reference points", perm <= lim)
            pts = [(c.n(f"{pre}.p{i}.init"), c.n(f"{pre}.p{i}.final")) for i in range(1, 7)]
            pts = [abs(a - b) for a, b in pts if a is not None and b is not None]
            if pts:
                c.add(st, "pressure_type", f"{pre}_defl_xcheck", f"Cross-check {lab} deflection from reference points",
                      f"max {max(pts):.2f} mm", "equals reported value ± 0.01", "max |final - initial|", abs(max(pts) - perm) <= 0.011)
            obs = c.t(f"{pre}.obs")
            if obs:
                c.add(st, "pressure_type", f"{pre}_obs", f"Observation - {lab} test", obs, "no permanent deformation beyond limit",
                      "text check", _contains_any(obs, ("no permanent", "within limit", "no leak")))
    if "pressure_routine" in c.tests:
        obs = c.t("prr.obs")
        if obs:
            c.add("pressure", "pressure_routine", "prr_obs", "Pressure test (routine) observation", obs,
                  "no leakage / no permanent deformation", "text check", _contains_any(obs, ("no leak", "no permanent", "within limit")))
        else:
            c.missing("pressure", "pressure_routine", "prr_obs", "Pressure test (routine) observation")
    if "oil_leak" in c.tests:
        obs, head, test = c.t("ol.obs"), c.n("ol.head_kpa"), c.n("ol.test_kpa")
        if head is not None and test is not None:
            c.add("pressure", "oil_leak", "ol_pressure", "Oil leakage test pressure >= 2 x head pressure",
                  f"{test:g} kPa (head {head:g} kPa)", f">= {2 * head:g} kPa", "test pressure at base >= 2 x normal head", test >= 2 * head - 0.05)
        if obs:
            c.add("pressure", "oil_leak", "ol_obs", "Oil leakage observation", obs, "no leakage", "text check",
                  _contains_any(obs, ("no leak",)))
        else:
            c.missing("pressure", "oil_leak", "ol_obs", "Oil leakage observation")

    return c.results, c.calc


def summary(results):
    """(passed, failed, info) counts."""
    p = sum(1 for r in results if r["passed"] is True)
    f = sum(1 for r in results if r["passed"] is False)
    return p, f, len(results) - p - f


def test_ok(results, test_code):
    """True if no rule for this requested test failed (used for report 'Observations')."""
    rs = [r for r in results if r["test"] == test_code and r["passed"] is not None]
    return all(r["passed"] for r in rs) if rs else True
