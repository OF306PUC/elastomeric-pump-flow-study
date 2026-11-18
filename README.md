# Elastomeric pump flow study

In-vitro study of the delivered flow of Baxter elastomeric infusion pumps
(Infusor LV5) under different catheter configurations, measured with Sensirion
SLF3S-0600F liquid flow sensors.

This repository holds the **study**: its experiment definitions, the analysis,
the ambient temperature logger and the lab notes. The sensor logger itself is a
separate, general-purpose project included as a git submodule.

| Path | What it is |
|---|---|
| `driver/` | **Submodule** — [`slf3s-0600f-python-driver`](https://github.com/OF306PUC/slf3s-0600f-python-driver), pinned to the exact commit the study runs. Every data file also records it (`driver_commit` header key). |
| `manifests/` | Experiment manifests (TOML): conditions, replicates, required fields and the run plan. The logger refuses a run that does not match. |
| `run.sh` | Launches one acquisition run through `driver/run.sh` with this study's manifests. |
| `experimental_analysis/` | Analysis: per-run curves, condition means, `summary.md`. |
| `ambient-temp-meas/` | SHT31 ambient temperature/humidity logger (C++), one file per tanda. |
| `experiment_notes/` | Lab notes (text). |
| `camera/` | Time-lapse prototype, not integrated. |

## Clone

```
git clone --recurse-submodules https://github.com/OF306PUC/elastomeric-pump-flow-study.git
cd elastomeric-pump-flow-study
git config submodule.recurse true     # so `git pull` also updates driver/
```

## Run an acquisition (on a host)

Create the environment file in this directory from `driver/.env.template`
(`MANIFEST=pump-flow-main.toml`, condition, replicate, lot, `META="sensor=… tanda=…"`),
then:

```
./run.sh
```

`data/` and `logs/` are created here. See `driver/slf3s/CSV_FORMAT.md` for the
file format and `driver/slf3s/check_readout.py` to verify a finished run.

## Analyse

```
cd experimental_analysis
python3 main.py --campaign main
```

Data are read from `experimental_analysis/Temp/campana-main/<condition>/` and the
per-sensor zero-flow offsets from `experimental_analysis/Temp/offset/` — both
local only, never committed.

## Updating the driver

A driver change takes two steps: commit and push it in the driver repository,
then here move the submodule to that commit and commit the pointer:

```
cd driver && git fetch && git checkout <commit> && cd ..
git add driver && git commit -m "chore: driver <commit>"
```

Never commit a pointer to a driver commit that is not pushed: every clone would
fail to check it out.
