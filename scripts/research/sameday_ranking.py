#!/usr/bin/env python3
"""Same-day ranking value: mean excess of the day's top 10 minus all candidates that day,
only on days with more than 10 candidates. Compares the old order with the live one
(fundamentals tier 0, then bottom_base_context.literature_score).

Usage: python3 scripts/research/sameday_ranking.py
"""
import sys, json, statistics as st, bisect
from collections import defaultdict
sys.path.insert(0,'scripts'); sys.path.insert(0,'scripts/research')
from backtest_bottom_base import annotate_market, benchmark_index, v26
from combo_v13_v26 import fundamentals_tier0
from factor_study import FEATURES, PERIODS, QUOTES, features, load_fundamentals
from portfolio_sim import exit_result
from bottom_base_context import literature_score
PRIOR={k:v for k,v in FEATURES.items() if v}
keep=lambda s: s["date"]>="2023-07-01" and not s["date"].startswith("2024-09")
idx=benchmark_index(); q=json.load(open(QUOTES))
shares={c:(x["totalMarketCap"]/x["close"],x["floatMarketCap"]/x["close"]) for c,x in q.items() if x.get("close") and x.get("totalMarketCap")}
fa,t0=load_fundamentals(),fundamentals_tier0()
from screen_latest_30 import fetch_financials
prof=set(fetch_financials())
for model,path,flt,exits in (("v2.6","output/backtest/signals.json",v26,[("40日·止损5%",40,0.95)]),
                             ("v1.3","output/backtest/v13_signals.json",lambda s: s["code"] in prof,[("持有20日",20,None)])):
    sig=[s for s in json.load(open(path)) if keep(s) and flt(s)]
    annotate_market(sig,idx); sig=[s for s in sig if s.get("m")]
    for s in sig: s["feat"]=features(s,fa,t0,shares)
    train=[s for s in sig if s["date"]<"2025-01"]
    ref={f:sorted(s["feat"][f]*g for s in train if f in s["feat"]) for f,g in PRIOR.items()}
    for s in sig:
        s["score"]=literature_score(s["feat"])
    rules={"旧排序（基本面→大涨次数→低价）":lambda s:(-s["feat"].get("fundTier0",0),-min(s["feat"].get("bigUp250",0),3),s["feat"].get("price",0)),
           "新排序（基本面层级→文献合成分）":lambda s:(-s["feat"].get("fundTier0",0),-s["score"])}
    byday=defaultdict(list)
    for s in sig: byday[s["date"]].append(s)
    for ename,days,buf in exits:
        print(f"\n{model} · {ename} · 同日超额差（选中10只 − 当天全部候选），只计候选>10只的日子")
        for rule,key in rules.items():
            cells=[]
            for pname,lo,hi in PERIODS:
                diffs=[]; months=defaultdict(list)
                for d,c in byday.items():
                    if not(lo<=d<hi) or len(c)<=10: continue
                    ex={}
                    for s in c:
                        r=exit_result(s["code"],s["i"],days,buf,None)
                        if r and r["complete"] and r["exitDate"] in idx:
                            ex[id(s)]=r["ret"]-(idx[r["exitDate"]]/idx[d]-1)
                    if len(ex)<=10: continue
                    pool=[s for s in c if id(s) in ex]
                    top=sorted(pool,key=key)[:10]
                    v=st.mean(ex[id(s)] for s in top)-st.mean(ex.values())
                    diffs.append(v); months[d[:7]].append(v)
                if diffs:
                    pm=sum(st.mean(v)>0 for v in months.values())/len(months)
                    t=st.mean(diffs)/(st.stdev(diffs)/len(diffs)**.5) if len(diffs)>2 and st.stdev(diffs) else 0
                    cells.append(f"{st.mean(diffs):+.2%}（t{t:+.1f}，正月{pm:.0%}）")
                else: cells.append("n/a")
            print(f"  {rule:16s} "+" | ".join(f"{p[0][:2]} {c}" for p,c in zip(PERIODS,cells)))
