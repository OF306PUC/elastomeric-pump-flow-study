# Elastomeric pump flow study

In-vitro study of the delivered flow of Baxter elastomeric infusion pumps
(Infusor LV5) with different catheter configurations, measured with Sensirion
SLF3S-0600F liquid flow sensors.

This repository is the **study**: experiment definition, analysis, ambient
temperature logger and lab notes. The sensor logger is a separate project,
included as the submodule `driver/`.

---

## How it runs — the short version

| What | Where | How it runs | Started with | Stopped by |
|---|---|---|---|---|
| **Flow logger** (one per sensor) | pi1, pi8, pi9, pi10 | **Docker container** `slf3s-logger`, detached | `./run.sh` | itself: flow stops, or `HOURS` reached |
| **Ambient temperature** (SHT31) | pi12 | **systemd service** `sht31-logger@<tanda>` | `systemctl enable --now` | `systemctl disable --now` |

| Host | Sensor |
|---|---|
| pi1 | SEN2 |
| pi8 | SEN1 |
| pi9 | SEN4 |
| pi10 | SEN3 |
| pi12 | SHT31 (ambient) |

> ⚠ **The flow logger does not survive a reboot or power cut.** The container is
> not restarted automatically — on purpose: relaunching under the same name
> would overwrite the run's files. After a power cut the run is incomplete and is
> replaced by a new replicate (methodology § 4.4). The ambient logger, being a
> systemd service, does come back by itself and appends to the same file.

---

## One tanda, step by step

A **tanda** = 4 simultaneous runs, one per sensor. Which condition, replicate and
pump lot goes on which sensor is fixed by the plan in
[`manifests/pump-flow-main.toml`](manifests/pump-flow-main.toml) — the logger
refuses anything else.

### 0. Before the first tanda (once per host)

```bash
git clone --recurse-submodules https://github.com/OF306PUC/elastomeric-pump-flow-study.git
cd elastomeric-pump-flow-study
git config submodule.recurse true      # so `git pull` also updates driver/
```

Each flow host needs Docker and the sensor cable plugged in
(`ls /dev/serial/by-id/` shows one `usb-Sensirion…` entry).

### 1. Update every host

```bash
cd ~/elastomeric-pump-flow-study && git pull
```

### 2. Start the ambient temperature log (pi12) — before the first pump

```bash
sudo systemctl enable --now sht31-logger@T01
journalctl -u sht31-logger@T01 -n 5        # must say "writing …/ambient_TAMB1_T01.csv"
```

Installing the service is a one-time step: see
[`ambient-temp-meas/README.md`](ambient-temp-meas/README.md).

### 3. Prepare and launch each flow run (pi1, pi8, pi9, pi10)

In the study directory of each host, edit the environment file `.env` (copy
`driver/.env.template` the first time). Only these lines change between runs:

```bash
MANIFEST=pump-flow-main.toml
CONFIG=C1                       # condition, from the plan
EXPERIMENT_REP=rep_1            # replicate, from the plan
PUMP_LOT=LOTC                   # lot of the pump in the box, from the plan
META="sensor=SEN2 tanda=T01"    # this host's sensor + the tanda
DEVICE_LOGGER_ID=pi1
HOURS=96
END_OF_INFUSION=on
```

Then fill and purge the pump, connect the line, and:

```bash
./run.sh
```

`run.sh` first checks the run against the plan. If anything does not match
(wrong lot, sensor, tanda…) it prints why and **launches nothing**. If it is
accepted it starts the container in the background — you can close the SSH
session. Confirm it is reading:

```bash
docker logs slf3s-logger | grep -E "single reader|Acquisition started"
```

### 4. While it runs

```bash
docker ps                          # slf3s-logger must be "Up"
tail -2 data/C1_rep_1.csv          # one new row per minute
```

To stop a run by hand: `docker stop slf3s-logger` (footer `status=INTERRUPTED`).

### 5. When the run ends

The logger stops by itself once the flow has stayed near zero for about 20
minutes (end of infusion), or after `HOURS`. Then check it:

```bash
tail -1 data/C1_rep_1.csv                               # status=COMPLETE, coverage ≥ 0.99
python3 driver/slf3s/check_readout.py data/C1_rep_1.bin # must say PASS
```

A run that fails the check is replaced by a new replicate (methodology § 4.4).

### 6. Stop the ambient log (pi12) — after the LAST run of the tanda

```bash
sudo systemctl disable --now sht31-logger@T01
```

### 7. Bring the data to the workstation and analyse

```bash
# on the workstation, from this repository
rsync -a pi1:elastomeric-pump-flow-study/data/C1_rep_1.{csv,bin} experimental_analysis/Temp/campana-main/C1/
# … one line per run of the tanda

cd experimental_analysis
python3 main.py --campaign main          # → results/campana-main/summary.md
```

The per-sensor zero offsets used by the analysis are in
`experimental_analysis/Temp/offset/offset_por_sensor.csv`. Data and results are
local only — never committed.

---

## Repository layout

| Path | What it is |
|---|---|
| `driver/` | Submodule [`slf3s-0600f-python-driver`](https://github.com/OF306PUC/slf3s-0600f-python-driver), pinned to the commit the study runs. Every data file also records it (`driver_commit`). File format: `driver/slf3s/CSV_FORMAT.md`. |
| `manifests/` | The campaign definition: conditions, replicates, required fields, run plan. |
| `run.sh` | Launches one flow run through `driver/run.sh` with this study's manifests. |
| `experimental_analysis/` | Analysis: per-run curves, condition means, `summary.md`. |
| `ambient-temp-meas/` | SHT31 ambient logger (C++) and its systemd unit. |
| `experiment_notes/` | Lab notes (text). |
| `camera/` | Time-lapse prototype, not integrated. |

## Updating the driver

A driver change takes two commits: one in the driver repository (pushed), then
one here moving the submodule:

```bash
cd driver && git fetch && git checkout <commit> && cd ..
git add driver && git commit -m "chore: driver <commit>"
git push
```

Never push a pointer to a driver commit that is not pushed itself: every clone
would fail. On the hosts, a plain `git pull` then brings the new driver, and the
next `./run.sh` rebuilds the image.
