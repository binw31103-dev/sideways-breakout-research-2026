#!/usr/bin/env python3
"""Offline reliability audit. Writes new artifacts; live ledger changes require --apply-tracking.

No downloads, Feishu calls, strategy tuning, or overwriting historical reports.
"""
from __future__ import annotations

import argparse
import ast
from collections import Counter, defaultdict
import copy
import datetime as dt
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import sys
import tempfile
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "scripts"), str(ROOT / "scripts/research")]
import bottom_base_context as context
import run_daily_screen_and_notify as daily
from screen_reliability import rule_provenance
from v26_rule_comparison import compare_rules


def load(path):
    return json.loads(path.read_text(encoding="utf-8"))


def event_inventory(tracking: dict) -> Counter:
    events = Counter()
    for record in tracking["records"].values():
        for event in record.get("history", []):
            events[(record["model"], record["code"], event["date"], event["close"], event.get("state"))] += 1
    return events


def technical_invariants(baseline: Path) -> dict:
    checks = {
        "screen_v26_unified.py": ["calculate_metrics", "strict_condition_checks", "right_edge_condition_checks", "startup_confirmation"],
        "screen_v13_right_start.py": ["find_recent_breakout", "right_start_candidate"],
        "screen_red_zone_5.py": ["MARKET_POOL_DEFAULTS", "macd_metrics", "detect_secondary_confirmation"],
        "bottom_base_context.py": ["TRACK_DAYS", "TRACK_DAYS_V13", "STOP_BUFFER", "LAUNCH_GAIN", "literature_score"],
        "research/zero_axis_live.py": ["NEAR_ZERO", "MAX_RISE_LOW", "MAX_RANGE20", "MAX_CHANGE5", "MAX_VOLUME_RATIO"],
        "research/underwater_cross.py": ["macd_lines", "classify"],
        "preopen_check.py": ["SKIP_GAP", "CAUTION_GAP", "verdict"],
    }
    def selected_nodes(path, names):
        result = {}
        for node in ast.parse(path.read_text()).body:
            if isinstance(node, (ast.FunctionDef, ast.ClassDef)) and node.name in names:
                result[node.name] = ast.dump(node, include_attributes=False)
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id in names:
                        result[target.id] = ast.dump(node, include_attributes=False)
        return result
    result = {}
    for relative, names in checks.items():
        old = selected_nodes(baseline / "scripts" / relative, names)
        new = selected_nodes(ROOT / "scripts" / relative, names)
        for name in names:
            result[f"{relative}:{name}"] = name in old and old[name] == new.get(name)
    # The existing ranking expressions and diversification limits must stay the same.
    for relative in ["screen_v26_unified.py", "screen_v13_right_start.py"]:
        def sort_keys(path):
            tree = ast.parse(path.read_text())
            return [ast.dump(n, include_attributes=False) for n in ast.walk(tree)
                    if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "sort"]
        result[f"{relative}:sort_expressions"] = sort_keys(baseline / "scripts" / relative) == sort_keys(ROOT / "scripts" / relative)
    return result


def replay_tracking(original: dict, cache: Path, as_of: str) -> tuple[dict, dict]:
    """Replay actual historical selection events, using cached K-lines only."""
    series = {}
    for record in original["records"].values():
        if record.get("model") not in {"v13", "v26", "ucross", "zaxis"}:
            continue
        code = record["code"]
        if code not in series:
            series[code] = [r for r in load(cache / f"{code}.json")["rows"] if r["date"] <= as_of]
    events = defaultdict(list)
    for record in original["records"].values():
        if record.get("model") in {"v13", "v26", "ucross", "zaxis"}:
            for event in record.get("history", []):
                if event["date"] > as_of:
                    raise RuntimeError("audit date precedes an existing selection")
                events[event["date"]].append((record, event))
    def cached(code):
        return series[code], "offline cached K-line"
    unchanged = {k: copy.deepcopy(r) for k, r in original["records"].items()
                 if r.get("model") not in {"v13", "v26", "ucross", "zaxis"}}
    with tempfile.TemporaryDirectory() as temporary, \
            mock.patch.object(daily, "TRACKING_PATH", Path(temporary) / "tracking.json"), \
            mock.patch.object(context, "fetch_kline", side_effect=cached), \
            mock.patch.object(context, "extend_to", side_effect=lambda code, history, day: history):
        daily.save_json(daily.TRACKING_PATH, {"version": 2, "records": unchanged})
        for date in sorted(events):
            by_model = defaultdict(list)
            for record, event in events[date]:
                by_model[record["model"]].append({"code": record["code"], "name": record["name"],
                    "close": event["close"], "state": event.get("state"), "featured": record.get("featured", False)})
            daily.update_tracking(list((model, {"selected": items}) for model, items in by_model.items()), date)
        result = load(daily.TRACKING_PATH)
        for record in result["records"].values():
            if record.get("model") in {"v13", "v26", "ucross", "zaxis"}:
                record["followUp"] = daily.record_follow_up(record, as_of)
    pending = [r["episodeId"] for r in result["records"].values()
               if r.get("followUp", {}).get("status") == "数据待复核"]
    same_events = event_inventory(original) == event_inventory(result)
    examples = []
    for key, old in original["records"].items():
        new = result["records"].get(key)
        if new and old["firstSelectedDate"] != new["firstSelectedDate"]:
            examples.append({"key": key, "name": old["name"], "oldFirst": old["firstSelectedDate"],
                             "newFirst": new["firstSelectedDate"], "oldStatus": old.get("followUp", {}).get("status"),
                             "newStatus": new.get("followUp", {}).get("status")})
    return result, {"originalRecords": len(original["records"]), "newEpisodes": len(result["records"]),
                    "eventCount": sum(event_inventory(original).values()), "allEventsPreserved": same_events,
                    "pendingEpisodes": pending, "reselectedStocks": examples}


def rule_snapshots(cache: Path, dates: list[str]) -> dict:
    variants = ["current", "no_daily_rsi_lower", "daily_rsi_upper_50", "no_weekly_rsi", "oldSimplifiedPool"]
    by_date = {date: {"evaluated": 0, "counts": dict.fromkeys(variants, 0), "extraSimplifiedExamples": []} for date in dates}
    for path in sorted(cache.glob("*.json")):
        bars = load(path)["rows"]
        if any(r["low"] <= 0 or r["close"] <= 0 for r in bars):
            continue
        for date in dates:
            window = [r for r in bars if r["date"] <= date]
            if len(window) < 260 or window[-1]["date"] != date:
                continue
            # Every comparison retains dist-low<=8%; this prefilter cannot remove a true hit.
            if window[-1]["close"] / min(r["low"] for r in window[-60:]) > 1.08:
                continue
            result = compare_rules(window)
            if result is None:
                continue
            stat = by_date[date]
            stat["evaluated"] += 1
            for variant in variants:
                stat["counts"][variant] += int(result[variant])
            if result["oldSimplifiedPool"] and not result["current"] and len(stat["extraSimplifiedExamples"]) < 3:
                stat["extraSimplifiedExamples"].append({"code": path.stem,
                    "failedChecks": [k for k, value in result["checks"].items() if not value]})
    return {"scope": "technical strict pool only; no current-cap/fundamental filters, no return inference",
            "dates": by_date}


def golden_cross_parity(baseline: Path, cache: Path, as_of: str, out: Path) -> dict:
    """Compare actual candidate sets with network calls replaced by identical local inputs."""
    import screen_latest_30 as data
    import screen_red_zone_5 as red
    import screen_v26_unified as v26
    import underwater_cross
    import zero_axis_live
    from kline_units import load_payload
    financials = load(ROOT / "output/cache/financials-all.json")
    quotes = load(ROOT / "output/cache/quotes" / f"{as_of}.json")
    cache_rows = {}
    def cached(code):
        if code not in cache_rows:
            cache_rows[code] = load_payload(cache / f"{code}.json")
        node = cache_rows[code]
        return list(node["rows"]), node.get("source", "offline")
    def accepted(item):
        return {**item, "financialState": "通过", "riskState": "基础通过"}
    result = {"scope": "technical parity only; risk gate accepts all as a test fixture", "models": {}}
    with mock.patch.object(data, "fetch_financials", return_value=financials), \
            mock.patch.object(red, "fetch_daily_quotes", return_value=quotes), \
            mock.patch.object(v26, "fetch_kline", side_effect=cached), \
            mock.patch.object(v26, "AS_OF", None), \
            mock.patch.object(context, "fundamental_trend", return_value={"tier": 2}), \
            mock.patch.object(red, "enrich_financial_risk", side_effect=accepted):
        for key, current in [("ucross", underwater_cross), ("zaxis", zero_axis_live)]:
            path = baseline / "scripts/research" / f"{current.__name__}.py"
            spec = importlib.util.spec_from_file_location(f"baseline_{key}", path)
            previous = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(previous)
            def run(module, file):
                if key == "ucross":
                    return module.live(as_of, 1, file)
                return module.live(as_of, file, 1)
            old = run(previous, out / f"{key}-before.json")
            new = run(current, out / f"{key}-after.json")
            signature = lambda items: [(r["code"], r["crossDate"]) for r in items]
            result["models"][key] = {"sameCandidatesAndOrder": signature(old) == signature(new),
                "before": signature(old), "after": signature(new),
                "scan": load(out / f"{key}-after.json")["scan"]}
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--as-of", required=True)
    parser.add_argument("--cache-dir", type=Path, required=True)
    parser.add_argument("--baseline", type=Path, required=True, help="Folder containing before/ and baseline-manifest.json")
    parser.add_argument("--output-dir", type=Path, required=True, help="New directory; existing directories are refused")
    parser.add_argument("--apply-tracking", action="store_true", help="Back up and replace live tracking only after all checks pass")
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=False)
    live = ROOT / "output/daily/tracking.json"
    original_bytes = live.read_bytes()
    original = json.loads(original_bytes)
    invariants = technical_invariants(args.baseline / "before")
    preview, replay = replay_tracking(original, args.cache_dir, args.as_of)
    snapshots = rule_snapshots(args.cache_dir, ["2026-08-31", "2026-09-30", "2026-10-30", args.as_of])
    parity = golden_cross_parity(args.baseline / "before", args.cache_dir, args.as_of, args.output_dir)
    result = {"asOf": args.as_of, "technicalInvariants": invariants, "trackingReplay": replay,
              "ruleComparison": snapshots, "goldenCrossParity": parity, "provenance": rule_provenance(ROOT),
              "liveTrackingApplied": False}
    daily.save_json(args.output_dir / "tracking-v2-preview.json", preview)
    if (not all(invariants.values()) or not replay["allEventsPreserved"] or replay["pendingEpisodes"]
            or not all(model["sameCandidatesAndOrder"] for model in parity["models"].values())):
        daily.save_json(args.output_dir / "verification.json", result)
        raise RuntimeError("verification incomplete; live tracking was not changed")
    if args.apply_tracking:
        if live.read_bytes() != original_bytes:
            raise RuntimeError("live tracking changed during audit; refusing to overwrite")
        backup = args.output_dir / "tracking-original.json"
        shutil.copy2(live, backup)
        preview["migration"] = {"asOf": args.as_of, "sourceSha256": hashlib.sha256(original_bytes).hexdigest(),
                                 "backup": str(backup.resolve()), "allSelectionEventsPreserved": True}
        daily.save_json(live, preview)
        result["liveTrackingApplied"] = True
    daily.save_json(args.output_dir / "verification.json", result)
    lines = ["# 可靠性离线验证", "", f"数据截至：{args.as_of}", "",
             f"- 技术条件及排序 AST 对照：{sum(invariants.values())}/{len(invariants)} 项一致。",
             f"- 台账：{replay['originalRecords']} 条 → {replay['newEpisodes']} 个观察批次。",
             f"- 历史入选事件：{replay['eventCount']} 条，完整保留：{replay['allEventsPreserved']}。",
             f"- 数据待核批次：{len(replay['pendingEpisodes'])}。",
             f"- 已更新正式台账：{result['liveTrackingApplied']}。", "",
             "## 实际重新入选案例", "", "| 股票 | 原首次日 | 新一轮首次日 | 原状态 | 新状态 |",
             "| --- | --- | --- | --- | --- |"]
    lines.extend(f"| {x['name']} {x['key']} | {x['oldFirst']} | {x['newFirst']} | {x['oldStatus']} | {x['newStatus']} |" for x in replay["reselectedStocks"])
    lines += ["", "## 相同生产检查函数的技术规则对照", "",
              "以下只验证严格技术候选池口径，不代表完整入选名单，也不证明收益改善。", "",
              "| 日期 | 当前完整条件 | 去日RSI下限 | 日RSI上限<50 | 去周RSI条件 | 旧研究简化池 |",
              "| --- | ---: | ---: | ---: | ---: | ---: |"]
    for date, stat in snapshots["dates"].items():
        lines.append(f"| {date} | " + " | ".join(str(x) for x in stat["counts"].values()) + " |")
    lines += ["", "未重新估计超额收益；既有历史区间已被多次研究，参数与排序保持原样。"]
    lines += ["", "## 金叉清单技术结果对照", "", "同一份本地财务名单、收盘快照与K线；风险检查使用全部放行的测试替身，只隔离验证技术条件。", ""]
    for model, stat in parity["models"].items():
        lines.append(f"- {model}：原版 {len(stat['before'])} 只，新版 {len(stat['after'])} 只，名单及顺序一致：{stat['sameCandidatesAndOrder']}。")
    (args.output_dir / "verification.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps({"invariants": all(invariants.values()), "tracking": replay,
                      "applied": result["liveTrackingApplied"], "output": str(args.output_dir)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
