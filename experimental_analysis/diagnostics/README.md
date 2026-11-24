# Sensor diagnostics — two sensors in series

Tools to check one flow sensor against another when both are mounted **in series
on the same line**. They see the same flow, so the ratio of their readings is the
error of the sensor under test, independent of the pump, the catheter and the room.

Written for SEN4 after tanda T01: it integrated 202.7 mL while the other three
lines, filled alike, integrated 285–287 mL, and its reading relative to the other
lines fell from 0.89 to 0.65 over ~36 h. The reference is SEN3. The physical setup, conditions and launch lines are in
[`manifests/sensor-series-diag.toml`](../../manifests/sensor-series-diag.toml).

| File | What it does |
| --- | --- |
| `series_compare.py` | Compares the two minute-level CSV files: volume ratio, drift of the ratio, ratio at each flow level, gain and offset, noise, effect of the sensor temperature on the ratio (separated from drift), optional gravimetric check, PASS/FAIL. Pure standard library; draws a figure if matplotlib is present. |
| `series_watch.sh` | Live check while a condition runs: copies both CSV files from their hosts and compares the last N minutes. Read-only on the hosts. |

## Setup

```
flow source → sensor upstream → short tube (no catheter) → sensor downstream
            → open outlet, at the height of the source, into a COVERED collector
```

- Each sensor stays on its own cable and host (SEN3 on pi10, SEN4 on pi9, SEN1 on
  pi8). Both loggers run at the same time; clocks are synced by NTP. The tubes
  between the sensors must be short enough to reach — move a host if needed.
- Fill both sensors slowly from below, with no air: an air reading in either
  sensor leaves those minutes out.
- The sensor under test is always `rep_1`, the reference `rep_2`.

## Conditions (in order)

| Condition | What | Source and length |
| --- | --- | --- |
| S0 | Control: SEN1 → SEN3, two sensors that agreed in T01 | optional |
| S1 | SEN3 → SEN4, SEN4 as it came out of T01 — **first, before any purge or cleaning** | elastomeric pump until it empties (catches drift); or syringe pump, 4 rates × 30 min |
| S2 | SEN4 → SEN3, order swapped | as S1, can be shorter |
| S3 | SEN3 → SEN4 after purging SEN4 at high flow | watch live right after the purge |
| S4 | SEN3 → SEN4 after cleaning SEN4 | as S1 |

A used pump refilled with saline is a valid source here: first use matters for the
study runs, not for a series test, because both sensors see whatever flow it gives.

Suggested rates for a syringe pump: 20, 50, 80 and 150 µL/min (the pumps of the
study delivered 40–90 µL/min). The long elastomeric run is what catches drift: in
T01 the error of SEN4 grew mostly between hours 27 and 34.

A condition stops in one of three ways: with an elastomeric pump and
`END_OF_INFUSION=on`, by itself ~20 min after the pump empties; with
`END_OF_INFUSION=off`, at `HOURS`; both end `status=COMPLETE`. A run stopped by
hand ends `INTERRUPTED`, which is fine for a diagnostic but must be said.

## Launch (per host, the operator edits the environment file)

```bash
# pi9 — sensor under test
MANIFEST=sensor-series-diag.toml
CONFIG=S1
EXPERIMENT_REP=rep_1
META="sensor=SEN4 position=downstream source=elastomeric"
DEVICE_LOGGER_ID=pi9
HOURS=96
END_OF_INFUSION=on

# pi10 — reference
MANIFEST=sensor-series-diag.toml
CONFIG=S1
EXPERIMENT_REP=rep_2
META="sensor=SEN3 position=upstream source=elastomeric"
DEVICE_LOGGER_ID=pi10
HOURS=96
END_OF_INFUSION=on
```

With a syringe pump instead, use `source=syringe`, `END_OF_INFUSION=off` and
`HOURS` = the planned length (e.g. 2.5 for 4 rates × 30 min).

Then `./run.sh --check` on both (it rejects a sensor on the wrong cable, a
position that does not match the plan, or a run that already exists), and
`./run.sh` on both within a few seconds.

## During and after

```bash
cd experimental_analysis/diagnostics
./series_watch.sh pi9 pi10 S1 30                    # live: last 30 min
# after the run, bring the files in with ../fetch_data.sh, then:
python3 series_compare.py ../Temp/campana-diag/S1/S1_rep_1.csv \
                          ../Temp/campana-diag/S1/S1_rep_2.csv \
                          --out S1_report.md --plot S1.png
```

`fetch_data.sh` files each run as `Temp/campana-<campaign>/<condition>/`, so these
land in `Temp/campana-diag/S1/`. Exit code: 0 = PASS, 1 = FAIL, 2 = too few valid blocks.

### Temperature

The report fits the block ratio against the sensors' own temperature,
`ratio = r0 + r_T·(T - 27 °C)`, alone and together with time, and gives the
correction `q = q_test / (r0 + r_T·(T - 27))`. Drift and temperature look alike
when the room warms up during a run; the report says how tied they are in that
run. It needs the temperature to move by at least 1.5 °C (`--min-temp-span`).

Use short blocks (`--block-min 2`) for this: a room that cools in ten minutes
falls between 10-min blocks and is lost. The `±` of each coefficient assumes
independent blocks, so with short blocks it is optimistic.

The effect is reported, not judged: there is no limit yet, because the value of
a healthy pair is unknown until S0 (SEN1 → SEN3) runs.

Measured on S4 (2026-11-23, SEN3 → SEN4, water, 11:36–16:31, 2-min blocks):
ratio 0.7326 at 27 °C, −0.0030 per °C (−0.41 %/°C); the room cooled twice
(~14:00 to 26 °C, 16:20 to 24.5 °C) and the ratio rose both times. Above
~27.5 °C the ratio barely moves, below it rises faster, so the straight line is
an approximation over 24.5–29 °C. A fit against flow alone had shown an offset of
−5.9 µL/min that was this effect.

With a balance, weigh the covered collector before and after a known interval and
pass `--collected-g <grams> --collect-start <time> --collect-end <time>`: both
sensors are then compared with an absolute volume (0.9 % NaCl, 1.0046 g/mL).

## Acceptance limits (defaults, all adjustable)

| Check | Default | Why |
| --- | --- | --- |
| Volume ratio test/ref | within ±3 % | SEN1–SEN3 agreed within 0.6 % in T01; the datasheet allows up to ±5 % of reading per sensor |
| Change of the ratio over the run | within ±2 % | SEN1 stayed within 1.044–1.069 of C1–C2 over three days in T01 |
| Noise of test | at most 3× the reference | Catches a noisy sensor. (SEN4's ~10× noise in T01 turned out to be the catheter's drops on line C3, not the sensor: in series, without catheter, it was normal.) |
| Reference vs gravimetric | within ±3 % | only with `--collected-g` |

## Self-test

```bash
python3 series_compare.py --selftest
```

Builds two synthetic series runs with a known error (a sensor reading 0.85×
falling to 0.80×, 10× noisier, loggers 23 s apart; and a healthy sensor 1 % high)
and checks that the first FAILS with the right volume ratio and the second PASSES.
A third run has a gain that depends on temperature (0.733 at 27 °C, −0.0030 per
°C, room swinging 24–29 °C) and checks that both numbers are recovered.
