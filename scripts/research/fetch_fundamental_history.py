import json, subprocess, time, urllib.parse, sys
from pathlib import Path
# Writes to output/cache/fundamentals/ (perf.json, holders.json). Includes every quarter up to the latest one.
OUT = Path(__file__).resolve().parents[2] / "output" / "cache" / "fundamentals"
OUT.mkdir(parents=True, exist_ok=True)
BASE = "https://datacenter-web.eastmoney.com/api/data/v1/get?"
def get(params):
    url = BASE + urllib.parse.urlencode(params)
    for _ in range(4):
        try:
            out = subprocess.run(["curl", "-sS", "-m", "30", url], capture_output=True, text=True).stdout
            return json.loads(out)["result"] or {"data": [], "pages": 0}
        except Exception:
            time.sleep(2)
    raise RuntimeError(url)
def fetch_all(report, cols, flt, sort):
    rows, page = [], 1
    while True:
        r = get({"reportName": report, "columns": cols, "pageSize": 500, "pageNumber": page,
                 "filter": flt, "sortColumns": sort, "sortTypes": 1})
        rows += r.get("data") or []
        if page >= (r.get("pages") or 0): return rows
        page += 1; time.sleep(0.2)
quarters = [f"{y}-{md}" for y in range(2022, 2027) for md in ("03-31", "06-30", "09-30", "12-31") if "2022-06-30" <= f"{y}-{md}" <= time.strftime("%Y-%m-%d")]
perf, holders = [], []
for q in quarters:
    p = fetch_all("RPT_LICO_FN_CPD", "SECURITY_CODE,REPORTDATE,NOTICE_DATE,PARENT_NETPROFIT,SJLTZ,YSTZ,DEDUCT_BASIC_EPS,WEIGHTAVG_ROE,TOTAL_OPERATE_INCOME",
                  f"(REPORTDATE='{q}')", "SECURITY_CODE")
    h = fetch_all("RPT_HOLDERNUM_DET", "SECURITY_CODE,END_DATE,HOLD_NOTICE_DATE,HOLDER_NUM,HOLDER_NUM_RATIO,AVG_HOLD_NUM",
                  f"(END_DATE='{q}')", "SECURITY_CODE")
    perf += p; holders += h
    print(q, len(p), len(h), flush=True)
json.dump(perf, open(OUT / "perf.json", "w"), ensure_ascii=False)
json.dump(holders, open(OUT / "holders.json", "w"), ensure_ascii=False)
