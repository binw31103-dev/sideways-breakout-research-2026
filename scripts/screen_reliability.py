"""Shared scan health and observation-risk checks; no technical thresholds here."""
from __future__ import annotations

import concurrent.futures
import datetime as dt
import hashlib
from pathlib import Path
from typing import Any, Callable


def scan_candidates(codes: list[str], analyze: Callable, workers: int = 3) -> tuple[list[dict], dict]:
    """Keep missing data separate from a valid technical non-match."""
    hits, failures = [], []
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(analyze, code): code for code in codes}
        for future in concurrent.futures.as_completed(futures):
            try:
                hit = future.result()
                if hit is not None:
                    hits.append(hit)
            except Exception as exc:
                failures.append({"code": futures[future], "error": str(exc)})
    failures.sort(key=lambda row: row["code"])
    status = "failed" if not codes or len(failures) > len(codes) * 0.3 else "partial" if failures else "success"
    hits.sort(key=lambda item: item["code"])
    return hits, {"status": status, "expected": len(codes), "succeeded": len(codes) - len(failures),
                  "failed": len(failures), "technicalHits": len(hits), "failures": failures}


def observation_risk(candidate: dict, financial: dict) -> dict:
    """Use exactly the financial/announcement gate already used by v13 and v26."""
    from screen_red_zone_5 import enrich_financial_risk

    candidate.setdefault("profit", float(financial.get("PARENT_NETPROFIT") or 0))
    candidate.setdefault("profitPeriod", str(financial.get("QDATE") or financial.get("REPORTDATE") or ""))
    candidate.setdefault("conditionChecks", {})
    return enrich_financial_risk(candidate)


def filter_observation_risks(hits: list[dict], financials: dict, diagnostics: dict) -> list[dict]:
    accepted, rejected, failures = [], [], []
    for hit in hits:
        try:
            item = observation_risk(hit, financials[hit["code"]])
            if item["financialState"] == "不通过" or item["riskState"] == "不通过":
                rejected.append({"code": item["code"], "financialState": item["financialState"],
                                 "riskState": item["riskState"]})
            else:
                accepted.append(item)
        except Exception as exc:
            failures.append({"code": hit["code"], "error": f"financial-risk: {exc}"})
    diagnostics.update(riskRejected=rejected, riskFailures=failures, selected=len(accepted))
    if failures and diagnostics["status"] == "success":
        diagnostics["status"] = "partial"
    # An incomplete market scan must not create apparently authoritative observations.
    if diagnostics["status"] == "failed":
        diagnostics["selected"] = 0
        return []
    return accepted


def scan_message(payload: dict | None) -> str:
    diagnostics = (payload or {}).get("scan") or {}
    status = diagnostics.get("status")
    if status in {"failed", "partial"}:
        label = "扫描失败" if status == "failed" else "扫描未完成"
        return (f"{label}：成功{diagnostics.get('succeeded', 0)}/{diagnostics.get('expected', 0)}，"
                f"取数失败{diagnostics.get('failed', 0)}，风险待核{len(diagnostics.get('riskFailures', []))}；"
                "本次数量不代表全市场完整结果。"
                + (f" 原因：{diagnostics['error']}" if diagnostics.get("error") else ""))
    return ""


def primary_scan_health(expected: int, failures: list[dict]) -> dict:
    failed = len({row["code"] for row in failures})
    return {"status": "partial" if failed else "success", "expected": expected,
            "succeeded": max(0, expected - failed), "failed": failed}


def rule_provenance(root: Path) -> dict[str, Any]:
    paths = ["screen_v13_right_start.py", "screen_v26_unified.py", "screen_red_zone_5.py",
             "bottom_base_context.py", "screen_reliability.py", "research/underwater_cross.py",
             "research/zero_axis_live.py"]
    return {"recordedAt": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
            "ruleHashes": {name: hashlib.sha256((root / "scripts" / name).read_bytes()).hexdigest()
                           for name in paths}}


def signal_overlap(models: dict[str, dict]) -> dict:
    by_code: dict[str, list[str]] = {}
    for model, payload in models.items():
        for item in payload.get("selected", []):
            by_code.setdefault(item["code"], []).append(model)
    return {"signalCount": sum(len(p.get("selected", [])) for p in models.values()),
            "uniqueStockCount": len(by_code),
            "overlaps": {code: sorted(set(keys)) for code, keys in sorted(by_code.items()) if len(set(keys)) > 1}}
