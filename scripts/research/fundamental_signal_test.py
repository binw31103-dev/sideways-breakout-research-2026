import json, statistics as st, sys, bisect
from collections import defaultdict
sys.path.insert(0, "scripts")
from backtest_bottom_base import benchmark_index, annotate_market, v26
idx = benchmark_index()
sig = [s for s in json.load(open("/tmp/sideways-analysis/signals.json")) if s["date"] >= "2023-07-01" and not s["date"].startswith("2024-09")]
annotate_market(sig, idx); sig = [s for s in sig if s["m"]]
# point-in-time fundamentals
perf = defaultdict(dict)
for r in json.load(open("/tmp/sideways-analysis/fund/perf.json")):
    if not r.get("NOTICE_DATE"): continue
    key = r["REPORTDATE"][:10]; n = r["NOTICE_DATE"][:10]
    old = perf[r["SECURITY_CODE"]].get(key)
    if old is None or n < old["n"]:
        perf[r["SECURITY_CODE"]][key] = {"n": n, "q": key, "yoy": r.get("SJLTZ"), "rev": r.get("YSTZ"), "np": r.get("PARENT_NETPROFIT"), "roe": r.get("WEIGHTAVG_ROE")}
hold = defaultdict(list)
for r in json.load(open("/tmp/sideways-analysis/fund/holders.json")):
    if r.get("HOLD_NOTICE_DATE"):
        hold[r["SECURITY_CODE"]].append((r["HOLD_NOTICE_DATE"][:10], r["END_DATE"][:10], r.get("HOLDER_NUM_RATIO")))
for v in hold.values(): v.sort()
def pit(code, date):
    reps = sorted((x for x in perf.get(code, {}).values() if x["n"] < date), key=lambda x: x["q"])
    hs = [h for h in hold.get(code, []) if h[0] < date]
    hs.sort(key=lambda h: h[1])
    f = {}
    if reps:
        a = reps[-1]; f.update(yoy=a["yoy"], rev=a["rev"], np=a["np"], roe=a["roe"], fresh=(int(date[:4])*372+int(date[5:7])*31+int(date[8:]))-(int(a["n"][:4])*372+int(a["n"][5:7])*31+int(a["n"][8:])))
        if len(reps) >= 2: f["yoyPrev"] = reps[-2]["yoy"]
    if hs:
        f["holder"] = hs[-1][2]
        if len(hs) >= 2: f["holderPrev"] = hs[-2][2]
    return f
cache = {}
def rows(code):
    if code not in cache: cache[code] = json.load(open(f"/tmp/sideways-analysis/kline/{code}.json"))["rows"]
    return cache[code]
base, last = [], {}
for s in sig:
    if v26(s) and s["i"] - last.get(s["code"], -20) >= 20:
        last[s["code"]] = s["i"]; base.append(s)
res = []
for s in base:
    r = rows(s["code"]); i = s["i"]
    if i + 41 >= len(r): continue
    e = r[i+1]["open"]; k = i + 40
    b0, b1 = idx.get(s["date"]), idx.get(r[k]["date"])
    if not b0 or not b1: continue
    res.append({"s": s, "ex": r[k]["close"]/e-1-(b1/b0-1), "ff": pit(s["code"], s["date"])})
bym = defaultdict(list)
for x in res: bym[x["s"]["date"][:7]].append(x["ex"])
mm = {m: st.mean(v) for m, v in bym.items()}
for x in res: x["dm"] = x["ex"] - mm[x["s"]["date"][:7]]
cov = sum(1 for x in res if "yoy" in x["ff"]) / len(res); covh = sum(1 for x in res if "holder" in x["ff"]) / len(res)
print(f"样本 {len(res)}  有业绩数据 {cov:.0%}  有股东户数 {covh:.0%}\n")
def num(v): return isinstance(v, (int, float))
TESTS = {
 "净利同比>0": lambda f: num(f.get("yoy")) and f["yoy"] > 0,
 "净利同比>30%": lambda f: num(f.get("yoy")) and f["yoy"] > 30,
 "净利同比由负转正(拐点)": lambda f: num(f.get("yoy")) and num(f.get("yoyPrev")) and f["yoy"] > 0 and f["yoyPrev"] <= 0,
 "净利同比改善(本期>上期)": lambda f: num(f.get("yoy")) and num(f.get("yoyPrev")) and f["yoy"] > f["yoyPrev"],
 "营收同比>0": lambda f: num(f.get("rev")) and f["rev"] > 0,
 "营收>0且净利>0同比": lambda f: num(f.get("rev")) and num(f.get("yoy")) and f["rev"] > 0 and f["yoy"] > 0,
 "当期盈利(归母>0)": lambda f: num(f.get("np")) and f["np"] > 0,
 "ROE>5%(期内)": lambda f: num(f.get("roe")) and f["roe"] > 5,
 "财报30天内刚披露": lambda f: num(f.get("fresh")) and f["fresh"] <= 30,
 "股东户数减少": lambda f: num(f.get("holder")) and f["holder"] < 0,
 "股东户数减少>5%": lambda f: num(f.get("holder")) and f["holder"] < -5,
 "股东户数连续两期减少": lambda f: num(f.get("holder")) and num(f.get("holderPrev")) and f["holder"] < 0 and f["holderPrev"] < 0,
 "股东户数增加>5%": lambda f: num(f.get("holder")) and f["holder"] > 5,
}
print(f"{'条件':24s} {'命中':>6s} | 同月内超额差(命中-未命中)  前段      后段   | 命中组40日超额 前段/后段")
for name, t in TESTS.items():
    out = []
    for lab, test in (("前", lambda d: d < "2025-01"), ("后", lambda d: d >= "2025-01")):
        a = [x for x in res if test(x["s"]["date"]) and t(x["ff"])]; b = [x for x in res if test(x["s"]["date"]) and not t(x["ff"])]
        out.append((st.mean(y["dm"] for y in a) - st.mean(y["dm"] for y in b) if a and b else float("nan"), st.mean(y["ex"] for y in a) if a else float("nan"), len(a)))
    hit = sum(1 for x in res if t(x["ff"]))
    print(f"{name:24s} {hit/len(res):6.0%} |   {out[0][0]:+.2%}(n={out[0][2]})  {out[1][0]:+.2%}(n={out[1][2]}) | {out[0][1]:+.2%} / {out[1][1]:+.2%}")
json.dump([{"code": x["s"]["code"], "date": x["s"]["date"], "ex": x["ex"], "dm": x["dm"], "ff": x["ff"], "f": x["s"]["f"]} for x in res], open("/tmp/sideways-analysis/fund_res.json", "w"))
