"""K-line storage contract: volume is unadjusted traded shares, never lots."""
from __future__ import annotations

import json
import os
from pathlib import Path
import threading

SCHEMA_VERSION = 2


def source_volume_unit(source: str, code: str) -> str:
    if "新浪" in source and not source.startswith("腾讯"):
        return "shares"
    if source.startswith("腾讯"):
        # Tencent's STAR K-lines and quote feed use shares; other A-share boards use lots.
        return "shares" if code.startswith(("688", "689")) else "lots"
    raise ValueError(f"unknown K-line volume unit for {code}: {source}")


def normalize_payload(payload: dict, code: str) -> dict:
    unit = payload.get("volumeUnit") or source_volume_unit(str(payload.get("source") or ""), code)
    if unit not in {"shares", "lots"}:
        raise ValueError(f"unsupported volume unit: {unit}")
    scale = 100.0 if unit == "lots" else 1.0
    rows = []
    for row in payload["rows"]:
        volume = float(row["volume"])
        if volume < 0:
            raise ValueError(f"negative volume: {code} {row['date']}")
        rows.append({**row, "volume": volume * scale})
    return {**payload, "schemaVersion": SCHEMA_VERSION, "volumeUnit": "shares",
            "sourceVolumeUnit": payload.get("sourceVolumeUnit") or unit, "rows": rows}


def write_payload(path: Path, payload: dict) -> None:
    if payload.get("volumeUnit") != "shares":
        raise ValueError("K-line cache writes require volumeUnit=shares")
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(f".{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        temp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


def load_payload(path: Path, migrate: bool = False) -> dict:
    original = json.loads(path.read_text(encoding="utf-8"))
    normalized = normalize_payload(original, path.stem)
    if migrate and (original.get("volumeUnit") != "shares" or original.get("schemaVersion") != SCHEMA_VERSION):
        write_payload(path, normalized)
    return normalized
