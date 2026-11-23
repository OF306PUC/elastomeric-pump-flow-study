# Sensor diagnostics — two sensors in series

Tools to check one flow sensor against another when both are mounted **in series
on the same line**. They see the same flow, so the ratio of their readings is the
error of the sensor under test, independent of the pump, the catheter and the room.

Written for SEN4 after tanda T01: it integrated 202.7 mL where ~297 mL passed
(the pump emptied), and its reading relative to the other lines fell from 0.89 to
0.65 over ~36 h. The physical setup, conditions and launch lines are in
[`manifests/sensor-series-diag.toml`](../../manifests/sensor-series-diag.toml).

| File | What it does |
| --- | --- |
| `series_compare.py` | Compares the two minute-level CSV files: volume ratio, drift of the ratio, ratio at each flow level, gain and offset, noise, optional gravimetric check, PASS/FAIL. Pure standard library; draws a figure if matplotlib is present. |
| `series_watch.sh` | Live check while a condition runs: copies both CSV files from their hosts and compares the last N minutes. Read-only on the hosts. |

## Setup

```
flow source → sensor upstream → short tube (no catheter) → sensor downstream
            → open outlet, at the height of the source, into a COVERED collector
```

- Each sensor stays on its own cable and host (SEN4 on pi9, SEN1 on pi8, SEN3 on
  pi10). Both loggers run at the same time; clocks are synced by NTP. The tubes
  between the sensors must be short enough to reach — move a host if needed.
- Fill both sensors slowly from below, with no air: an air reading in either
  sensor leaves those minutes out.
- The sensor under test is always `rep_1`, the reference `rep_2`.

## Conditions (in order)

| Condition | What | Source and length |
| --- | --- | --- |
| S0 | Control: SEN3 → SEN1, two sensors that agreed in T01 | syringe pump, 4 rates × 30 min |
| S1 | SEN4 → SEN1, SEN4 as it came out of T01 | syringe pump, 4 rates × 30 min; then elastomeric pump ≥ 24 h |
| S2 | SEN1 → SEN4, order swapped | syringe pump, 4 rates × 30 min |
| S3 | SEN4 → SEN1 after purging SEN4 at high flow | syringe pump; watch live right after the purge |
| S4 | SEN4 → SEN1 after cleaning SEN4 | as S1 |

Suggested rates for a syringe pump: 20, 50, 80 and 150 µL/min (the pumps of the
study delivered 40–90 µL/min). The long elastomeric run is what catches drift: in
T01 the error of SEN4 grew mostly between hours 27 and 34.

A condition stops in one of two ways. With `END_OF_INFUSION=off` it ends at
`HOURS` with `status=COMPLETE` (set `HOURS` to the planned length); a run stopped
by hand ends `INTERRUPTED`, which is fine for a diagnostic but must be said.

## Launch (per host, the operator edits the environment file)

```bash
# pi9 — sensor under test
MANIFEST=sensor-series-diag.toml
CONFIG=S1
EXPERIMENT_REP=rep_1
META="sensor=SEN4 position=upstream source=syringe"
DEVICE_LOGGER_ID=pi9
HOURS=2.5
END_OF_INFUSION=off

# pi8 — reference
MANIFEST=sensor-series-diag.toml
CONFIG=S1
EXPERIMENT_REP=rep_2
META="sensor=SEN1 position=downstream source=syringe"
DEVICE_LOGGER_ID=pi8
HOURS=2.5
END_OF_INFUSION=off
```

Then `./run.sh --check` on both (it rejects a sensor on the wrong cable, a
position that does not match the plan, or a run that already exists), and
`./run.sh` on both within a few seconds.

## During and after

```bash
cd experimental_analysis/diagnostics
./series_watch.sh pi9 pi8 S1 30                     # live: last 30 min
# after the run, bring the files in with ../fetch_data.sh, then:
python3 series_compare.py ../Temp/campana-diag/S1/S1_rep_1.csv \
                          ../Temp/campana-diag/S1/S1_rep_2.csv \
                          --out S1_report.md --plot S1.png
```

`fetch_data.sh` files each run as `Temp/campana-<campaign>/<condition>/`, so these
land in `Temp/campana-diag/S1/`. Exit code: 0 = PASS, 1 = FAIL, 2 = too few valid blocks.

With a balance, weigh the covered collector before and after a known interval and
pass `--collected-g <grams> --collect-start <time> --collect-end <time>`: both
sensors are then compared with an absolute volume (0.9 % NaCl, 1.0046 g/mL).

## Acceptance limits (defaults, all adjustable)

| Check | Default | Why |
| --- | --- | --- |
| Volume ratio test/ref | within ±3 % | SEN1–SEN3 agreed within 0.6 % in T01; the datasheet allows up to ±5 % of reading per sensor |
| Change of the ratio over the run | within ±2 % | SEN1 stayed within 1.044–1.069 of C1–C2 over three days in T01 |
| Noise of test | at most 3× the reference | SEN4 was ~10× noisier than the others in T01 |
| Reference vs gravimetric | within ±3 % | only with `--collected-g` |

## Self-test

```bash
python3 series_compare.py --selftest
```

Builds two synthetic series runs with a known error (a sensor reading 0.85×
falling to 0.80×, 10× noisier, loggers 23 s apart; and a healthy sensor 1 % high)
and checks that the first FAILS with the right volume ratio and the second PASSES.
