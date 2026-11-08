#!/usr/bin/env python3
"""Audit/migrate local daily and long K-line caches to shares, backing up every edit."""
from __future__ import annotations

import argparse
from collections import Counter
import datetime as dt
import hashlib
import json
from pathlib import Path
import zipfile

from kline_units import normalize_payload, write_payload

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--backup", type=Path, help="New .zip file under a local ignored directory")
    args = parser.parse_args()
    if args.report.exists():
        raise SystemExit("report already exists; choose a new path")
    if args.apply and (not args.backup or args.backup.exists()):
        raise SystemExit("--apply requires a new --backup path")
    paths = sorted((ROOT / "output/cache/kline").glob("*/*.json")) + sorted((ROOT / "output/cache/kline_long").glob("*.json"))
    # Validate every source/schema before any mutation.
    failures, sources, changed, hashes = [], Counter(), [], {}
    for path in paths:
        try:
            raw = path.read_bytes()
            payload = json.loads(raw)
            normalized = normalize_payload(payload, path.stem)
            sources[payload["source"]] += 1
            if payload.get("volumeUnit") != "shares" or payload.get("schemaVersion") != 2:
                changed.append((path, normalized["sourceVolumeUnit"]))
                hashes[str(path.relative_to(ROOT))] = hashlib.sha256(raw).hexdigest()
        except Exception as exc:
            failures.append({"path": str(path.relative_to(ROOT)), "error": str(exc)})
    report = {"createdAt": dt.datetime.now().astimezone().isoformat(), "scanned": len(paths),
              "sourceCounts": dict(sources), "needsMetadataOrConversion": len(changed),
              "lotsToShares": sum(unit == "lots" for _, unit in changed), "failures": failures,
              "applied": False, "backup": str(args.backup) if args.backup else None}
    if args.apply and not failures:
        args.backup.parent.mkdir(parents=True, exist_ok=True)
        # Complete the original-file backup before changing any cache file.
        with zipfile.ZipFile(args.backup, "x", compression=zipfile.ZIP_DEFLATED, compresslevel=1) as backup:
            for path, _ in changed:
                if hashlib.sha256(path.read_bytes()).hexdigest() != hashes[str(path.relative_to(ROOT))]:
                    raise RuntimeError(f"cache changed during validation: {path}")
                backup.write(path, str(path.relative_to(ROOT)))
            backup.writestr("original-sha256.json", json.dumps(hashes, ensure_ascii=False))
        for path, _ in changed:
            if hashlib.sha256(path.read_bytes()).hexdigest() != hashes[str(path.relative_to(ROOT))]:
                raise RuntimeError(f"cache changed after backup: {path}")
            payload = json.loads(path.read_text())
            write_payload(path, normalize_payload(payload, path.stem))
        report["applied"] = True
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))
    if failures:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
