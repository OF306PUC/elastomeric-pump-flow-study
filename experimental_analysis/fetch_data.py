#!/usr/bin/env python3
"""
fetch_data.py — bring every run from the acquisition hosts into Temp/, filed by
what each file says it is.

    ./fetch_data.sh                 # all hosts
    ./fetch_data.sh --hosts pi1 pi8 # some hosts
    ./fetch_data.sh --dry-run       # download, but only report where files would go
    ./fetch_data.sh --skip-sync     # re-file what is already downloaded

1. rsync each flow host's data/ (*.csv, *.bin) into Temp/_incoming/<host>/ — a
   mirror, so a large .bin is only transferred once.
2. File each run by its CSV HEADER, never by its file name:
       Temp/campana-<campaign>/<tanda>/<stem>.{csv,bin}          (header has tanda=)
       Temp/campana-<campaign>/<configuration>/<stem>.{csv,bin}  (no tanda: campaign 1,
                                                                   diagnostics)
   A tanda is the unit of the main campaign — four runs measured together — so
   its runs live together, e.g. Temp/campana-main/T01/C0_rep_1.csv … C3_rep_1.csv.
   A run launched without a manifest (campaign "none", e.g. an offset test)
   goes to Temp/sin-campana/<configuration>/.
3. A CSV without its closing "# END" line is still being written: left in
   _incoming/ and reported, never filed.
4. A destination that already holds a DIFFERENT file is never overwritten: the
   old one is moved to Temp/_superseded/<date>/... first.
5. The ambient temperature logs of pi12 are mirrored into Temp/ambient/, and a
   CLOSED ambient log of a tanda (ambient_<sensor>_<tanda>.csv, ending in
   "# END") is also copied into that tanda's folder, so the folder holds
   everything measured during it.

Standard library + rsync/ssh. Hosts are the aliases of ~/.ssh/config.
"""
import argparse
import datetime
import filecmp
import pathlib
import re
import shutil
import subprocess
import sys

HERE = pathlib.Path(__file__).resolve().parent
TEMP = HERE / "Temp"
INCOMING = TEMP / "_incoming"
SUPERSEDED = TEMP / "_superseded"

FLOW_HOSTS = ["pi1", "pi8", "pi9", "pi10"]
AMBIENT_HOST = "pi12"
# Tried in order, relative to the remote home: the study clone first, the old
# driver clone while the hosts are being migrated.
FLOW_DATA_DIRS = ["elastomeric-pump-flow-study/data",
                  "Desktop/slf3s-0600f-python-driver/data"]
AMBIENT_DATA_DIRS = ["elastomeric-pump-flow-study/ambient-temp-meas/data",
                     "ambient-temp-meas/data"]


def remote_dir(host: str, candidates: list):
    """The first candidate directory that exists on `host`, or None."""
    for d in candidates:
        r = subprocess.run(["ssh", "-o", "ConnectTimeout=8", host, f"test -d {d}"],
                           capture_output=True)
        if r.returncode == 0:
            return d
        if r.returncode == 255:          # ssh itself failed: host unreachable
            print(f"  [{host}] unreachable: {r.stderr.decode().strip()}")
            return None
    return None


def rsync(host: str, src: str, dest: pathlib.Path, patterns: list) -> bool:
    dest.mkdir(parents=True, exist_ok=True)
    cmd = ["rsync", "-a", "--include=*/"]
    cmd += [f"--include={p}" for p in patterns] + ["--exclude=*",
            f"{host}:{src}/", f"{dest}/"]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        print(f"  [{host}] rsync failed: {r.stderr.strip()}")
        return False
    return True


def read_header(csv_path: pathlib.Path) -> dict:
    """The '# key : value' metadata block, plus whether the closing footer exists."""
    meta, last = {}, ""
    with csv_path.open(errors="replace") as f:
        for line in f:
            s = line.strip()
            if s:
                last = s
            if s.startswith("#") and ":" in s and not s.startswith("# END"):
                k, _, v = s[1:].partition(":")
                meta.setdefault(k.strip(), v.strip())
    meta["_closed"] = last.startswith("# END")
    meta["_footer"] = last if meta["_closed"] else ""
    return meta


TANDA = re.compile(r"^T[0-9]{2,}[a-z]?$")


def destination(meta: dict) -> pathlib.Path:
    campaign = meta.get("campaign")
    cond = meta.get("configuration") or "SIN_CONDICION"
    if campaign is None:
        return None                       # legacy file without a campaign: not ours to file
    folder = "sin-campana" if campaign == "none" else f"campana-{campaign}"
    tanda = meta.get("tanda", "")
    if campaign != "none" and TANDA.match(tanda):
        return TEMP / folder / tanda      # the four runs of a tanda live together
    return TEMP / folder / cond


def file_ambient(dry: bool) -> None:
    """Copy each CLOSED ambient log of a tanda into its tanda folder."""
    today = datetime.date.today().isoformat()
    rows, still_open = [], []
    for csv in sorted((TEMP / "ambient").glob("*/ambient_*_T*.csv")):
        tanda = csv.stem.rsplit("_", 1)[-1]
        if not TANDA.match(tanda):
            continue
        last = ""
        with csv.open(errors="replace") as f:
            for line in f:
                if line.strip():
                    last = line.strip()
        if not last.startswith("# END"):
            still_open.append(csv.name)
            continue
        dest_dir = TEMP / f"campana-{csv.parent.name}" / tanda
        rows.append((csv.name, dest_dir, place(csv, dest_dir, today, dry)))
    if rows:
        verb = "would be" if dry else "were"
        print(f"\nAmbient logs ({len(rows)}) {verb} copied into their tanda:")
        for name, d, status in rows:
            print(f"  {status:<9} {name:<28} → {d.relative_to(TEMP)}/")
    if still_open:
        print("\nAmbient logs still being written (no closing line) — not copied: "
              + ", ".join(still_open))


def place(src: pathlib.Path, dest_dir: pathlib.Path, today: str, dry: bool) -> str:
    """Copy src into dest_dir. Returns 'new', 'unchanged' or 'replaced'."""
    dest = dest_dir / src.name
    if dest.exists():
        if filecmp.cmp(src, dest, shallow=False):
            return "unchanged"
        if not dry:
            old = SUPERSEDED / today / dest_dir.relative_to(TEMP) / src.name
            old.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(dest), str(old))
            shutil.copy2(src, dest)
        return "replaced"
    if not dry:
        dest_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
    return "new"


def file_runs(hosts: list, dry: bool) -> int:
    today = datetime.date.today().isoformat()
    rows, open_runs, problems = [], [], []

    # Pass 1: classify everything, so a run found on two hosts is caught before
    # either copy is filed.
    found = {}                            # (dest_dir, stem) -> [(host, csv, meta)]
    for host in hosts:
        for csv in sorted((INCOMING / host).rglob("*.csv")):
            meta = read_header(csv)
            if not meta["_closed"]:
                open_runs.append((host, csv.name))
                continue
            dest_dir = destination(meta)
            if dest_dir is None:
                problems.append(f"{host}/{csv.name}: no 'campaign' in its header "
                                f"(written before manifests) — not filed")
                continue
            found.setdefault((dest_dir, csv.stem), []).append((host, csv, meta))

    # Pass 2: file the unambiguous ones.
    for (dest_dir, stem), copies in sorted(found.items(), key=lambda kv: str(kv[0])):
        if len(copies) > 1:
            problems.append(f"{stem}: found on {', '.join(c[0] for c in copies)} — "
                            f"no copy filed; resolve by hand")
            continue
        host, csv, meta = copies[0]
        bin_ = csv.with_suffix(".bin")
        if not bin_.exists():
            problems.append(f"{host}/{csv.name}: no .bin next to it")
        status = place(csv, dest_dir, today, dry)
        if bin_.exists():
            bin_status = place(bin_, dest_dir, today, dry)
            if bin_status != "unchanged":
                status = bin_status if status == "unchanged" else status
        rows.append((host, stem, dest_dir, status,
                     _footer_field(meta["_footer"], "status"),
                     _footer_field(meta["_footer"], "coverage")))

    verb = "would be" if dry else "were"
    print(f"\nRuns ({len(rows)}) — files {verb} filed under Temp/:")
    for host, stem, d, status, st, cov in rows:
        print(f"  {status:<9} {stem:<18} {host:<5} → {d.relative_to(TEMP)}/  "
              f"[{st or '?'}, coverage {cov or '—'}]")
    if open_runs:
        print("\nStill being written (no closing line yet) — not filed:")
        for host, name in open_runs:
            print(f"  {host}: {name}")
    if problems:
        print("\nNeeds attention:")
        for p in problems:
            print(f"  - {p}")
    replaced = [r for r in rows if r[3] == "replaced"]
    if replaced and not dry:
        print(f"\nThe previous version of {len(replaced)} run(s) was kept in "
              f"Temp/_superseded/{today}/.")
    return 1 if problems else 0


def _footer_field(footer: str, key: str):
    for tok in footer.split():
        if tok.startswith(key + "="):
            return tok.split("=", 1)[1]
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--hosts", nargs="+", default=FLOW_HOSTS)
    ap.add_argument("--dry-run", action="store_true",
                    help="download, but only report where files would go")
    ap.add_argument("--skip-sync", action="store_true",
                    help="do not contact the hosts; file what is already in _incoming/")
    ap.add_argument("--no-ambient", action="store_true")
    args = ap.parse_args()

    if not args.skip_sync:
        for host in args.hosts:
            d = remote_dir(host, FLOW_DATA_DIRS)
            if d is None:
                print(f"  [{host}] no data directory found — skipped")
                continue
            print(f"  [{host}] {d}/ → Temp/_incoming/{host}/")
            rsync(host, d, INCOMING / host, ["*.csv", "*.bin"])
        if not args.no_ambient:
            d = remote_dir(AMBIENT_HOST, AMBIENT_DATA_DIRS)
            if d is None:
                print(f"  [{AMBIENT_HOST}] no ambient data directory found — skipped")
            else:
                print(f"  [{AMBIENT_HOST}] {d}/ → Temp/ambient/")
                if not args.dry_run:
                    rsync(AMBIENT_HOST, d, TEMP / "ambient", ["*.csv"])

    rc = file_runs([h for h in args.hosts if (INCOMING / h).is_dir()], args.dry_run)
    if not args.no_ambient:
        file_ambient(args.dry_run)
    return rc


if __name__ == "__main__":
    sys.exit(main())
