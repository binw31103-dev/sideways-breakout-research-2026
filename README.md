# 横盘启动研究观察台

本项目把目录中的各轮 A 股横盘启动研究报告合并为一个本地网页，按报告、时间、类别和单票持续跟踪。页面仅用于观察池研究，不提供买卖点、仓位或交易指令。

## 运行

```bash
npm install
npm run data:refresh
npm run dev
```

默认地址：`http://127.0.0.1:5173/`

生产构建：

```bash
npm run build
npm run preview
```

## 数据更新

`scripts/build_data.py` 会读取项目内已登记的选股报告，合并同一股票的跨轮记录，并生成 `src/data/stock-data.json`。

- 首次入选日期使用报告形成日期。
- 首次入选价格使用该日期之前最近交易日的收盘价。
- 行情优先使用腾讯前复权日 K 线；接口不可用时回退新浪日 K 线，并在页面注明口径。
- 财务摘要使用东方财富最新主要财务指标。
- 新增报告时，在 `scripts/build_data.py` 的 `REPORTS` 中登记文件、日期、版本和规则摘要。

## 主要文件

- `网页执行提示词.md`：持续迭代网页的 AutoLoop 提示词。
- `scripts/build_data.py`：报告解析和行情/财务数据构建。
- `src/App.tsx`：四个主视图与单票详情交互。
- `src/styles.css`：桌面、平板和移动端视觉系统。
- `qa/research-dashboard-qa-ledger.txt`：浏览器验收和改进记录。

## 通达信公式

- `通达信公式/01_HPBASE_周线辅助指标.txt`：提供RSI14和MACD周线跨周期输出。
- `通达信公式/02_HPRED231_核心池条件选股.txt`：当前v2.3.1核心池技术预筛公式。
- `通达信公式/03_HPRED24_强化实验选股.txt`：增加修复方向和MA30下压限制的实验公式。
- `通达信公式/04_HPEDGE25_红框右沿跟踪选股.txt`：最近三日曾严格触发后的v2.5右沿跟踪公式。
- `通达信公式/通达信安装与规则对应说明.md`：安装顺序、规则映射和无法自动化的外部复核项。

## v2.5 状态跟踪

```bash
python3 scripts/update_v25_tracking_pool.py \
  --strict-snapshot output/tdx-selection-2026-07-28-close.json \
  --current output/tdx-selection-2026-07-29-intraday.json
```

脚本维护首次严格触发、红框右沿、启动确认、破位失效和数据待复核状态，不计算评分。正式日常运行应使用收盘后完整数据；盘中快照只作临时复核。

---

## 每日任务与模型（Python）

三个并行清单，每天 17:05 收盘后运行并推送飞书，14:45 和次日 9:26 各有一条辅助提醒。规则细节见 `三类策略说明-2026-10-29.md`、`当前筛选逻辑-v2.7.txt`、`横盘选股最优方案-2026-10-22.md`。

| 清单 | 说明 | 观察期 |
| --- | --- | --- |
| 右侧启动 v1.3 | 刚突破底部平台；分"右沿初确认""强启动确认" | 20 个交易日，不设破位 |
| 底部观察池 v2.6 | 仍在底部横盘；状态为"当前统一观察/右沿观察" | 40 个交易日，跌破入选前 60 日低点 5% 失效 |
| 低位水下金叉 | 低点附近出现水下金叉 + 底背离、无反复金叉 | 40 个交易日，不设破位 |

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
- `node_modules/`、`dist/`、`.claude/`

以上内容只用于观察池研究，不构成投资建议。
