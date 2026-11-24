#!/usr/bin/env python3
"""Compare two flow sensors mounted IN SERIES on one line.

Two sensors in series see the same flow, so the ratio of their readings is the
error of the sensor under test relative to the reference — free of pump,
catheter and room temperature. This is the diagnostic for SEN4 after tanda T01
(see manifests/sensor-series-diag.toml for the physical setup and conditions).

Input: the two minute-level CSV files (format v2) written by the logger, one per
host. The script never pairs minute rows directly: each logger starts its own
60-s windows, so rows of two hosts are offset by up to a minute. Instead it
interpolates each sensor's CUMULATIVE volume on a common grid of blocks and takes
the flow of a block as volume / time. That is exact for any alignment.

Reported (test relative to reference):
  - volume ratio over all valid blocks (the number to compare with T01's 0.68),
  - drift: the block ratio fitted against time, and its change over the run,
  - linearity: the ratio at each flow plateau, plus the fit q_test = a + b·q_ref
    (b = gain, a = offset), useful when a syringe pump steps through rates,
  - noise: median within-minute standard deviation of each sensor,
  - temperature: the block ratio fitted against the sensors' own temperature,
    ratio = r0 + r_T·(T - T0), alone and together with time, so a drift and a
    temperature effect are told apart (they look alike when the room warms up
    during a run). Gives the correction q = q_test / (r0 + r_T·(T - T0)).
    Reported only, no PASS/FAIL limit yet: the healthy value comes from S0.
  - optionally, both sensors against a gravimetric volume (collected mass).
and a PASS/FAIL against acceptance limits (all adjustable by flags).

Why the temperature: in the S4 run (2026-11-23) SEN4/SEN3 rose from 0.729 to
0.745 when the room cooled from 29 to 24.5 °C in 11 minutes. A fit against flow
alone had shown an offset of -5.9 µL/min that was really this effect.

Blocks where either sensor flags air or over-range, or where the reference flow
is below --min-flow, are left out of every number.

Pure standard library. A figure is drawn only if matplotlib is importable.

Usage:
  python3 series_compare.py TEST.csv REF.csv [--block-min 10] [--out report.md]
  python3 series_compare.py TEST.csv REF.csv --last 30        # live check
  python3 series_compare.py TEST.csv REF.csv --collected-g 251.3 \\
      --collect-start 2026-11-25T10:00 --collect-end 2026-11-26T10:00
  python3 series_compare.py --selftest
"""
from __future__ import annotations

import argparse
import datetime as dt
import math
import os
import random
import statistics as st
import sys
import tempfile
from dataclasses import dataclass, field

# Column order of the logger's CSV format v2 (slf3s/CSV_FORMAT.md).
COLS = ["window_index", "UTC_Time", "Flow_ul_min", "Flow_std_ul_min",
        "Flow_min_ul_min", "Flow_max_ul_min", "Volume_uL",
        "DeviceTemperature_degC", "Flag_Air", "Flag_High_Flow", "N_samples",
        "N_valid", "N_air", "N_high_flow", "N_exp_smoothing"]

# Density of 0.9 % NaCl at ~22 °C, g/mL (for --collected-g).
NACL_DENSITY_G_ML = 1.0046


# ── Reading a run ─────────────────────────────────────────────────────────────
@dataclass
class Run:
    path: str
    meta: dict = field(default_factory=dict)   # "# key : value" header lines
    footer: str = ""                            # "# END ..." line, if closed
    t: list = field(default_factory=list)       # window time, epoch seconds
    q: list = field(default_factory=list)       # mean flow, uL/min (None = all air)
    sd: list = field(default_factory=list)      # within-window std, uL/min
    vol: list = field(default_factory=list)     # cumulative volume, uL
    temp: list = field(default_factory=list)    # sensor temperature, degC (None = missing)
    bad: list = field(default_factory=list)     # window has air or over-range

    @property
    def label(self) -> str:
        s = self.meta.get("sensor", "?")
        pos = self.meta.get("position", "")
        return f"{s} ({pos})" if pos else s


def _num(x: str):
    return None if x in ("", "nan", "NaN") else float(x)


def read_run(path: str) -> Run:
    run = Run(path=path)
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.rstrip("\n")
            if line.startswith("# END"):
                run.footer = line[2:]
                continue
            if line.startswith("#"):
                if ":" in line:
                    k, v = line[1:].split(":", 1)
                    run.meta[k.strip()] = v.strip()
                continue
            if line.startswith("window_index") or not line.strip():
                continue
            p = line.split(",")
            if len(p) < len(COLS):
                continue                      # a row cut by a live copy
            r = dict(zip(COLS, p))
            run.t.append(float(r["UTC_Time"]))
            run.q.append(_num(r["Flow_ul_min"]))
            run.sd.append(_num(r["Flow_std_ul_min"]))
            run.vol.append(float(r["Volume_uL"]))
            run.temp.append(_num(r["DeviceTemperature_degC"]))
            run.bad.append(int(r["N_air"]) > 0 or int(r["N_high_flow"]) > 0)
    if len(run.t) < 2:
        raise SystemExit(f"{path}: fewer than two data rows")
    return run


def interp(xs: list, ys: list, x: float) -> float:
    """Linear interpolation of ys(xs) at x; xs ascending, x inside the range."""
    lo, hi = 0, len(xs) - 1
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if xs[mid] <= x:
            lo = mid
        else:
            hi = mid
    x0, x1 = xs[lo], xs[hi]
    w = 0.0 if x1 == x0 else (x - x0) / (x1 - x0)
    return ys[lo] + w * (ys[hi] - ys[lo])


def any_bad(run: Run, a: float, b: float) -> bool:
    """True if any window of `run` that overlaps [a, b] flagged air/over-range.
    A window stamped t covers roughly (t - 60, t]."""
    return any(bad and (t > a) and (t - 60.0 < b)
               for t, bad in zip(run.t, run.bad))


# ── Blocks ────────────────────────────────────────────────────────────────────
@dataclass
class Block:
    t0: float
    t1: float
    q_test: float      # uL/min
    q_ref: float
    dv_test: float     # uL
    dv_ref: float
    steady: bool = True   # reference flow constant (±5 %) inside the block
    temp: float | None = None   # mean temperature of both sensors in the block, degC

    @property
    def ratio(self) -> float:
        return self.q_test / self.q_ref

    @property
    def t_mid(self) -> float:
        return (self.t0 + self.t1) / 2


def make_blocks(test: Run, ref: Run, start: float, end: float,
                block_s: float, min_flow: float):
    kept, dropped = [], {"air/over-range": 0, "low flow": 0}
    a = start
    while a + block_s <= end + 1e-6:
        b = a + block_s
        dvt = interp(test.t, test.vol, b) - interp(test.t, test.vol, a)
        dvr = interp(ref.t, ref.vol, b) - interp(ref.t, ref.vol, a)
        minutes = block_s / 60.0
        qt, qr = dvt / minutes, dvr / minutes
        if any_bad(test, a, b) or any_bad(ref, a, b):
            dropped["air/over-range"] += 1
        elif qr < min_flow:
            dropped["low flow"] += 1
        else:
            inside = [q for t, q in zip(ref.t, ref.q)
                      if q is not None and a + 60.0 <= t <= b]
            steady = (len(inside) >= 2 and
                      (max(inside) - min(inside)) <= 0.05 * st.mean(inside))
            kept.append(Block(a, b, qt, qr, dvt, dvr, steady,
                              block_temp(test, ref, a, b)))
        a = b
    return kept, dropped


def block_temp(test: Run, ref: Run, a: float, b: float):
    """Mean sensor temperature over [a, b]: the windows whose middle (t - 30 s)
    falls inside the block, both sensors pooled. None if no window has one."""
    v = [x for r in (test, ref) for t, x in zip(r.t, r.temp)
         if x is not None and a < t - 30.0 <= b]
    return st.mean(v) if v else None


def lstsq(rows: list, ys: list):
    """Least squares y = X·beta for a few columns, by the normal equations.
    Returns (beta, standard errors, residual sd), or None if X'X is singular."""
    k, n = len(rows[0]), len(rows)
    if n <= k:
        return None
    xtx = [[sum(r[i] * r[j] for r in rows) for j in range(k)] for i in range(k)]
    xty = [sum(r[i] * y for r, y in zip(rows, ys)) for i in range(k)]
    # Gauss-Jordan inverse of X'X (k <= 3 here, well conditioned once centred).
    m = [row[:] + [1.0 if i == j else 0.0 for j in range(k)] for i, row in enumerate(xtx)]
    for c in range(k):
        p = max(range(c, k), key=lambda i: abs(m[i][c]))
        if abs(m[p][c]) < 1e-12:
            return None
        m[c], m[p] = m[p], m[c]
        piv = m[c][c]
        m[c] = [v / piv for v in m[c]]
        for i in range(k):
            if i != c and m[i][c] != 0.0:
                f = m[i][c]
                m[i] = [vi - f * vc for vi, vc in zip(m[i], m[c])]
    inv = [row[k:] for row in m]
    beta = [sum(inv[i][j] * xty[j] for j in range(k)) for i in range(k)]
    res = [y - sum(bi * xi for bi, xi in zip(beta, r)) for r, y in zip(rows, ys)]
    s2 = sum(e * e for e in res) / (n - k)
    se = [math.sqrt(max(s2 * inv[i][i], 0.0)) for i in range(k)]
    return beta, se, math.sqrt(s2)


def corr(xs: list, ys: list) -> float:
    mx, my = st.mean(xs), st.mean(ys)
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    return sxy / math.sqrt(sxx * syy) if sxx > 0 and syy > 0 else 0.0


def ols(xs: list, ys: list):
    """Least squares y = a + b·x. Returns (a, b, r2)."""
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx == 0:
        return my, 0.0, 0.0
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    b = sxy / sxx
    a = my - b * mx
    ss_tot = sum((y - my) ** 2 for y in ys)
    ss_res = sum((y - a - b * x) ** 2 for x, y in zip(xs, ys))
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else 1.0
    return a, b, r2


def plateaus(blocks: list, rel_gap: float = 0.15, min_blocks: int = 3):
    """Group the STEADY blocks into flow levels: sort by reference flow and start a
    new level wherever consecutive flows differ by more than rel_gap (relative).
    Blocks that straddle a change of rate are left out; so are levels with fewer
    than min_blocks blocks."""
    s = sorted((b for b in blocks if b.steady), key=lambda b: b.q_ref)
    if not s:
        return []
    groups, cur = [], [s[0]]
    for b in s[1:]:
        if (b.q_ref - cur[-1].q_ref) / cur[-1].q_ref > rel_gap:
            groups.append(cur)
            cur = [b]
        else:
            cur.append(b)
    groups.append(cur)
    return [g for g in groups if len(g) >= min_blocks]


# ── Time helpers ──────────────────────────────────────────────────────────────
def parse_when(text: str, run_start: float) -> float:
    """'+90' = minutes after the common start; otherwise a local ISO time."""
    if text.startswith("+"):
        return run_start + float(text[1:]) * 60.0
    return dt.datetime.fromisoformat(text).timestamp()


def hhmm(t: float) -> str:
    return dt.datetime.fromtimestamp(t).strftime("%d %H:%M")


# ── Analysis ──────────────────────────────────────────────────────────────────
def analyse(args) -> int:
    test, ref = read_run(args.test), read_run(args.ref)
    common0 = max(test.t[0], ref.t[0])
    common1 = min(test.t[-1], ref.t[-1])
    if common1 - common0 < args.block_min * 60:
        raise SystemExit("the two runs overlap for less than one block — "
                         "were they launched together?")
    start = parse_when(args.start, common0) if args.start else common0
    end = parse_when(args.end, common0) if args.end else common1
    if args.last:
        start = max(start, end - args.last * 60.0)
    start, end = max(start, common0), min(end, common1)

    blocks, dropped = make_blocks(test, ref, start, end,
                                  args.block_min * 60.0, args.min_flow)
    out = []
    w = out.append
    w(f"# Series comparison — {test.label} vs {ref.label}")
    w("")
    w(f"- Test: `{os.path.basename(test.path)}` · sensor {test.meta.get('sensor','?')} · "
      f"host {test.meta.get('device_id','?')} · condition {test.meta.get('configuration','?')}")
    w(f"- Reference: `{os.path.basename(ref.path)}` · sensor {ref.meta.get('sensor','?')} · "
      f"host {ref.meta.get('device_id','?')} · condition {ref.meta.get('configuration','?')}")
    if test.meta.get("configuration") != ref.meta.get("configuration"):
        w("- ⚠ The two files are from DIFFERENT conditions — check they were in series together.")
    for r in (test, ref):
        w(f"- {r.label} footer: {r.footer or 'none (run still open or cut)'}")
    w(f"- Window analysed: {hhmm(start)} → {hhmm(end)} "
      f"({(end - start) / 3600:.2f} h), blocks of {args.block_min:g} min")
    w(f"- Blocks kept: {len(blocks)} · left out: "
      + ", ".join(f"{v} {k}" for k, v in dropped.items()))
    w("")
    if len(blocks) < 3:
        w("**Too few valid blocks to judge.** Check the flow is running and air-free.")
        _emit(out, args)
        return 2

    ratios = [b.ratio for b in blocks]
    vol_ratio = sum(b.dv_test for b in blocks) / sum(b.dv_ref for b in blocks)
    hours = [(b.t_mid - blocks[0].t_mid) / 3600.0 for b in blocks]
    a_d, b_d, r2_d = ols(hours, ratios)
    span_h = hours[-1] - hours[0]
    drift_total = b_d * span_h                      # change of ratio over the run
    a_l, b_l, r2_l = ols([b.q_ref for b in blocks], [b.q_test for b in blocks])

    def med_sd(run: Run) -> float:
        v = [s for t, s, bad in zip(run.t, run.sd, run.bad)
             if s is not None and not bad and start < t <= end]
        return st.median(v) if v else float("nan")
    sd_t, sd_r = med_sd(test), med_sd(ref)

    w("## Result")
    w("")
    w("| Measure | Value |")
    w("| --- | --- |")
    w(f"| Volume ratio test/ref | **{vol_ratio:.4f}** ({(vol_ratio - 1) * 100:+.2f} %) |")
    w(f"| Block ratio: median [min, max] | {st.median(ratios):.4f} [{min(ratios):.4f}, {max(ratios):.4f}] |")
    w(f"| Drift of the ratio | {b_d * 100:+.3f} %/h → {drift_total * 100:+.2f} % over {span_h:.1f} h (r² {r2_d:.2f}) |")
    w(f"| Fit q_test = a + b·q_ref | b (gain) = {b_l:.4f}, a (offset) = {a_l:+.3f} µL/min (r² {r2_l:.4f}) |")
    w(f"| Mean flow: test / ref | {st.mean(b.q_test for b in blocks):.2f} / {st.mean(b.q_ref for b in blocks):.2f} µL/min |")
    w(f"| Noise (median within-minute sd): test / ref | {sd_t:.3f} / {sd_r:.3f} µL/min |")
    w("")

    temp_fit = _temperature_section(blocks, hours, args, w)

    levels = plateaus(blocks)
    if len(levels) > 1:
        w("## Ratio at each flow level")
        w("")
        w("| Reference flow (µL/min) | Blocks | Ratio test/ref |")
        w("| --- | --- | --- |")
        for g in levels:
            qr = st.mean(b.q_ref for b in g)
            rr = sum(b.dv_test for b in g) / sum(b.dv_ref for b in g)
            w(f"| {qr:.1f} | {len(g)} | {rr:.4f} |")
        w("")

    grav_ok = None
    if args.collected_g is not None:
        if not (args.collect_start and args.collect_end):
            raise SystemExit("--collected-g needs --collect-start and --collect-end")
        cs = parse_when(args.collect_start, common0)
        ce = parse_when(args.collect_end, common0)
        v_grav = args.collected_g / args.density * 1000.0          # uL
        w("## Gravimetric reference")
        w("")
        w(f"Collected {args.collected_g:.3f} g between {hhmm(cs)} and {hhmm(ce)} "
          f"→ {v_grav / 1000:.3f} mL at {args.density:.4f} g/mL.")
        w("")
        w("| Sensor | Volume measured (mL) | Ratio to gravimetric |")
        w("| --- | --- | --- |")
        grav_ok = True
        for r in (test, ref):
            v = interp(r.t, r.vol, min(ce, r.t[-1])) - interp(r.t, r.vol, max(cs, r.t[0]))
            w(f"| {r.label} | {v / 1000:.3f} | {v / v_grav:.4f} |")
            if r is ref and abs(v / v_grav - 1) > args.tol_grav:
                grav_ok = False
        w("")

    checks = [
        (f"volume ratio within ±{args.tol_ratio * 100:g} %", abs(vol_ratio - 1) <= args.tol_ratio),
        (f"drift over the run within ±{args.tol_drift * 100:g} %", abs(drift_total) <= args.tol_drift),
        (f"noise at most {args.max_noise_x:g}× the reference", sd_t <= args.max_noise_x * sd_r),
    ]
    if grav_ok is not None:
        checks.append((f"reference within ±{args.tol_grav * 100:g} % of gravimetric", grav_ok))
    w("## Verdict")
    w("")
    for name, ok in checks:
        w(f"- {'PASS' if ok else 'FAIL'} — {name}")
    verdict = all(ok for _, ok in checks)
    w("")
    w(f"**{'PASS' if verdict else 'FAIL'}**")

    if temp_fit is not None:
        w("")
        w(f"Temperature (reported, no limit yet): ratio {temp_fit['r0']:.4f} at "
          f"{args.t_ref:g} °C, {temp_fit['rel_pct']:+.2f} % per °C.")

    if args.plot:
        _plot(blocks, test, ref, args.plot)
        w("")
        w(f"Figure: `{args.plot}`")
    _emit(out, args)
    return 0 if verdict else 1


def _temperature_section(blocks: list, hours: list, args, w):
    """Fit the block ratio against temperature (and time); write the section.
    Returns the temperature-only fit as a dict, or None when it cannot be judged."""
    pairs = [(b, h) for b, h in zip(blocks, hours) if b.temp is not None]
    w("## Temperature")
    w("")
    if len(pairs) < 5:
        w("Not enough blocks with a sensor temperature to fit.")
        w("")
        return None
    temps = [b.temp for b, _ in pairs]
    t_span = max(temps) - min(temps)
    w(f"Sensor temperature over the blocks: {min(temps):.2f}–{max(temps):.2f} °C "
      f"(span {t_span:.2f} °C).")
    w("")
    if t_span < args.min_temp_span:
        w(f"The span is under {args.min_temp_span:g} °C: too narrow to measure a "
          "temperature effect. Nothing fitted.")
        w("")
        return None
    rs = [b.ratio for b, _ in pairs]
    hs = [h for _, h in pairs]
    dT = [x - args.t_ref for x in temps]
    f_t = lstsq([[1.0, d] for d in dT], rs)
    f_h = lstsq([[1.0, h] for h in hs], rs)
    f_th = lstsq([[1.0, d, h] for d, h in zip(dT, hs)], rs)
    if f_t is None or f_th is None or f_h is None:
        w("The fit is singular (temperature or time constant). Nothing fitted.")
        w("")
        return None
    (r0, rT), (se0, seT), s_t = f_t
    rel = 100.0 * rT / r0
    w("| Model of the ratio | Coefficients | Residual sd |")
    w("| --- | --- | --- |")
    w(f"| r0 + r_h·hours (drift only) | r_h = {f_h[0][1]:+.5f} /h | {f_h[2]:.4f} |")
    w(f"| r0 + r_T·(T − {args.t_ref:g}) | r0 = {r0:.4f} ± {se0:.4f}, "
      f"r_T = {rT:+.5f} ± {seT:.5f} /°C ({rel:+.2f} %/°C) | {s_t:.4f} |")
    (c0, cT, ch), (sc0, scT, sch), s_th = f_th
    w(f"| r0 + r_T·(T − {args.t_ref:g}) + r_h·hours | r0 = {c0:.4f}, "
      f"r_T = {cT:+.5f} ± {scT:.5f} /°C, r_h = {ch:+.5f} ± {sch:.5f} /h | {s_th:.4f} |")
    w("")
    rho = corr(temps, hs)
    if abs(rho) > 0.8:
        w(f"⚠ Temperature and time move together here (correlation {rho:+.2f}): drift "
          "and temperature cannot be separated in this run; the third row is not "
          "reliable. A run where the temperature also goes down and up is needed.")
    elif abs(rho) > 0.4:
        w(f"Temperature and time are partly tied (correlation {rho:+.2f}): the third "
          "row separates them, but with wider uncertainty (see its ±).")
    else:
        w(f"Temperature and time are not tied (correlation {rho:+.2f}), so the third "
          "row separates drift from temperature.")
    w("")
    w(f"Correction to the reference scale: q = q_test / ({r0:.4f} "
      f"{'+' if rT >= 0 else '−'} {abs(rT):.5f}·(T − {args.t_ref:g} °C)). "
      "It holds for this run only: a sensor whose gain changes with use needs it "
      "measured again each time.")
    w("")
    return {"r0": r0, "rT": rT, "rel_pct": rel, "sd": s_t}


def _emit(lines: list, args) -> None:
    text = "\n".join(lines) + "\n"
    sys.stdout.write(text)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(text)


def _plot(blocks, test, ref, path) -> None:
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        print(f"(matplotlib not available — no figure written to {path})", file=sys.stderr)
        return
    t = [dt.datetime.fromtimestamp(b.t_mid) for b in blocks]
    ratios = [b.ratio for b in blocks]
    tb = [(x, b.temp, b.ratio) for x, b in zip(t, blocks) if b.temp is not None]
    rows = 3 if tb else 2
    fig = plt.figure(figsize=(9, 3 * rows))
    ax1 = fig.add_subplot(rows, 1, 1)
    ax2 = fig.add_subplot(rows, 1, 2, sharex=ax1)
    ax1.plot(t, [b.q_ref for b in blocks], color="0.5", label=ref.label)
    ax1.plot(t, [b.q_test for b in blocks], color="tab:blue", label=test.label)
    ax1.set_ylabel("Flow (µL/min)")
    ax1.legend(frameon=False)
    ax2.plot(t, ratios, color="tab:blue")
    # The 1.0 line only when the ratio is near it: drawn far away it squashes
    # the scale and hides the variation the figure is for.
    if min(ratios) < 1.05 and max(ratios) > 0.95:
        ax2.axhline(1.0, color="0.5", ls="--", lw=1)
    ax2.set_ylabel("Ratio test / ref", color="tab:blue")
    for lab in ax1.get_xticklabels():
        lab.set_visible(False)
    if tb:
        ax2b = ax2.twinx()
        ax2b.plot([x for x, _, _ in tb], [v for _, v, _ in tb], color="tab:red", lw=1, alpha=0.7)
        ax2b.set_ylabel("Sensor temperature (°C)", color="tab:red")
        ax3 = fig.add_subplot(rows, 1, 3)
        ax3.scatter([v for _, v, _ in tb], [r for _, _, r in tb], s=8, color="tab:blue")
        ax3.set_xlabel("Sensor temperature (°C)")
        ax3.set_ylabel("Ratio test / ref")
    for lab in ax2.get_xticklabels():
        lab.set_rotation(30)
        lab.set_ha("right")
    fig.tight_layout()
    fig.savefig(path, dpi=130)


# ── Self-test ─────────────────────────────────────────────────────────────────
def _write_fake(path, meta, t0, seconds, q_of_t, offset_s, noise, seed,
                temp_of_t=lambda t: 25.0):
    """Write a synthetic v2 CSV: windows of 60 s starting at t0 + offset_s."""
    rnd = random.Random(seed)
    with open(path, "w", encoding="utf-8") as fh:
        for k, v in meta.items():
            fh.write(f"# {k:<18} : {v}\n")
        fh.write(",".join(COLS) + "\n")
        vol, t, i = 0.0, t0 + offset_s, 0
        while t + 60 <= t0 + seconds:
            q = q_of_t(t + 30) + rnd.gauss(0, noise / 50)   # mean of 3000 readings
            vol += q * 1.0                                  # 1 minute
            fh.write(f"{i},{t + 60:.3f},{q:.4f},{noise:.4f},{q - 3 * noise:.4f},"
                     f"{q + 3 * noise:.4f},{vol:.4f},{temp_of_t(t + 30):.4f},"
                     "0,0,3000,3000,0,0,0\n")
            t += 60
            i += 1
        fh.write("# END status=COMPLETE\n")


def selftest() -> int:
    """Synthetic series run: the reference reads the true flow; the test reads
    0.85×, falling linearly to 0.80× over 6 h, with 10× the noise. The flow steps
    20 → 50 → 80 → 150 uL/min. The two loggers start 23 s apart."""
    t0 = 1_800_000_000.0
    hours = 6
    steps = [20.0, 50.0, 80.0, 150.0]
    true_q = lambda t: steps[min(int((t - t0) / (hours * 3600 / len(steps))), len(steps) - 1)]
    gain = lambda t: 0.85 - 0.05 * (t - t0) / (hours * 3600)
    tmp = tempfile.mkdtemp(prefix="series_selftest_")
    ref_p, test_p = os.path.join(tmp, "ref.csv"), os.path.join(tmp, "test.csv")
    _write_fake(ref_p, {"sensor": "SEN1", "position": "downstream", "configuration": "S1",
                        "device_id": "fake"}, t0, hours * 3600, true_q, 0.0, 0.15, 1)
    _write_fake(test_p, {"sensor": "SEN4", "position": "upstream", "configuration": "S1",
                         "device_id": "fake"}, t0, hours * 3600,
                lambda t: gain(t) * true_q(t), 23.0, 1.5, 2)
    ns = build_parser().parse_args([test_p, ref_p, "--block-min", "10"])
    ns.out = os.path.join(tmp, "report.md")
    rc = analyse(ns)
    report = open(ns.out, encoding="utf-8").read()
    # Expected volume ratio: flow-weighted mean gain over the run.
    n = 10000
    tt = [t0 + (i + 0.5) * hours * 3600 / n for i in range(n)]
    expect = sum(gain(t) * true_q(t) for t in tt) / sum(true_q(t) for t in tt)
    got = float(report.split("Volume ratio test/ref | **")[1].split("**")[0])
    ok1 = abs(got - expect) < 0.005 and rc == 1 and "FAIL — drift" in report
    print(f"\nselftest 1 (faulty sensor): expected volume ratio {expect:.4f}, got {got:.4f}; "
          f"verdict FAIL as it should: {rc == 1} → {'OK' if ok1 else 'BROKEN'}\n")
    # Scenario 2: a healthy sensor 1 % high, same noise → must PASS.
    good_p = os.path.join(tmp, "good.csv")
    _write_fake(good_p, {"sensor": "SEN3", "position": "upstream", "configuration": "S0",
                         "device_id": "fake"}, t0, hours * 3600,
                lambda t: 1.01 * true_q(t), 41.0, 0.18, 3)
    ns2 = build_parser().parse_args([good_p, ref_p])
    ns2.out = os.path.join(tmp, "report2.md")
    rc2 = analyse(ns2)
    got2 = float(open(ns2.out, encoding="utf-8").read()
                 .split("Volume ratio test/ref | **")[1].split("**")[0])
    ok2 = rc2 == 0 and abs(got2 - 1.01) < 0.003
    print(f"\nselftest 2 (healthy sensor, +1 %): got {got2:.4f}; verdict PASS: {rc2 == 0} "
          f"→ {'OK' if ok2 else 'BROKEN'}")
    # Scenario 3: the S4 case — gain 0.733 at 27 °C, -0.0030 per °C, no drift; the
    # room swings 24-29 °C twice over the run while the flow slowly falls.
    temp3 = lambda t: 26.5 + 2.5 * math.sin(2 * math.pi * (t - t0) / (3 * 3600))
    q3 = lambda t: 90.0 - 2.0 * (t - t0) / 3600
    g3 = lambda t: 0.733 - 0.0030 * (temp3(t) - 27.0)
    ref3, test3 = os.path.join(tmp, "ref3.csv"), os.path.join(tmp, "test3.csv")
    _write_fake(ref3, {"sensor": "SEN3", "position": "upstream", "configuration": "S4",
                       "device_id": "fake"}, t0, hours * 3600, q3, 0.0, 0.15, 4, temp3)
    _write_fake(test3, {"sensor": "SEN4", "position": "downstream", "configuration": "S4",
                        "device_id": "fake"}, t0, hours * 3600,
                lambda t: g3(t) * q3(t), 17.0, 0.12, 5, temp3)
    ns3 = build_parser().parse_args([test3, ref3])
    ns3.out = os.path.join(tmp, "report3.md")
    analyse(ns3)
    rep3 = open(ns3.out, encoding="utf-8").read()
    try:
        line = rep3.split("Temperature (reported, no limit yet): ratio ")[1]
        r0_got = float(line.split(" at")[0])
        rel_got = float(line.split(", ")[1].split(" %")[0])
    except (IndexError, ValueError):
        r0_got, rel_got = float("nan"), float("nan")
    rel_true = 100 * -0.0030 / 0.733
    ok3 = abs(r0_got - 0.733) < 0.002 and abs(rel_got - rel_true) < 0.05
    print(f"\nselftest 3 (gain depends on temperature): r0 expected 0.7330, got {r0_got:.4f}; "
          f"{rel_true:+.2f} %/°C expected, got {rel_got:+.2f} → {'OK' if ok3 else 'BROKEN'}")
    return 0 if (ok1 and ok2 and ok3) else 1


# ── CLI ───────────────────────────────────────────────────────────────────────
def build_parser():
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("test", nargs="?", help="CSV of the sensor under test")
    p.add_argument("ref", nargs="?", help="CSV of the reference sensor")
    p.add_argument("--block-min", type=float, default=10.0,
                   help="block length in minutes (default 10)")
    p.add_argument("--min-flow", type=float, default=5.0,
                   help="leave out blocks where the reference reads less (uL/min, default 5)")
    p.add_argument("--start", help="start of the analysis: local ISO time or +minutes")
    p.add_argument("--end", help="end of the analysis: local ISO time or +minutes")
    p.add_argument("--last", type=float, help="analyse only the last N minutes (live check)")
    p.add_argument("--collected-g", type=float, help="mass collected at the outlet (g)")
    p.add_argument("--collect-start", help="when collection started (ISO or +minutes)")
    p.add_argument("--collect-end", help="when collection ended (ISO or +minutes)")
    p.add_argument("--density", type=float, default=NACL_DENSITY_G_ML,
                   help=f"liquid density g/mL (default {NACL_DENSITY_G_ML}, 0.9 %% NaCl)")
    p.add_argument("--tol-ratio", type=float, default=0.03,
                   help="accepted |volume ratio - 1| (default 0.03)")
    p.add_argument("--tol-drift", type=float, default=0.02,
                   help="accepted change of the ratio over the run (default 0.02)")
    p.add_argument("--max-noise-x", type=float, default=3.0,
                   help="accepted noise of test as a multiple of the reference (default 3)")
    p.add_argument("--tol-grav", type=float, default=0.03,
                   help="accepted |reference/gravimetric - 1| (default 0.03)")
    p.add_argument("--t-ref", type=float, default=27.0,
                   help="temperature the ratio is quoted at, degC (default 27)")
    p.add_argument("--min-temp-span", type=float, default=1.5,
                   help="fit the temperature effect only if the blocks span at least "
                        "this many degC (default 1.5)")
    p.add_argument("--out", help="also write the report to this Markdown file")
    p.add_argument("--plot", help="write a PNG figure here (needs matplotlib)")
    p.add_argument("--selftest", action="store_true",
                   help="run on synthetic data with a known error and check the result")
    return p


def main() -> int:
    args = build_parser().parse_args()
    if args.selftest:
        return selftest()
    if not (args.test and args.ref):
        build_parser().error("TEST.csv and REF.csv are required (or --selftest)")
    return analyse(args)


if __name__ == "__main__":
    sys.exit(main())
