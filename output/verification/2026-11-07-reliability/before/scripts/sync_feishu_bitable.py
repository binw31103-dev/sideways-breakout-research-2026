#!/usr/bin/env python3
"""Sync daily stock-screen records to Feishu Bitable."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DAILY_ROOT = ROOT / "output" / "daily"
CONFIG_PATH = ROOT / "config" / "feishu_bitable.json"
STATE_PATH = DAILY_ROOT / ".feishu_bitable_sync_state.json"
EXPORT_DIR = DAILY_ROOT / "feishu"
OPEN_FEISHU = "https://open.feishu.cn"
BATCH_SIZE = 100
KEYCHAIN_APP_ID_SERVICE = "codex-sideways-feishu-app-id"
KEYCHAIN_APP_SECRET_SERVICE = "codex-sideways-feishu-app-secret"


DAILY_FIELDS = [
    "同步主键",
    "数据日",
    "模型版本",
    "股票代码",
    "股票名称",
    "状态",
    "行业",
    "收盘价",
    "首次入选日",
    "距首次涨跌幅",
    "入选次数",
    "日RSI",
    "周RSI",
    "周MACD状态",
    "距近60日低点涨幅",
    "距一年低点涨幅",
    "5日涨幅",
    "10日涨幅",
    "20日涨幅",
    "MA5距离",
    "MA10距离",
    "MA15距离",
    "MA30距离",
    "行业启动状态",
    "财务状态",
    "风险状态",
    "输出文件路径",
    "同步时间",
]

PERFORMANCE_FIELDS = [
    "样本ID",
    "模型版本",
    "股票代码",
    "股票名称",
    "首次入选日",
    "首次入选收盘价",
    "首次状态",
    "当前数据日",
    "当前收盘价",
    "当前涨跌幅",
    "T+1涨跌幅",
    "T+3涨跌幅",
    "T+5涨跌幅",
    "T+10涨跌幅",
    "T+20涨跌幅",
    "入选后最高涨幅",
    "入选后最大回撤",
    "最高价日期",
    "最低价日期",
    "跟踪交易日数",
    "跟踪状态",
    "最近一次入选日",
    "同步时间",
]

CLEAN_DAILY_FIELDS = [
    "文本",
    "数据日",
    "结果状态",
    "股票代码",
    "股票名称",
    "入选状态",
    "行业",
    "收盘价",
    "首次入选日",
    "距首次涨跌幅",
    "日RSI",
    "周RSI",
    "20日涨幅",
    "行业启动状态",
    "风险状态",
    "同步主键",
]

V13_BOARD_FIELDS = [
    "文本",
    "数据日",
    "结果状态",
    "股票代码",
    "股票名称",
    "入选状态",
    "行业",
    "收盘价",
    "突破日期",
    "站上15/30日数",
    "平台上方幅度",
    "突破量比",
    "日RSI",
    "周RSI",
    "20日涨幅",
    "行业5日扩散",
    "风险状态",
    "同步主键",
]

DAILY_CHANGE_FIELDS = [
    "文本",
    "样本ID",
    "模型版本",
    "股票代码",
    "股票名称",
    "标记开始日",
    "起始收盘价",
    "当前数据日",
    "当前收盘价",
    "最新日涨跌幅",
    "较开始涨跌幅",
    "跟踪交易日数",
    "入选后最高涨幅",
    "入选后最大回撤",
    "最高价日期",
    "最低价日期",
    "每日收盘记录",
    "每日涨跌记录",
    "同步主键",
]

CLEAN_PERFORMANCE_FIELDS = [
    "股票代码",
    "股票名称",
    "首次入选日",
    "首次状态",
    "当前数据日",
    "标记开始日",
    "当前涨跌幅",
    "T+3涨跌幅",
    "T+5涨跌幅",
    "T+10涨跌幅",
    "T+20涨跌幅",
    "入选后最高涨幅",
    "入选后最大回撤",
    "跟踪交易日数",
    "跟踪状态",
    "样本ID",
]

NUMBER_FIELDS = {
    "收盘价",
    "距首次涨跌幅",
    "入选次数",
    "日RSI",
    "周RSI",
    "距近60日低点涨幅",
    "距一年低点涨幅",
    "5日涨幅",
    "10日涨幅",
    "20日涨幅",
    "MA5距离",
    "MA10距离",
    "MA15距离",
    "MA30距离",
    "首次入选收盘价",
    "当前收盘价",
    "当前涨跌幅",
    "T+1涨跌幅",
    "T+3涨跌幅",
    "T+5涨跌幅",
    "T+10涨跌幅",
    "T+20涨跌幅",
    "入选后最高涨幅",
    "入选后最大回撤",
    "跟踪交易日数",
    "站上15/30日数",
    "平台上方幅度",
    "突破量比",
    "行业5日扩散",
    "起始收盘价",
    "最新日涨跌幅",
    "较开始涨跌幅",
}


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


def request_json(method: str, path: str, token: str | None = None, body: dict[str, Any] | None = None) -> dict[str, Any]:
    command = [
        "curl",
        "-sS",
        "--max-time",
        "30",
        "-X",
        method,
        "-H",
        "Content-Type: application/json; charset=utf-8",
    ]
    if token:
        command.extend(["-H", f"Authorization: Bearer {token}"])
    if body is not None:
        command.extend(["--data-binary", "@-"])
    command.append(f"{OPEN_FEISHU}{path}")
    result = subprocess.run(
        command,
        input=None if body is None else json.dumps(body, ensure_ascii=False),
        text=True,
        capture_output=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Feishu curl request failed: {result.stderr.strip()}")
    payload = result.stdout
    result = json.loads(payload)
    if result.get("code") not in (0, None):
        raise RuntimeError(f"Feishu API error: {payload}")
    return result


def tenant_access_token(app_id: str, app_secret: str) -> str:
    result = request_json(
        "POST",
        "/open-apis/auth/v3/tenant_access_token/internal",
        body={"app_id": app_id, "app_secret": app_secret},
    )
    token = result.get("tenant_access_token")
    if not token:
        raise RuntimeError("Feishu tenant_access_token is missing in response.")
    return token


def keychain_password(service: str) -> str:
    result = subprocess.run(
        ["security", "find-generic-password", "-a", os.environ.get("USER", ""), "-s", service, "-w"],
        text=True,
        capture_output=True,
    )
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def feishu_credentials() -> tuple[str, str]:
    app_id = os.environ.get("FEISHU_APP_ID", "") or keychain_password(KEYCHAIN_APP_ID_SERVICE)
    app_secret = os.environ.get("FEISHU_APP_SECRET", "") or keychain_password(KEYCHAIN_APP_SECRET_SERVICE)
    return app_id, app_secret


def configured_table_id(table_id: str) -> bool:
    return bool(table_id) and not table_id.startswith("替换")


def pct(first: float, current: float) -> float:
    if first == 0:
        return 0.0
    return (current / first - 1) * 100


def as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def item_row(model_key: str, item: dict[str, Any], data_date: str, output_path: Path, synced_at: str) -> dict[str, Any]:
    tracking = item.get("tracking") or {}
    checks = item.get("conditionChecks") or {}
    weekly_macd = item.get("weeklyMacd") or {}
    persistence = item.get("sectorPersistence") or {}
    return {
        "同步主键": f"{data_date}|{model_key}|{item.get('code', '')}",
        "数据日": data_date,
        "模型版本": item.get("model") or model_key,
        "股票代码": item.get("code", ""),
        "股票名称": item.get("name", ""),
        "状态": item.get("state", ""),
        "行业": item.get("industry", ""),
        "收盘价": as_float(item.get("close")),
        "首次入选日": tracking.get("firstSelectedDate", item.get("date", data_date)),
        "距首次涨跌幅": as_float(tracking.get("changeSinceFirst")),
        "入选次数": int(tracking.get("selectedCount", 1) or 1),
        "日RSI": as_float(item.get("dailyRsi")),
        "周RSI": as_float(item.get("weeklyRsi")),
        "周MACD状态": weekly_macd.get("class") or weekly_macd.get("state") or "",
        "距近60日低点涨幅": as_float(item.get("riseRecentLow")),
        "距一年低点涨幅": as_float(item.get("riseYearLow")),
        "5日涨幅": as_float(item.get("change5")),
        "10日涨幅": as_float(item.get("change10")),
        "20日涨幅": as_float(item.get("change20")),
        "MA5距离": as_float(item.get("maDistance5")),
        "MA10距离": as_float(item.get("maDistance10")),
        "MA15距离": as_float(item.get("maDistance15")),
        "MA30距离": as_float(item.get("maDistance30")),
        "行业启动状态": checks.get("sector", ""),
        "财务状态": item.get("financialState", ""),
        "风险状态": item.get("riskState", ""),
        "突破日期": item.get("breakoutDate", ""),
        "站上15/30日数": item.get("daysAbove1530", ""),
        "平台上方幅度": item.get("currentAbovePlatformPct", ""),
        "突破量比": item.get("breakoutVolumeRatio", ""),
        "行业5日扩散": persistence.get("activeDays5", ""),
        "输出文件路径": str(output_path),
        "同步时间": synced_at,
    }


def collect_daily_rows(synced_at: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in sorted(DAILY_ROOT.glob("20??-??-??/combined.json")):
        combined = load_json(path, {})
        data_date = str(combined.get("dataThrough") or path.parent.name)
        models = [("v26", "v26")]
        if "v13" in combined:
            models.append(("v13", "v1.3右侧启动"))
        for model_key, model_name in models:
            payload = combined.get(model_key) or {}
            selected = payload.get("selected") or []
            if selected:
                rows.extend(item_row(model_key, item, data_date, path, synced_at) for item in selected)
    return rows


def build_performance_rows(daily_rows: list[dict[str, Any]], synced_at: str) -> list[dict[str, Any]]:
    by_code: dict[str, list[dict[str, Any]]] = {}
    for row in daily_rows:
        code = str(row.get("股票代码") or "")
        if not code:
            continue
        model = str(row.get("模型版本") or "")
        first_date = str(row.get("首次入选日") or row.get("数据日") or "")
        by_code.setdefault(f"{model}|{code}|{first_date}", []).append(row)

    rows: list[dict[str, Any]] = []
    windows = {1: "T+1涨跌幅", 3: "T+3涨跌幅", 5: "T+5涨跌幅", 10: "T+10涨跌幅", 20: "T+20涨跌幅"}
    for _, records in sorted(by_code.items()):
        records = sorted(records, key=lambda row: str(row["数据日"]))
        first = records[0]
        code = str(first.get("股票代码") or "")
        first_close = as_float(first.get("收盘价"))
        closes = [(str(row["数据日"]), as_float(row.get("收盘价")), str(row.get("状态") or "")) for row in records]
        gains = [(date_value, pct(first_close, close)) for date_value, close, _ in closes if close]
        if not gains:
            continue
        high_date, high_gain = max(gains, key=lambda item: item[1])
        low_date, low_gain = min(gains, key=lambda item: item[1])
        current_date, current_close, _ = closes[-1]
        performance = {
            "样本ID": f"{first.get('模型版本')}:{code}:{first.get('首次入选日') or first.get('数据日')}",
            "模型版本": first.get("模型版本", "v26"),
            "股票代码": code,
            "股票名称": first.get("股票名称", ""),
            "首次入选日": first.get("首次入选日") or first.get("数据日"),
            "标记开始日": first.get("首次入选日") or first.get("数据日"),
            "首次入选收盘价": first_close,
            "首次状态": first.get("状态", ""),
            "当前数据日": current_date,
            "当前收盘价": current_close,
            "当前涨跌幅": pct(first_close, current_close),
            "入选后最高涨幅": high_gain,
            "入选后最大回撤": low_gain,
            "最高价日期": high_date,
            "最低价日期": low_date,
            "跟踪交易日数": len(records),
            "跟踪状态": "满20日归档" if len(records) >= 20 else "跟踪中",
            "最近一次入选日": records[-1].get("数据日", ""),
            "同步时间": synced_at,
        }
        for offset, field in windows.items():
            performance[field] = pct(first_close, closes[offset][1]) if len(closes) > offset else ""
        rows.append(performance)
    return rows


def sample_records(daily_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    samples: dict[str, dict[str, Any]] = {}
    for row in sorted(daily_rows, key=lambda item: str(item.get("数据日", ""))):
        code = str(row.get("股票代码") or "")
        if not code:
            continue
        model = str(row.get("模型版本") or "")
        start_date = str(row.get("首次入选日") or row.get("数据日") or "")
        key = f"{model}:{code}:{start_date}"
        samples.setdefault(key, {**row, "样本ID": key, "标记开始日": start_date})
    return list(samples.values())


def build_daily_change_rows(daily_rows: list[dict[str, Any]], synced_at: str) -> list[dict[str, Any]]:
    from screen_latest_30 import fetch_kline

    rows: list[dict[str, Any]] = []
    for sample in sample_records(daily_rows):
        code = str(sample.get("股票代码") or "")
        sample_id = str(sample.get("样本ID") or "")
        start_date = str(sample.get("标记开始日") or "")
        start_close = as_float(sample.get("收盘价"))
        if not code or not start_date or not start_close:
            continue
        try:
            kline, _ = fetch_kline(code)
        except Exception as exc:
            print(f"[feishu] daily-change skip {code}: {exc}", file=sys.stderr)
            continue
        selected_rows = [row for row in kline if str(row.get("date", "")) >= start_date]
        if not selected_rows:
            continue
        closes = [(str(row.get("date", "")), as_float(row.get("close"))) for row in selected_rows]
        gains = [(date_value, pct(start_close, close)) for date_value, close in closes if close]
        if not gains:
            continue
        high_date, high_gain = max(gains, key=lambda item: item[1])
        low_date, low_gain = min(gains, key=lambda item: item[1])
        current_date, current_close = closes[-1]
        previous_close = closes[-2][1] if len(closes) > 1 else start_close
        close_log = "; ".join(f"{date_value}:{close:.2f}" for date_value, close in closes)
        gain_log = "; ".join(f"{date_value}:{pct(start_close, close):+.2f}%" for date_value, close in closes)
        rows.append({
            "文本": f"{sample.get('股票名称', '')} {start_date}起",
            "样本ID": sample_id,
            "模型版本": sample.get("模型版本", ""),
            "股票代码": code,
            "股票名称": sample.get("股票名称", ""),
            "标记开始日": start_date,
            "起始收盘价": start_close,
            "当前数据日": current_date,
            "当前收盘价": current_close,
            "最新日涨跌幅": pct(previous_close, current_close),
            "较开始涨跌幅": pct(start_close, current_close),
            "跟踪交易日数": len(closes),
            "入选后最高涨幅": high_gain,
            "入选后最大回撤": low_gain,
            "最高价日期": high_date,
            "最低价日期": low_date,
            "每日收盘记录": close_log,
            "每日涨跌记录": gain_log,
            "同步主键": sample_id,
        })
    rows.sort(key=lambda row: (str(row.get("模型版本", "")), str(row.get("股票代码", "")), str(row.get("标记开始日", ""))))
    return rows


def build_clean_daily_rows(daily_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for row in daily_rows:
        has_stock = bool(row.get("股票代码"))
        rows.append({
            "文本": f"{row.get('数据日', '')} {row.get('股票名称', '')}",
            "数据日": row.get("数据日", ""),
            "结果状态": "有入选" if has_stock else "无满足",
            "股票代码": row.get("股票代码", ""),
            "股票名称": row.get("股票名称", ""),
            "入选状态": row.get("状态", ""),
            "行业": row.get("行业", ""),
            "收盘价": row.get("收盘价", ""),
            "首次入选日": row.get("首次入选日", ""),
            "距首次涨跌幅": row.get("距首次涨跌幅", ""),
            "日RSI": row.get("日RSI", ""),
            "周RSI": row.get("周RSI", ""),
            "20日涨幅": row.get("20日涨幅", ""),
            "行业启动状态": row.get("行业启动状态", ""),
            "风险状态": row.get("风险状态", ""),
            "同步主键": row.get("同步主键", ""),
        })
    return rows


def build_v26_board_rows(daily_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        row
        for row in build_clean_daily_rows(daily_rows)
        if str(row.get("同步主键", "")).split("|")[1:2] == ["v26"]
    ]


def build_v13_board_rows(daily_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for row in daily_rows:
        if not str(row.get("同步主键", "")).split("|")[1:2] == ["v13"]:
            continue
        has_stock = bool(row.get("股票代码"))
        rows.append({
            "文本": f"{row.get('数据日', '')} {row.get('股票名称', '')}",
            "数据日": row.get("数据日", ""),
            "结果状态": "有入选" if has_stock else "无满足",
            "股票代码": row.get("股票代码", ""),
            "股票名称": row.get("股票名称", ""),
            "入选状态": row.get("状态", ""),
            "行业": row.get("行业", ""),
            "收盘价": row.get("收盘价", ""),
            "突破日期": row.get("突破日期", ""),
            "站上15/30日数": row.get("站上15/30日数", ""),
            "平台上方幅度": row.get("平台上方幅度", ""),
            "突破量比": row.get("突破量比", ""),
            "日RSI": row.get("日RSI", ""),
            "周RSI": row.get("周RSI", ""),
            "20日涨幅": row.get("20日涨幅", ""),
            "行业5日扩散": row.get("行业5日扩散", ""),
            "风险状态": row.get("风险状态", ""),
            "同步主键": row.get("同步主键", ""),
        })
    return rows


def build_clean_performance_rows(performance_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {field: row.get(field, "") for field in CLEAN_PERFORMANCE_FIELDS}
        for row in sorted(
            performance_rows,
            key=lambda row: as_float(row.get("当前涨跌幅"), -999),
            reverse=True,
        )
    ]


def remote_fields(app_token: str, table_id: str, token: str) -> set[str]:
    result = request_json("GET", f"/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/fields", token=token)
    return {item["field_name"] for item in result.get("data", {}).get("items", [])}


def list_record_ids(app_token: str, table_id: str, token: str) -> list[str]:
    ids: list[str] = []
    page_token = ""
    while True:
        path = f"/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records?page_size=500"
        if page_token:
            path += f"&page_token={page_token}"
        result = request_json("GET", path, token=token)
        data = result.get("data", {})
        ids.extend(item.get("record_id") or item.get("id") for item in data.get("items", []))
        if not data.get("has_more"):
            return [record_id for record_id in ids if record_id]
        page_token = data.get("page_token", "")


def delete_records(app_token: str, table_id: str, token: str, record_ids: list[str]) -> None:
    for start in range(0, len(record_ids), BATCH_SIZE):
        batch = record_ids[start:start + BATCH_SIZE]
        request_json(
            "POST",
            f"/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records/batch_delete",
            token=token,
            body={"records": batch},
        )
        time.sleep(0.2)


def convert_record(row: dict[str, Any], fields: list[str], allowed_fields: set[str]) -> dict[str, Any]:
    values: dict[str, Any] = {}
    for field in fields:
        if allowed_fields and field not in allowed_fields:
            continue
        value = row.get(field, "")
        if field in NUMBER_FIELDS and value != "":
            values[field] = value
        elif field not in NUMBER_FIELDS:
            values[field] = str(value)
    return {"fields": values}


def batch_create(
    app_token: str,
    table_id: str,
    token: str,
    records: list[dict[str, Any]],
    on_batch_complete: Any | None = None,
) -> None:
    for start in range(0, len(records), BATCH_SIZE):
        batch = records[start:start + BATCH_SIZE]
        request_json(
            "POST",
            f"/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records/batch_create",
            token=token,
            body={"records": batch},
        )
        print(f"[feishu] created {start + len(batch)}/{len(records)}", flush=True)
        if on_batch_complete is not None:
            on_batch_complete(start + len(batch))
        time.sleep(0.2)


def sync_table(
    app_token: str,
    table_name: str,
    spec: dict[str, Any],
    rows: list[dict[str, Any]],
    fields: list[str],
    token: str | None,
    state: dict[str, Any],
    dry_run: bool,
) -> int:
    table_id = spec.get("table_id", "")
    if not configured_table_id(table_id):
        print(f"[feishu] skip {table_name}: table_id not configured")
        return 0
    mode = spec.get("mode", "append")
    if dry_run:
        print(f"[feishu] dry-run {table_name}: {len(rows)} local rows")
        return len(rows)
    if token is None:
        print(f"[feishu] skip {table_name}: FEISHU_APP_ID/FEISHU_APP_SECRET missing")
        return 0

    allowed_fields = remote_fields(app_token, table_id, token)
    table_state = state.setdefault(table_name, {"synced_keys": []})
    synced_keys = set() if mode == "replace" else set(table_state.get("synced_keys", []))
    records = []
    new_keys = []
    for row in rows:
        key = str(row.get("同步主键") or row.get("样本ID") or "")
        if mode != "replace" and key in synced_keys:
            continue
        records.append(convert_record(row, fields, allowed_fields))
        new_keys.append(key)

    if mode == "replace":
        delete_records(app_token, table_id, token, list_record_ids(app_token, table_id, token))
    if records:
        def persist_created_batches(completed: int) -> None:
            table_state["synced_keys"] = sorted(synced_keys | set(new_keys[:completed]))
            table_state["last_synced_at"] = datetime.now().isoformat(timespec="seconds")
            write_json(STATE_PATH, state)

        batch_create(
            app_token,
            table_id,
            token,
            records,
            on_batch_complete=persist_created_batches,
        )
    if mode != "replace":
        table_state["synced_keys"] = sorted(synced_keys | set(new_keys))
    table_state["last_synced_at"] = datetime.now().isoformat(timespec="seconds")
    print(f"[feishu] synced {table_name}: {len(records)}")
    return len(records)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=str(CONFIG_PATH))
    parser.add_argument("--env", default=str(ROOT / ".env"))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    load_dotenv(Path(args.env))
    synced_at = datetime.now().isoformat(timespec="seconds")
    daily_rows = collect_daily_rows(synced_at)
    performance_rows = build_performance_rows(daily_rows, synced_at)
    clean_daily_rows = build_clean_daily_rows(daily_rows)
    clean_performance_rows = build_clean_performance_rows(performance_rows)
    v26_board_rows = build_v26_board_rows(daily_rows)
    v13_board_rows = build_v13_board_rows(daily_rows)
    daily_change_rows = build_daily_change_rows(daily_rows, synced_at)
    write_json(EXPORT_DIR / "daily-selection-detail.json", daily_rows)
    write_json(EXPORT_DIR / "performance-tracking.json", performance_rows)
    write_json(EXPORT_DIR / "daily-board.json", clean_daily_rows)
    write_json(EXPORT_DIR / "v26-board.json", v26_board_rows)
    write_json(EXPORT_DIR / "performance-board.json", clean_performance_rows)
    write_json(EXPORT_DIR / "v13-fengfan-board.json", v13_board_rows)
    write_json(EXPORT_DIR / "daily-change-tracking.json", daily_change_rows)
    print(
        "[feishu] built local exports: "
        f"daily={len(daily_rows)} performance={len(performance_rows)} "
        f"v26_board={len(v26_board_rows)} v13_board={len(v13_board_rows)} "
        f"performance_board={len(clean_performance_rows)} daily_change={len(daily_change_rows)}"
    )

    config = load_json(Path(args.config), {})
    app_token = config.get("app_token", "")
    tables = config.get("tables") or {}
    if not app_token or not tables:
        print(f"[feishu] config missing app_token/tables, skip remote sync: {args.config}")
        return

    app_id, app_secret = feishu_credentials()
    token = None
    if app_id and app_secret and not args.dry_run:
        token = tenant_access_token(app_id, app_secret)

    state = load_json(STATE_PATH, {})
    sync_table(
        app_token,
        "01 v2.6统一观察每日票池",
        tables.get("01 v2.6统一观察每日票池", {}),
        v26_board_rows,
        CLEAN_DAILY_FIELDS,
        token,
        state,
        args.dry_run,
    )
    sync_table(
        app_token,
        "03 表现看板",
        tables.get("03 表现看板", {}),
        clean_performance_rows,
        CLEAN_PERFORMANCE_FIELDS,
        token,
        state,
        args.dry_run,
    )
    sync_table(
        app_token,
        "02 v1.3右侧启动每日票池",
        tables.get("02 v1.3右侧启动每日票池", {}),
        v13_board_rows,
        V13_BOARD_FIELDS,
        token,
        state,
        args.dry_run,
    )
    sync_table(
        app_token,
        "04 股票每日涨跌跟踪",
        tables.get("04 股票每日涨跌跟踪", {}),
        daily_change_rows,
        DAILY_CHANGE_FIELDS,
        token,
        state,
        args.dry_run,
    )
    if not args.dry_run and token:
        active_tables = {
            "01 v2.6统一观察每日票池",
            "02 v1.3右侧启动每日票池",
            "03 表现看板",
            "04 股票每日涨跌跟踪",
        }
        for table_name in list(state):
            if table_name not in active_tables:
                state.pop(table_name, None)
        write_json(STATE_PATH, state)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"[feishu] failed: {exc}", file=sys.stderr)
        sys.exit(1)
