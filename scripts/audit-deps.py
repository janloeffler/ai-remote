#!/usr/bin/env python3
"""Audit the pinned dependencies against OSV and PyPI. Standard library only.

Checks every package in the dev lockfiles (a superset of the runtime locks):
  * known vulnerabilities and reported malware (OSV: GHSA, PYSEC, MAL-*)  -> fail
  * yanked releases                                                      -> fail
  * releases younger than --min-age days (supply-chain cooldown)          -> warn
  * releases newer than the pinned one                                    -> info

Exit status: 0 clean (warnings allowed), 1 findings, 2 could not reach OSV/PyPI.
"No findings" means "nothing reported yet", not "proven safe": a malicious release that
has not been reported is invisible to OSV, which is what the cooldown is for.

    python3 scripts/audit-deps.py [--min-age DAYS] [lockfile ...]
"""
import argparse
import datetime as dt
import json
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_LOCKS = ["backend/requirements-dev.txt", "agent/requirements-dev.txt"]
PIN = re.compile(r"^([A-Za-z0-9_.\-]+)==([^\s;\\]+)")


def fetch(url, payload=None):
    req = urllib.request.Request(
        url,
        json.dumps(payload).encode() if payload is not None else None,
        {"Content-Type": "application/json"} if payload is not None else {},
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.load(resp)


def read_pins(paths):
    pins = {}
    for path in paths:
        for line in Path(path).read_text().splitlines():
            m = PIN.match(line)
            if m:
                pins.setdefault(m[1].lower().replace("_", "-"), set()).add(m[2])
    return pins


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("locks", nargs="*", default=[str(ROOT / p) for p in DEFAULT_LOCKS])
    ap.add_argument("--min-age", type=int, default=7, help="warn for releases younger than this many days")
    args = ap.parse_args()

    pins = read_pins(args.locks)
    queries = [(n, v) for n, vs in sorted(pins.items()) for v in sorted(vs)]
    try:
        batch = fetch(
            "https://api.osv.dev/v1/querybatch",
            {"queries": [{"package": {"name": n, "ecosystem": "PyPI"}, "version": v} for n, v in queries]},
        )["results"]
        failures, warnings, infos = [], [], []
        for (name, version), result in zip(queries, batch):
            for vuln in result.get("vulns", []):
                detail = fetch(f"https://api.osv.dev/v1/vulns/{vuln['id']}")
                kind = "MALWARE" if vuln["id"].startswith("MAL-") else "vulnerability"
                failures.append(f"{name}=={version}: {kind} {vuln['id']} — {detail.get('summary', '')}")
            info = fetch(f"https://pypi.org/pypi/{name}/json")
            files = info["releases"].get(version, [])
            if any(f.get("yanked") for f in files):
                failures.append(f"{name}=={version}: release was yanked from PyPI")
            if files:
                uploaded = min(dt.datetime.fromisoformat(f["upload_time_iso_8601"].replace("Z", "+00:00")) for f in files)
                age = (dt.datetime.now(dt.timezone.utc) - uploaded).days
                if age < args.min_age:
                    warnings.append(f"{name}=={version}: released {age} day(s) ago (< {args.min_age})")
            latest = info["info"]["version"]
            if latest != version:
                infos.append(f"{name}: pinned {version}, latest {latest}")
    except (urllib.error.URLError, TimeoutError, KeyError) as exc:
        print(f"audit incomplete — could not query OSV/PyPI: {exc}", file=sys.stderr)
        return 2

    print(f"Checked {len(queries)} pinned package versions from {len(args.locks)} lockfile(s).")
    for label, items in (("FAIL", failures), ("WARN", warnings), ("INFO", infos)):
        for item in items:
            print(f"  [{label}] {item}")
    if not (failures or warnings or infos):
        print("  No findings.")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
