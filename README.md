# 横盘启动选股

A 股底部横盘启动选股研究台：每天收盘后自动跑四条观察清单并推送飞书，另配盘中预选和开盘前检查。只用于观察池研究，不构成投资建议，不提供买卖点、仓位或交易指令。

## 规则文档

- `当前筛选逻辑-v2.7.txt`：当前执行的完整筛选逻辑（以代码为准）。
- `四类策略说明-2026-11-07.md`：四类策略的名称、判断依据、状态标签和回测结论（**当前版本**）。
- `策略数据快照-2026-11-06.json`：最新一期入选名单、跟踪台账统计和跟踪中明细。
- `三类策略说明-2026-10-29.md`：旧版三类说明，保留作历史对照。
- `横盘选股最优方案-2026-10-22.md`：v2.7 调整依据。
- `v2.6统一横盘启动选股规则.md`、`需求文档.md`：历史规则与需求沿革。

## 每日任务与模型（Python）

每天 17:05 收盘后运行并推送飞书，14:45 和次日 9:26 各有一条辅助提醒。规则细节见 `四类策略说明-2026-11-07.md`、`当前筛选逻辑-v2.7.txt`、`横盘选股最优方案-2026-10-22.md`。

| 名称 | 原编号 | 说明 | 观察期 |
| --- | --- | --- | --- |
| 平台初破 | v1.3 | 平台刚被突破、还没走远；分"右沿初确认""强启动确认" | 20 个交易日，不设破位 |
| 底部横盘 | v2.6 | 仍在底部横盘；状态为"当前统一观察/右沿观察" | 40 个交易日，跌破入选前 60 日低点 5% 失效 |
| 水下金叉 | ucross | 低点附近出现水下金叉 + 底背离、无反复金叉 | 40 个交易日，不设破位 |
| 零轴金叉 | — | 水下金叉且贴近零轴（\|DIF\|/价≤1%）、位置低、还没动 | 20 个交易日，不设破位 |

```bash
python3 scripts/run_daily_screen_and_notify.py            # 收盘选股（加 --send 推送）
python3 scripts/intraday_screen.py --simulate 2026-10-30  # 14:45 盘中预选（回放测试）
python3 scripts/preopen_check.py --force                  # 9:26 开盘前检查（干跑）
```

定时任务通过 macOS LaunchAgent 配置：`com.sideways.daily-screen`（17:05）、`com.sideways.intraday-screen`（14:45）、`com.sideways.preopen-check`（9:26）。

### 数据来源（不依赖单一平台）

- 日 K：腾讯前复权（两个主机轮换）；腾讯不可用时用"新浪不复权 K 线 + 新浪复权因子"。
- 回测：`scripts/research/build_kline_dataset.py` 生成比例复权数据集（新浪原始价 + 新浪复权因子），并用东财分红表、旧腾讯序列交叉核对。
- 行情快照：东方财富，失败时用腾讯；盘中预选用腾讯 + 新浪互相核对。
- 财务/股东户数/披露日期：东方财富数据中心。
- 飞书凭证不在文件里：webhook 在 macOS 钥匙串，app id/secret 走环境变量。多维表格 id 见 `config/feishu_bitable.json`（本地，不入库），格式参照 `config/feishu_bitable.example.json`。

### 回测与研究脚本

`scripts/research/` 下的脚本覆盖：组合重验（`combo_v13_v26.py`）、特征与打分（`factor_study.py`）、每日选股模拟（`portfolio_sim.py`）、水下金叉（`underwater_cross.py`）、持有期曲线（`holding_period.py`）、开盘跳空（`entry_gap.py`、`gap_buckets.py`）、行业龙头（`leader_link.py`）、实际推送表现（`live_since.py`）等。结果表保存在 `output/backtest/`。

### 仓库内容说明

为控制体积，以下内容不入库，可重新生成：

- `output/cache/`：K 线、行情、财务缓存（约 1.4G）
- `output/daily/*/`、`output/intraday/*/`：每日原始输出（保留 `output/daily/tracking.json` 跟踪台账和 `output/intraday/consistency.jsonl` 核对记录）
- `output/backtest/*.json`：信号缓存（`--rescan` 重建）
- `.claude/`

以上内容只用于观察池研究，不构成投资建议。
