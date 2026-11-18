# ambient-temp-meas — SHT31 ambient logger

C++17 driver for the Sensirion SHT31 (Linux i2c-dev) and a CSV logger whose
output joins on time with the SLF3S flow logs.

## Build

```
cmake -B build && cmake --build build
./build/sht31_logger --dry-run --sensor-id TEST --window-s 2 --interval-ms 200   # no hardware
./build/sht31_logger --sensor-id TAMB1 --tanda T03 --experiment pump-flow --campaign main \
    --device-id pi12 --location junto_a_restrictores
```

The user needs access to `/dev/i2c-1` (`sudo usermod -aG i2c $USER`, then log in again).
## One record per tanda

The study records ambient temperature once per **tanda** (the block of four
simultaneous flow runs): one record characterises all four runs. Run the logger
through the systemd template unit, whose instance name is the tanda:

```
sudo cp sht31-logger@.service /etc/systemd/system/ && sudo systemctl daemon-reload   # once
sudo systemctl enable --now sht31-logger@T03    # when preparing T03, before the first pump
sudo systemctl disable --now sht31-logger@T03   # after the last run of T03 has ended
```

`--tanda` must match `^T[0-9]{2}$`, the manifest's pattern. With a tanda the
logger writes a single file, `data/main/ambient_TAMB1_T03.csv`, however many
days the tanda lasts; a crash or power cut appends to it under a new header
block (`appended : yes`). Without `--tanda` it logs freely, one file per UTC day.

## Output

`ambient_<sensor_id>_<tanda>.csv` (or `_<YYYYMMDD>` without a tanda), one row per
window (default: one reading per second, one row per minute). Each file starts
with a `# key : value` metadata block — `experiment`, `campaign`, `tanda`,
sensor, host, clock synchronisation, and any `--meta key=value`. Every row is
fsync'ed. The file ends with `# END rows=N status=STOPPED|COMPLETE|ROTATED`:
STOPPED = stopped by a signal (the normal end of a tanda), COMPLETE =
`--duration-h` reached, ROTATED = day change in free logging. No footer = the
process died (power cut); rows written until then are on disk.

| Column | Meaning |
|---|---|
| `window_start` | Window start, Unix epoch seconds (UTC). Windows are aligned to multiples of `window_s` since the epoch, so every host shares the same boundaries |
| `UTC_Time` | Mean timestamp of the window's readings, Unix epoch seconds — same time base as the flow CSV's `UTC_Time` |
| `Temp_degC`, `Temp_std_degC`, `Temp_min_degC`, `Temp_max_degC` | Temperature mean, sample SD, min, max |
| `RH_pct`, `RH_std_pct`, `RH_min_pct`, `RH_max_pct` | Relative humidity, same statistics |
| `N_samples` | Successful readings in the window |
| `N_errors` | Failed readings (CRC or bus error). A row with `N_samples = 0` has `nan` values |

A window with no attempts (logger not running) produces no row.

## Correlating with a flow run

The flow CSV's header carries `tanda : T03`; the ambient file of that run is
`ambient_TAMB1_T03.csv`. Both use Unix epoch seconds, so after picking the file
by tanda a nearest-time join is enough:

```python
import pandas as pd
amb = pd.read_csv("data/main/ambient_TAMB1_T03.csv", comment="#")
flow = pd.read_csv("C1_rep_1.csv", comment="#")      # header: tanda : T03
both = pd.merge_asof(flow.sort_values("UTC_Time"), amb.sort_values("UTC_Time"),
                     on="UTC_Time", direction="nearest", tolerance=60, suffixes=("", "_amb"))
```

Check coverage: the ambient file must span the whole run (first and last
`UTC_Time` of the flow CSV inside the ambient range). A run outside its
tanda's ambient record means the logger was started late or stopped early.

**Requirement:** every host's clock must be NTP-synchronised. The header
records `clock_synchronized`; with `no`, timestamps from different hosts cannot
be trusted to line up.
