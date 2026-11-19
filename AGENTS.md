# AGENTS.md — FinSight-Agent 全局开发准则（所有会话必读）

> 本文件是本仓库的**唯一项目记忆与架构准绳**。任何开发会话（人或 Agent）在改动代码前
> 必须先读本文件；与本文件冲突的实现一律视为缺陷。
>
> 项目定位：面向金融投研 / 财务尽调的**多智能体上市公司财报深度解构与财务排雷系统**（开源展示）。

---

## 0. 当前阶段与硬边界

**Phase 4（当前，收官）**：Streamlit 投研工作台（`app.py` + `src/ui/`）、打印级 HTML 导出、
旗舰级 `README.md` 与全局文档收口；**Patch 4.1** 补北交所 920 新号段 / 新三板兼容与友好降级。
**已完成范围**：`src/tools/`（Phase 1 / 1.1）、`src/agents/`（Phase 2）、`src/rag/`（Phase 3）、
`src/ui/` + `app.py`（Phase 4）、`tests/` 五套实盘验证脚本（离线 283 项断言 + 联网变体）。

**Phase 4 已确定的边界**：
1. **UI 层只读不写**：`src/ui/*` 与 `app.py` 不得出现任何财务运算（`/ 1e8`、加减乘除业务口径、
   重算比率一律禁止），所有数字必须来自 `FinancialReport` / `PeerComparison` / `rag_evidences`；
   唯一的数值变换是"小数 → 百分数"的**显示换算**（×100，与 `context_formatter.fmt_pct` 同构）。
2. **图表数据源唯一**：杜邦三因子贡献等派生量必须由 `src/tools/financial_engine.py` 计算并落库
   （`YearDuPont.net_margin_effect` 等），**不允许前端现算**。
3. **打印级导出不得引入二进制依赖**：不用 weasyprint / wkhtmltopdf，用纯 Python 生成
   自包含 HTML（内联 CSS + `@media print` A4）。
4. **历史回放不消耗 Token**：`agent_trace.json` 不含完整 `FinancialReport`，
   允许通过 `analyze_stock`（磁盘缓存）毫秒级重建，但**不得**为此重新调用 LLM。
5. **密钥零回显**：页面只显示 `已配置(len=N)`，任何异常信息必须过 `mask_secret()`。

后续阶段（Phase 5+）在本文档末尾的路线图中推进，未获得用户明确指令不得提前开工。

---

## 1. 四条架构铁律（不可协商）

### 铁律 1｜计算与推理严格分离
- 所有财务比率、同比变动、杜邦拆解、舞弊预警模型分值，**必须**由 Python（`pandas`/`numpy`）精确计算，
  并以结构化 JSON 输出。
- **严禁**让 LLM 直接口算、心算或"读表推数"任何原始报表数字。
- LLM 的合法职责只有：解读已算好的 JSON、组织语言、给出业务假设与追问清单。
- 代码位置约定：一切数值逻辑只允许出现在 `src/tools/`；`src/agents/` 只能调用、不得重算。

### 铁律 2｜数据可追溯性（防幻觉）
任何计算结果必须能回答三个问题：**这个数从哪来、属于哪个报告期、原始科目是多少**。
具体要求：
- 每个年度指标必须携带 `report_period`（如 `"2024年报"`）与 `report_year`。
- 报告根节点必须保留 `raw_metrics`：原始科目数值（统一换算为**亿元**，保留 4 位小数）。
- 报告根节点必须保留 `meta.data_source`：数据源名称、接口函数名、抓取时间、缓存命中状态。
- 必备交叉校验：`include_cross_validation=True` 时与东方财富/新浪财务指标做独立比对，
  输出每一项的偏差值与是否通过，用于自证引擎正确性。

### 铁律 3｜容错与防御性编程
- 财报缺失值（`NaN`/`None`）与分母为 0 是常态，不是异常。
- 缺失科目 / 无法计算的指标 → **返回 `null`**，并在 `warnings` 中写明：
  哪个股票、哪个报告期、哪个科目、为什么算不出来。
- **绝对禁止**把缺失值静默填充为 `0`、`1` 或行业均值来"跑通流程"——这会直接误导财务判断。
- 分母为 0 或近似 0（`abs(denominator) < 1e-9`）时必须短路返回 `null`。
- 唯一允许的"缺失当 0"场景：`有息负债` 各构成科目（短期借款/长期借款/应付债券…）在数据源中
  **全部为空**时，按 0 处理，但**必须**同权限写警告，提示以年报附注为准。

### 铁律 4｜模块化开发 + 实盘验证才算完成
- 每完成一个模块，必须同时提交一个**实盘股票**测试脚本，并在终端跑通、打印出合理数值后才算完成。
- 只写代码不跑测试 = 未完成。
- 测试必须包含**数值合理性断言**（例如毛利率落在合理区间、杜邦恒等式误差 < 1e-6、
  JSON 可被 `json.dumps(..., allow_nan=False)` 序列化），而不是只断言"没报错"。

---

## 2. 已实测确认的数据源契约（禁止凭记忆猜字段名！）

> 以下结论均由 `probe/` 会话在 **akshare 1.18.88** 上实测得出（2026-11 于贵州茅台 600519、
> 宁德时代 300750、招商银行 600036 三只标的上验证）。**任何字段名改动前必须重新实测。**

### 2.1 接口清单（东方财富年报口径，`symbol` 需带市场前缀）

| 用途 | 函数 | `symbol` 示例 | 返回规模 |
|---|---|---|---|
| 资产负债表（年度） | `ak.stock_balance_sheet_by_yearly_em` | `SH600519` / `SZ300750` | ~28 行 × 319 列 |
| 利润表（年度） | `ak.stock_profit_sheet_by_yearly_em` | 同上 | ~28 行 × 203 列 |
| 现金流量表（年度） | `ak.stock_cash_flow_sheet_by_yearly_em` | 同上 | ~26 行 × 254 列 |
| 财务指标（交叉校验） | `ak.stock_financial_analysis_indicator` | `600519`（**无前缀**） | 季度行 × 86 列 |
| 财务摘要（备用校验） | `ak.stock_financial_abstract` | `600519` | 报告期为列名 |

**重要**：`*_by_yearly_em` 三个接口返回的**全部是年报**（`REPORT_TYPE` 恒为 `'年报'`，
`REPORT_DATE` 恒为 `YYYY-12-31`），因此**不需要**再做季度累计值差分。
另有 `*_by_report_em`（报告期口径，含季报）与 `*_by_quarterly_em`（单季口径），本项目不使用。

### 2.2 关键公共字段
- `REPORT_DATE`：`'2024-12-31 00:00:00'`（字符串，需 `pd.to_datetime`）。
- `REPORT_DATE_NAME`：`'2024年报'`（可直接作为 `report_period`）。
- `SECURITY_NAME_ABBR`：股票简称；`ORG_TYPE`：**`通用` / `银行` / `证券` / `保险` / …** ——
  **这是判定"金融类公司"的官方依据，优于任何代码前缀猜测。**
- `NOTICE_DATE`：公告日。用于剔除"报告期已到但年报尚未披露"的占位行。
- `OPINION_TYPE`：审计意见类型（如 `标准无保留意见`）；银行股可能为 `None`。
- `CURRENCY`：`CNY`；**数值单位统一为「元」**，输出前必须 `/1e8` 换算为亿元。
- 每个科目都有同名的 `*_YOY` 列（单位：**百分数**，如 `-1.200097` 表示 -1.20%）。
  本项目**不直接采信**该列，而是自行用原始科目重算 YoY（铁律 1），仅作对照参考。

### 2.3 实测有效的核心科目（列名 → 中文）

**资产负债表**（金额，元）
`MONETARYFUNDS` 货币资金 · `ACCOUNTS_RECE` 应收账款 · `NOTE_RECE` 应收票据 ·
`NOTE_ACCOUNTS_RECE` 应收账款及票据合计 · `INVENTORY` 存货 · `PREPAYMENT` 预付款项 ·
`OTHER_CURRENT_ASSET` 其他流动资产 · `TOTAL_CURRENT_ASSETS` 流动资产合计 ·
`FIXED_ASSET` 固定资产 · `CIP` 在建工程 · `INTANGIBLE_ASSET` 无形资产 · `GOODWILL` 商誉 ·
`TOTAL_ASSETS` 资产总计 · `SHORT_LOAN` 短期借款 · `NONCURRENT_LIAB_1YEAR` 一年内到期的非流动负债 ·
`LONG_LOAN` 长期借款 · `BOND_PAYABLE` 应付债券 · `LEASE_LIAB` 租赁负债 ·
`SHORT_BOND_PAYABLE` 应付短期债券 · `ACCOUNTS_PAYABLE` 应付账款 · `CONTRACT_LIAB` 合同负债 ·
`TOTAL_CURRENT_LIAB` 流动负债合计 · `TOTAL_LIABILITIES` 负债合计 ·
`TOTAL_PARENT_EQUITY` 归属于母公司所有者权益合计 · `TOTAL_EQUITY` 所有者权益合计 ·
`MINORITY_EQUITY` 少数股东权益

**利润表**（金额，元）
`TOTAL_OPERATE_INCOME` 营业总收入 · `OPERATE_INCOME` 营业收入 · `OPERATE_COST` 营业成本 ·
`TOTAL_OPERATE_COST` 营业总成本 · `OPERATE_TAX_ADD` 税金及附加 · `SALE_EXPENSE` 销售费用 ·
`MANAGE_EXPENSE` 管理费用 · `RESEARCH_EXPENSE` 研发费用 · `FINANCE_EXPENSE` 财务费用 ·
`FE_INTEREST_INCOME` 财务费用-利息收入（**"存贷双高"检验的唯一可用利息收入口径**）·
`FE_INTEREST_EXPENSE` 财务费用-利息费用 · `ASSET_IMPAIRMENT_LOSS` 资产减值损失 ·
`CREDIT_IMPAIRMENT_LOSS` 信用减值损失 · `INVEST_INCOME` 投资收益 ·
`FAIRVALUE_CHANGE_INCOME` 公允价值变动收益 · `OTHER_INCOME` 其他收益 ·
`OPERATE_PROFIT` 营业利润 · `TOTAL_PROFIT` 利润总额 · `INCOME_TAX` 所得税费用 ·
`NETPROFIT` 净利润 · `PARENT_NETPROFIT` 归母净利润 · `MINORITY_INTEREST` 少数股东损益 ·
`DEDUCT_PARENT_NETPROFIT` 扣非归母净利润

**现金流量表**（金额，元）
`SALES_SERVICES` 销售商品、提供劳务收到的现金 · `NETCASH_OPERATE` 经营活动现金流量净额 ·
`NETCASH_INVEST` 投资活动现金流量净额 · `NETCASH_FINANCE` 筹资活动现金流量净额 ·
`CONSTRUCT_LONG_ASSET` 购建固定资产、无形资产和其他长期资产支付的现金（**资本开支 Capex**）·
`END_CCE` 期末现金及现金等价物余额 · `FA_IR_DEPR` 固定资产折旧等 ·
`IA_AMORTIZE` 无形资产摊销 · `INVENTORY_REDUCE` 存货的减少

### 2.4 已踩过的坑（务必规避）
1. `BASIC_EPS` 在年度接口中恒为 `0.0`，**不可用**；需要 EPS 请自行用净利润/股本计算或另取接口。
2. 资产负债表**没有** `INTEREST_RECE`（应收利息，新准则已并入其他科目），对多数公司恒为 `NaN`；
   利息收入只能走利润表 `FE_INTEREST_INCOME`。
3. `TRADE_FINASSET`（交易性金融资产）对不少公司为 `NaN`，不可作为"有息负债/金融资产"的唯一依据。
4. 银行股资产负债表**只有 221 列**（缺少 `INVENTORY` / `MONETARYFUNDS` / `TOTAL_CURRENT_ASSETS`
   等经营类科目），代码必须按"列可能不存在"来写，不能用硬索引。
5. **银行股利润表没有 `TOTAL_OPERATE_INCOME`（营业总收入）列**，只有 `OPERATE_INCOME`（营业收入）。
   因此所有"营业总收入"取数必须走 `_StatementView.total_revenue()` 的降级逻辑，
   否则整个成长性与杜邦维度会全部返回 null（已踩过此坑）。
6. `ak.stock_individual_info_em`（行业信息）在本环境**会因代理问题偶发 ProxyError**，
   不得作为主流程依赖；行业/金融属性判定一律用报表自带的 `ORG_TYPE`。
7. `stock_financial_analysis_indicator` 的 `销售毛利率(%)` 对白酒类返回 `NaN`，只能校验其他指标。
8. 同一接口的列名与其单位可能不一致：`经营现金净流量与净利润的比率(%)` 标注 `(%)`，
   实测存的是**倍数**（0.7212 即净现比 72.12%）。交叉校验必须逐列验证，不能只看列名。
9. 报告期已到但年报未披露时，接口仍会给出一行占位数据；必须用 `NOTICE_DATE <= 截止日`
   过滤，否则会把"未审计的空表"当成最新年度。
10. **上游数据可能自相矛盾**（实测 000858 五粮液 2025 年报）：利润表营业总收入 405.29 亿元，
    同期现金流量表"销售商品收到的现金"却是 914.00 亿元（收现/营收 = 2.26 倍），
    两表显然不匹配。已核实**每个 `REPORT_DATE` 只有一行数据**，因此不是去重/排序问题，
    而是上游数据源本身的脏数据。处置：`peer_comparison.py` 增加两条护栏
    （`PEER_YOY_ANOMALY=40%`、`CASH_REVENUE_MISMATCH_MAX=1.6`），只提示不删数，
    并要求下游 Agent 不得据此排优劣。

### 2.5 已完成的数值交叉验证（可作为回归基准）
以贵州茅台 600519 2025 年报为例，引擎计算值与外部指标源对比：

| 指标 | 引擎 | 外部源 | 结论 |
|---|---|---|---|
| 营业总收入 | 1720.5417 亿元 | 1720.542 亿元 | ✅ |
| 归母净利润 | 823.2007 亿元 | 823.2007 亿元 | ✅ |
| 销售净利率 | 50.528% | 50.5279% | ✅ |
| 总资产周转率 | 0.5602 次 | 0.5602 次 | ✅ |
| 存货周转率 | 0.2573 次 | 0.2573 次 | ✅ |
| 净现比 | 0.7212 | 0.7212 | ✅ |
| 毛利率（自算） | 91.18% | 公开年报 91.18% | ✅ |
| M-Score | -2.8351（阈值 -1.78） | — | ✅ 未触发嫌疑 |
| 杜邦恒等式误差 | 0.0 | — | ✅ 精确成立 |

回归基准断言已写入 `tests/test_financial_engine.py`：
`2024年报 营业总收入 = 1741.4407 亿元`、`2023年报 = 1505.6033 亿元`（容差 0.001）。

### 2.6 Phase 1.1 会计口径精修（已落地，属于**规则定义**，改动需同步本文档）

> 本节记录经 CPA 复核后修订的四项会计口径。它们不是代码细节，而是**判定逻辑的定义**，
> 后续任何修改都必须同步更新本节与对应测试断言。

#### (1) 受限货币资金勾稽：货币资金 vs 期末现金及现金等价物
会计原理：`MONETARYFUNDS`（货币资金）含库存现金、银行存款、其他货币资金；
`END_CCE`（现金流量表期末现金及现金等价物）只含可随时支取部分。
* `MONETARYFUNDS > END_CCE` → 差额近似为**受限货币资金**（保证金、质押冻结、定期存款、被占用资金）：
  `implied_restricted_cash_ratio = (MONETARYFUNDS - END_CCE) / MONETARYFUNDS`；
  若同时 `货币资金/总资产 ≥ 10%` 且该比例 `> 30%` → **触发**（severity=high）。
* `END_CCE > MONETARYFUNDS × 1.05` → 差额来自**合并范围内的财务公司/财务性子公司**
  将存放央行及同业款项列入"拆出资金/存放同业"等科目，属**列报口径差异而非资金风险**。
  此时必须显式排除受限资金判定（茅台 2025：1264.26 亿 vs 516.91 亿，2.45 倍，
  营业总收入与营业收入差额 32.16 亿元即财务公司利息收入）。
* **禁止**在 `END_CCE > MONETARYFUNDS` 时计算受限比例（差额为负，会把正常公司误判为资金异常）。
* `evidence` 新增键：`ending_cash_and_equivalents` / `restricted_cash` /
  `implied_restricted_cash_ratio` / `restricted_cash_high` / `cce_exceeds_cash` /
  `financial_company_hint` / `revenue_gap_total_minus_operating`。

#### (2) 纸面富贵：先过"重要性原则（Materiality）"再论增速差
应收账款增速高于营收增速 15pct 只是**必要条件**，还需满足
`应收账款 / 营业收入 ≥ 1%`（`RuleThresholds.min_receivables_to_revenue`）才构成**充分条件**。
理由：先款后货模式（白酒典型）应收基数极小（茅台 0.0015%），微小绝对波动会放大为百倍增速，
属统计噪音。被重要性门槛拦下时，必须在 `detail` 中写明拦截理由，不得静默丢弃。

#### (3) 非经常性损益：**先判方向，再判幅度**
`nonrecurring = 归母净利润 - 扣非归母净利润`，其符号决定完全相反的会计含义：

| 情形 | 判定 | severity |
|---|---|---|
| 归母 > 0 且 扣非 < 0 | **保壳扭亏**：主营实际亏损，靠非经常性损益"做成"盈利 | high |
| `nonrecurring > 0` 且 归母 > 0 且占比 > 30% | 常规依赖，主业质量打折 | medium |
| 归母 ≤ 0 且 `nonrecurring > 0` 且占比 > 30% | 亏损被非经常性净收益掩盖 | medium |
| `nonrecurring ≤ 0` 且 归母 > 0 | **非经常性净支出**：主业扎实、被一次性支出拖累，**不得预警** | info |

最后一行是最易犯的错：旧实现用 `abs(nonrecurring)/abs(归母)` 单向判断，
会把"扣非 > 归母"的优质公司误判为依赖非经常性损益。`evidence` 新增
`nonrecurring_direction`(income/expense/zero) / `is_reverse_turnaround` /
`is_nonrecurring_expense` / `loss_masked`；既有 `reliance` 键语义保持不变（绝对值口径）。

#### (4) 精度统一
* `YearGrowth` 全部 `*_yoy` 字段统一经 `round_ratio()`（6 位小数）。
* `evidence.debt_composition` 各明细统一经 `round_amount()`（4 位小数）。
* `detail` 中的人类可读金额统一 `:.4f`；极小百分比（< 0.01%）用 `_pct_text()` 提高精度，
  避免出现"占营业收入 0.00%"这类失真表述。

> 兼容性承诺：以上全部为 **evidence / thresholds 的追加键**，未改动任何既有键名或语义，
> 顶层 Schema 与 `YearGrowth` 字段集完全不变（`tests` 中有向后兼容断言）。

---

### 2.7 Patch 4.1：北交所 920 新号段 / 新三板兼容（2026-11-19 实测）

> 起因：实盘 `920012`（北交所新号段）与同业 `874886`（新三板）触发
> `TypeError: 'NoneType' object is not subscriptable`。已按铁律先探测再改码，结论如下。

| 代码段 | 板块 | 东方财富年度三表 | 巨潮年报 PDF |
|---|---|---|---|
| `6` / `0` / `3` / `2` | 沪深 A/B 股 | ✅ | ✅ |
| **`920xxx`** | **北交所（新号段）** | **✅ 实测 `BJ920012` 创达新材 10 行 × 319 列** | ⚠️ 视披露进度（920000 有 5 份） |
| `43x` / `83x` / `87x` / `88x` | 北交所旧号段 / 新三板 | ❌ 一律 NoneType（含 `stock_financial_abstract`） | ✅ 实测 430047/830799/870436 各 5 份 |
| `87xxxx` 新三板创新层（如 `874886`） | 新三板挂牌 | ❌ | ❌ topSearch 完全无记录 |

**四个根因与修复**

1. **映射错误（真 bug）**：旧实现 `_SH_PREFIXES = ("6", "9")` 把 `920012` 判成 `SH920012`
   → 上游 `result=None` → akshare 直接下标 → `TypeError`。
   **`920` 必须映射 `BJ`，只有 `900` 才是沪市 B 股**（`2xxxxx` 为深市 B 股）。
2. **旧号段无免费 fallback（事实）**：`43/83/87/88` 在东方财富三个年度接口与财务摘要接口上
   **全部返回空**，不存在"换个 reportName 就能取到"的通道 → 只能做业务级降级。
3. **巨潮 column 也判错**：`920xxx` 旧实现返回 `sse` → 检索 0 条；修正为 `bj` 后
   `920000` 可列出 5 份年报。`900xxx` 保持 `sse`。
4. **异常必须翻译**：`describe_unavailable()` 把上游异常归为三类并输出中文业务提示——
   ① 网络/代理失败；② 旧号段/新三板未收录（附两条可行出路）；③ 通用暂不可用。
   **任何提示中都不得出现 `NoneType` / `subscriptable`**（有断言守护）。

**降级行为契约**

* 单标的取数失败 → `DataUnavailableError`（中文提示），前端原样展示，绝不暴露底层类名；
* 同业混入不可用代码 → 自动记入 `PeerComparison.skipped`，主标的照常出报告，**绝不阻塞**；
* 旧号段虽无三表，但**巨潮仍收录其年报原文** → 提示用户上传 PDF 走附注穿透与即时检索
  （这是本项目相对纯数据工具的真实优势，必须在文案里说出来）。

**支持矩阵速查**：沪深主板/创业板/科创板 ✅、北交所 920 ✅、B 股 900/200 ✅、
北交所旧号段 43/83/87/88 与 新三板 87xxxx ⚠️（仅 PDF 通道）。

---

### 2.8 Patch 4.2：本地年报 PDF 三大表还原（`src/tools/pdf_financial_extractor.py`）

> 起因：`874886` 既无在线三表、也无巨潮 PDF，但用户手里有年报原件。
> 现在只要把 PDF 放进 `data/pdfs/`（或用工作台上传），全链路即可跑通。

**实测版式契约（874886 映日科技 2025 年报，147 页）**

| 事实 | 结论 |
|---|---|
| 报表标题 | `合并资产负债表` / `母公司资产负债表` / `合并利润表` / `母公司利润表` / `合并现金流量表` / `母公司现金流量表` 六个**独立成行**的标题，合并与母公司**交替排布** |
| 解析方式 | 先按标题把全文切成 6 段，**只在"合并"段内**匹配科目（否则会把母公司数字当合并数字） |
| 科目标签 | 会**跨行折断**（`归属于母公司所有者权益（或股东` + `权益）合计`、`2.归属于母公司所有者的净利润（净亏损以` + `“-”号填列）`）→ 由远及近拼接匹配 |
| 关键坑 | 折断标签的**末行**恰好等于另一个合法科目名（`所有者权益（或股东权益）合计`）→ **必须最长跨度优先**，否则归母权益被错记成所有者权益合计 |
| 金额格式 | 千分位 `1,234.56`、括号负数 `(1,234.56)`、空白 `-` → **None**（铁律 3，绝不填 0） |
| 摘要表 | P.8「主要会计数据和财务指标」是**唯一**能取到**扣非归母净利润**的地方；表头列号与数据行可能错位（合并单元格），按"行内第 1/2 个数值"取值 |
| 补充资料 | 折旧/摊销/存货减少在 `现金流量表补充资料`（P.125）里，且**必须从该小节起扫**——固定资产附注的「本期计提折旧」会先出现并污染结果 |
| 新三板措辞 | 摘要表用「归属于挂牌公司股东的净利润 / 净资产」，正文报表仍用「归属于母公司所有者」→ 两套措辞都要映射 |

**契约**

* 输出 `dict[table_key, DataFrame]` 与东方财富接口**完全同构**（`REPORT_DATE` / `SECURITY_NAME_ABBR` / EM 列名，单位统一为**元**），下游 `_normalize` → 引擎 → Agent 零改动；
* `meta.data_source.provider = "local_pdf_tables"`、`upstream = "本地年报 PDF · <文件名>"`、
  每表 `interface = "local_pdf:<文件名>"`，警告里写清页数与期次（铁律 2）；
* 覆盖率不足（缺资产负债表或利润表）时**放弃兜底**并保留原在线错误，绝不半套数据冒充成功；
* 上传/归档：`data/pdfs/` 里任意文件名的 PDF 会被识别代码与年度并重命名为 `{code}_{year}年报.pdf`，
  使 `list_local_pdfs` / `pdf_fetcher` 100% 命中。

**支持矩阵补充**：新三板 87xxxx、北交所旧号段 43/83/87 → **有年报 PDF 即可全链路分析**。

---

## 3. 目录结构与职责边界

```
FinSight-Agent/
├── AGENTS.md                  # 本文件：全局准则 + 数据源契约 + 编排/RAG/UI 契约（项目记忆）
├── README.md                  # 对外门面文档（架构图 / 技术壁垒 / 实盘案例 / 启动指南）
├── app.py                     # ★ Phase 4：Streamlit 投研工作台入口（双入口 + 5 标签页）
├── requirements.txt           # 运行依赖（当前锁定实测通过版本）
├── .env.example               # 环境变量模板（DEEPSEEK_API_KEY 等）
├── .gitignore
├── src/
│   ├── config.py              # 全局路径 / 缓存 TTL / 单位换算常量
│   ├── tools/                 # ★ 一切数值逻辑的唯一归属地（Phase 1）
│   │   ├── schemas.py         # Pydantic v2 结构化模型（唯一数据契约）
│   │   ├── diagnostics.py     # 结构化警告收集器 + 数值安全工具
│   │   ├── financial_data.py  # 数据获取 + CSV/JSON 本地缓存 + 列名归一
│   │   ├── financial_engine.py# ★ 核心引擎：成长/杜邦/现金流质量/会计排雷
│   │   ├── cross_validation.py# 与外部指标源的独立交叉校验（可选，默认关闭）
│   │   └── peer_comparison.py # 同业横向对标（复用 analyze_stock + 缓存）
│   ├── agents/                # ★ Phase 2：多智能体编排
│   │   ├── context_formatter.py  # 零歧义 Fact Pack（比率→%、金额→亿元）+ 锚点 + 警告聚合
│   │   ├── llm_client.py         # DeepSeek（openai 兼容）客户端：密钥脱敏 / JSON 容错 / 用量记账
│   │   ├── prompts.py            # 提示词与 JSON 契约（必选章节的唯一来源）
│   │   ├── fact_check.py         # 确定性数值质检：章节/锚点/符号/单位/报告期/页码/必转述
│   │   ├── nodes.py              # 6 个节点 + 条件边路由（AgentState 定义于此）
│   │   ├── graph.py              # StateGraph 装配 + 编排器 + CLI + 产物落盘
│   │   └── offline_templates.py  # 无密钥时的确定性降级模板（CI 可用）
│   ├── rag/                   # ★ Phase 3：年报 PDF 附注 RAG
│   │   ├── pdf_fetcher.py     # 双通道获取：data/pdfs 优先 + 巨潮 cninfo 下载
│   │   ├── pdf_parser.py      # 附注感知解析：章节定界 / 表格保留 / 页码溯源 / 切片缓存
│   │   └── retriever.py       # 科目路由 + jieba/BM25 混合检索（无 Embedding API）
│   └── ui/                    # ★ Phase 4：前端层（只读，不算）
│       ├── theme.py           # 券商深蓝/石板灰/琥珀金/警示红 配色 + 全局 CSS
│       ├── charts.py          # 7 类 Plotly 交互图（成长/瀑布/现金流/M-Score/同业）
│       ├── exporter.py        # 自包含打印级 HTML（纯 Python，内联 CSS + A4 分页）
│       ├── components.py      # KPI 条 / 排雷规则卡片 / 附注证据卡片 / 可观测性面板
│       └── state.py           # 历史存档扫描 / 报告重建 / 检索器缓存 / 派生统计
├── tests/
│   ├── test_financial_engine.py  # Phase 1 实盘闭环验证（56 项断言）
│   ├── test_agent_workflow.py    # Phase 2/3 实盘闭环验证（89 项断言）
│   ├── test_rag_pipeline.py      # Phase 3 RAG 流水线验证（26 项断言）
│   ├── test_ui_smoke.py          # Phase 4 UI 冒烟验证（78 项断言）
│   └── test_market_compat.py     # Patch 4.1 市场兼容（离线 70 / 联网 78 项断言）
└── data/
    ├── cache/statements/      # 原始三表 CSV 缓存 + JSON 元数据（不入库）
    ├── cache/rag/             # 解析后的 Chunk 库（*.json，不入库）
    ├── pdfs/                  # 年报 PDF（不入库，可由巨潮自动下载）
    └── reports/               # JSON / Markdown / HTML 报告、运行轨迹、事实包（不入库）
```

**依赖方向（单向，禁止反向）**：
`schemas.py` ← `diagnostics.py` ← `financial_data.py` ← `financial_engine.py` ← `peer_comparison.py`
← `context_formatter.py` ← `fact_check.py` ← `nodes.py` ← `graph.py` ← `tests/`

`src/rag/*` 只被 `nodes.footnote_rag_verifier` 单向调用；**`rag` 不得反向 import `agents`**，
也不得出现任何财务计算（它只做"取文本 + 匹配文本"）。

**铁律 1 在 Phase 2 的延伸**：`agents/` 内不允许出现 `/ 1e8`、复利、加总等财务运算；
所有数字只能来自 `FinancialReport`，或由 `context_formatter` 做**纯格式化**（×100 加 %）。

---

## 4. 工程约定

### 4.1 单位与精度（最容易出错的地方）
- **所有金额输出统一为「亿元」**，保留 4 位小数（数据源为「元」，除以 `1e8`）。
- **所有比率输出统一为「小数」**，不做 ×100（`0.9193` = 91.93%）。报告根部 `meta.unit_notes` 显式声明。
- 内部计算全程使用原始精度，**只在构造输出模型时**做 rounding，禁止中途四舍五入。

### 4.2 代码风格
- Python 3.10+，全量类型注解（`from __future__ import annotations`）。
- 中文会计注释说明**每个比率的会计含义与公式**；公式字符串要能直接给到 LLM 做解释。
- 不引入除 `akshare / pandas / numpy / pydantic / python-dotenv` 之外的重依赖。
- 新增指标必须同时补：① Pydantic 字段 ② 计算函数 ③ 测试断言 ④ 本文件的字段说明。

### 4.3 缓存策略
- 原始三表缓存为 `data/cache/statements/{code}_{table}.csv` + `{code}_{table}.meta.json`。
- 默认 TTL 12 小时（`src/config.py::DEFAULT_CACHE_TTL_HOURS`）；`force_refresh=True` 可强制刷新。
- 缓存只缓存**原始数据**，绝不缓存计算结果（避免阈值调整后读到旧结论）。

### 4.4 验证协议（每个模块的 Definition of Done）
```powershell
python tests/test_financial_engine.py          # Phase 1：600519 全链路（56 项断言）
python -m src.tools.financial_engine 600519    # CLI 冒烟

python tests/test_agent_workflow.py            # Phase 2/3：多智能体 + 附注穿透（89 项断言）
python tests/test_agent_workflow.py --offline  # 无密钥时的确定性回归
python tests/test_rag_pipeline.py              # Phase 3：PDF → 切片 → 检索（26 项断言）
python tests/test_ui_smoke.py                  # Phase 4：图表 + HTML 导出 + AppTest（78 项断言）
python tests/test_ui_smoke.py --skip-app       # 跳过 AppTest（更快的纯函数验证）
python tests/test_market_compat.py             # Patch 4.1：北交所/新三板映射与降级（离线）
python tests/test_market_compat.py --online    # 追加 920012 实盘与 874886 降级验证
python -m src.agents.graph 600519 --peers 000858,000568   # 手工跑一份纪要
python -m src.agents.graph 600519 --no-rag                 # 关闭附注穿透
streamlit run app.py                                       # 前端工作台（http://localhost:8501）
```
必须满足：① 零报错；② 数值合理性断言全绿；③ JSON 通过 `allow_nan=False`；
④ 交叉校验偏差在容差内；⑤ 金融类（如 600036）走降级路径时给出明确"不适用"提示而非崩溃；
⑥ **Phase 2 追加**：`fact_check` 通过、`footnote_queries` 非空且命中触发规则对应科目、
产物中不得出现密钥字符串；
⑦ **Phase 3 追加**：切片页码合法、目标科目 Top-3 命中、`rag_evidences` 页码必须落在
检索证据范围内、报告正文必须引用 `P.xx`；
⑧ **Phase 4 追加**：7 类 Plotly 图 trace 非空、杜邦瀑布三因子之和 = ROE 实际变动（容差 0.01pct）、
HTML 自包含且无密钥、AppTest 5 个标签页渲染无异常、`src/ui/*` 无 `1e8` 单位换算。

---

## 5. Phase 2 多智能体编排契约（`src/agents/`）

> 本节是编排层的**架构契约**，与 §2 的数据源契约同等地位：修改拓扑、状态通道、
> 质检规则或 `footnote_queries` 结构都必须同步更新本节与 `tests/test_agent_workflow.py`。

### 5.1 工作流拓扑

```
START
  │
  ▼
data_loader ── analyze_stock() + compare_peers() → FinancialReport + FactPack
  │
  ├─────────────────┬─────────────────┐        ← 并行 fan-out（同一 superstep）
  ▼                 ▼                 ▼
fundamental_    forensic_        peer_valuation_
 analyst          auditor           analyst
                    │                 │
                    ▼                 │        ← 审计疑点 → 附注穿透（Phase 3）
            footnote_rag_verifier     │
                    │                 │
  └─────────────────┴─────────────────┘        ← fan-in：add_edge([a,b], "chief_editor")
                    ▼
              chief_editor ── 六章节 Markdown 纪要
                    ▼
              fact_checker ── 纯 Python 质检
                    │
       ┌────────────┴────────────┐
       ▼ rewrite                 ▼ accept
  chief_editor                 END
  (retry_count < 2)
```

**并行语义**：三个分析节点由 `data_loader` 在同一条边集合上触发，处于同一 superstep；
`footnote_rag_verifier` 由 `forensic_auditor` 触发，与基本面/同业两支并行推进；
fan-in 采用 `add_edge([...], target)` 的"等待全部"写法，因此 `chief_editor` 只会执行一次。

### 5.2 `AgentState` 通道约定（`src/agents/nodes.py`）

| 通道 | 写入方 | 归并方式 |
|---|---|---|
| `stock_code` / `peer_codes` / `lookback_years` | 调用方（初始 state） | 覆盖 |
| `report` / `fact_pack` / `anchors` / `peer_comparison` | `data_loader` | 覆盖 |
| `fundamental` / `forensic` / `peer_analysis` | 三个并行节点各写各的键 | 覆盖（键不冲突） |
| `report_markdown` / `audit_feedback` / `retry_count` / `fact_check` / `next_action` | `chief_editor` / `fact_checker` | 覆盖 |
| **`usage` / `trace` / `warnings`** | 多个节点同时写 | **`Annotated[list, operator.add]`** |

> 并行节点**禁止**写同一个非归并通道，否则后写覆盖先写（LangGraph 会抛 `InvalidUpdateError`）。
> 新增需要并行累加的信息时，必须加到 `trace`（或新建带 reducer 的通道）。

### 5.3 节点职责（不可越界）

| 节点 | 输入 | 输出 | 禁止事项 |
|---|---|---|---|
| `data_loader` | 股票代码 | `report` / `fact_pack` / `anchors` / `peer_comparison` | 不做任何 LLM 调用 |
| `fundamental_analyst` | FactPack | JSON：趋势、毛利、**杜邦三因子归因**、现金流含金量 | 不重算数字、不给评级 |
| `forensic_auditor` | FactPack | JSON：M-Score 变量点评、6 条规则复核、**`footnote_queries`** | 不得把财务公司口径差异误判为受限资金 |
| `peer_valuation_analyst` | FactPack（含第 9 节） | JSON：四维度对标；无同业时改历史分位 | 无同业时禁止编造行业均值 |
| `footnote_rag_verifier` | `forensic.footnote_queries` | `rag_evidences`（含 `P.xx` 页码与 verdict）+ `rag_meta` | 检索失败必须退化，不得阻塞 |
| `chief_editor` | FactPack + 三份专家结论 + 附注证据 + 可选反馈 | 六章节 Markdown | 不得引入 FactPack 之外的数字 |
| `fact_checker` | Markdown + anchors | `fact_check` / `next_action` | **纯 Python，禁止调用 LLM 自查** |

### 5.4 零歧义事实包规范（`context_formatter.py`）

所有比率必须经 `fmt_pct()` 转成带 `%` 的字符串；金额经 `fmt_amount()` 带「亿元」；
周转率带「次」；天数带「天」；百分点差用 `pct`。**这是 Phase 2 最重要的防幻觉设计**：

| 引擎原始值 | 事实包呈现 | 禁止出现的形态 |
|---|---|---|
| `-0.012001`（营收同比小数） | `-1.20%` | `-0.0120`（会被误读为 -0.012%） |
| `0.911796`（毛利率小数） | `91.18%` | `0.9118` |
| `1720.5417`（亿元） | `1720.5417 亿元` | `172054170000 元` |
| `0.5602`（周转率） | `0.5602 次` | `56.02%` |

`warnings` 必须经 `aggregate_warnings()` 去重聚合（同类缺失科目合并为一行），
上限 12 行，避免几十条同质警告稀释模型注意力。

### 5.5 `fact_checker` 五类校验与重试语义

| 校验 | 判定方式 | 阈值 |
|---|---|---|
| 章节完整性 | 六个 `## 一/二/…、标题` 齐备、序号关键词正确、正文非空 | 每节 ≥ 80 字符，全文 ≥ 1200 字符 |
| 核心锚点 | 最新年度 8 个锚点必须出现且量级正确 | `max(0.01, |值|×0.1%)` |
| 符号方向 | 同一量级只出现反号 → `sign_flipped` | — |
| 单位 | 出现「万元」即判错；金额只允许「亿元」或「元」 | 微额（< 0.01 亿元）用元，如 `1,283,984.83 元` |
| 报告期 | 出现事实包之外的「20XX年报」即判错 | — |

**重试语义**：`retry_count < 2` 时把 `fact_check.errors` 写入 `audit_feedback`
并打回 `chief_editor` 重写（携带上一版全文，便于定位）；达到上限则记录 `warnings` 后放行，
**绝不无限循环**。`retry_count` 在 `fact_checker` 内自增，因此上限判断始终一致。

> **单位红线的由来（实测踩坑）**：Phase 3 的附注微额（茅台存货跌价准备 128.4 万元
> = 1,283,984.83 元）若换算成亿元会退化为 `0.0000 亿元`，模型因此改写为「万元」，
> 触发 `unit_wan_yuan` 并连改两次仍未通过。现行规则是：**常规金额用亿元、
> 不足 0.01 亿元的微额直接用元、万元一律禁止**（含"原文括注"场景，
> 1 万元 = 10,000 元先换算成元再括注）。错误信息必须给出**具体位置片段 + 换算示例**，
> 否则模型改不动。

### 5.6 `footnote_queries` 契约（Phase 3 RAG 的标准输入）

`forensic_auditor` 必须输出 `footnote_queries: list[dict]`，每项字段固定为：

| 字段 | 类型 | 含义 | 示例 |
|---|---|---|---|
| `target_account` | str | 待核查的报表科目或附注主题 | `存货` |
| `risk_reason` | str | 为什么查（关联到哪条规则/变量） | `存货周转率连续两年下滑且占比抬升` |
| `suggested_rag_query` | str | 可直接检索年报附注的自然语言查询句 | `年报附注-存货分类明细：基酒/半成品与成品酒构成、跌价准备计提比例` |
| `priority` | str | `high` / `medium` / `low` | `high` |

**硬性要求**：凡触发的规则必须有对应查询；另加至少 2 条常规核查项
（货币资金 / 收入确认 / 有息负债）。Phase 3 直接消费此结构，**不得改字段名**。

### 5.7 安全红线（密钥）

* 密钥只经 `LLMConfig.from_env()` 读入内存，`LLMConfig.api_key` 字段 `repr=False`；
* 所有异常信息必须过 `mask_secret()` 再输出，任何形如 `sk-xxxx` 的字符串都会被替换为 `sk-****`；
* `status_text()` 只输出 `已配置(len=N)`，禁止输出前缀/后缀；
* 测试脚本 `test_agent_workflow.py` 会对所有产物做 `sk-[A-Za-z0-9]{16,}` 扫描断言。

### 5.8 离线降级模式（`FINSIGHT_LLM_MODE=offline`）

未配置密钥或显式指定 offline 时，`DeepSeekClient` 返回**确定性模板**响应
（`offline_templates.py`）：数值直接搬运 FactPack 中的表格，因此天然通过锚点校验。
用途：CI 回归、无网络环境演示、图结构调试。**产物会显式标注 `mode=offline`**，不得冒充 LLM 分析。

### 5.9 性能与成本基线（实测，2026-11-18，deepseek-chat）

| 场景 | 墙钟耗时 | Token（prompt / completion / 合计） | LLM 调用 | 质检重写 |
|---|---|---|---|---|
| 单股 `--no-peers` | 32.9s | 33,363 / 11,720 / **45,083** | 4 | 0 |
| 带同业（000858、000568） | 32.0s | 35,121 / 11,905 / **47,026** | 4 | 0 |
| 离线 `--offline` | 0.3s | 0 | 0 | 0 |

* 事实包约 9.7k 字符 ≈ 6.1k tokens（含 4 年原始科目快照），是 prompt 成本主体。
* **并行收益**：三个专家节点各自耗时 8~20s，并行后整体 ~32s（≈ 最慢节点 + 撰稿），
  而非三者之和；这是选 LangGraph fan-out 的主要理由。
* 单份纪要成本量级 < ¥0.1（DeepSeek 官方价），适合批量跑行业池。

### 5.10 Phase 2 已踩坑（务必规避）

1. **`max_tokens=4096` 会硬截断**：六章节 Markdown 在第五章被切断，
   `forensic` 的长 JSON（8 变量 + 6 规则 + 9 条 footnote_queries）同样会被切断并导致解析失败。
   现已统一 8192，并把 `finish_reason` 记入 `usage` / `trace`；出现 `length` 立即告警。
2. **截断 → JSON 解析失败 → 静默降级**是最危险的组合：报告看起来完整，但 Phase 3 接口丢了。
   现在的策略是：先做一次"压缩输出"重试；`forensic` 节点为重试仍失败则 **raise
   `AgentExecutionError` 中止**，绝不用一个缺字段的报告冒充成功。
3. **模型会"看到但没写"**：同业数据可信度护栏在事实包里，但首版报告没转述。
   此类关键提示不能只靠提示词，已通过 `fact_check` 的 `required_phrases` **确定性强制转述**。
4. **同业数据源可能异常**：实测 000858 五粮液 2025 年报营收 891.75 亿 → 405.29 亿（-54.55%），
   且 2024 年报行被同期重述，不符合行业常识。已加 `PEER_YOY_ANOMALY=40%` 护栏：
   只做提示、不删数据（铁律 3），并要求报告不得据此排优劣。
5. **`.env` 命名坑**：Windows 用"新建文本文档"改名会得到 `新建 文本文档.env`，
   `dotenv` 只认 `.env`。`LLMConfig.from_env()` 现已显式加载 `<项目根>/.env` 并清洗成对引号。

---

## 6. Phase 3 年报附注 RAG 契约（`src/rag/`）

> 与 §2、§5 同等地位：修改解析规则、检索打分或 `rag_evidences` 结构，
> 都必须同步更新本节与 `tests/test_rag_pipeline.py`、`tests/test_agent_workflow.py`。

### 6.1 三件套职责与依赖

| 模块 | 职责 | 禁止事项 |
|---|---|---|
| `pdf_fetcher.py` | 本地 `data/pdfs/` 优先 → 巨潮 `hisAnnouncement/query` 下载 | 失败**必须返回 None**，不得抛错阻塞主流程 |
| `pdf_parser.py` | 章节定界 / 表格保留 / 页码溯源 / 切片缓存 | 不做检索，不改数字 |
| `retriever.py` | 科目路由 + jieba/BM25 混合打分 | **禁止引入 Embedding API**；禁止写财务计算 |

### 6.2 PDF 期次对齐（铁律 2）

* 文件名约定 `{code}_{year}年报.pdf`；`_infer_year()` 从文件名解析年度。
* 目标年度未披露 → 自动回退最近一期，并把差异写入 `PdfArtifact.period_mismatch_note`。
* `rag_evidences` 中的 `pdf_period` **一律以真实 PDF 期次覆盖模型输出**；
  若 `pdf_period != 财务数据最新期次`，必须置 `period_mismatch=true` 并在第六章首行声明。
* 标题黑名单（`摘要/英文版/更正前/已取消/补充/问询/ESG/内控`）必须过滤，
  否则会把 20 页摘要当成年报。

### 6.3 解析契约（`pdf_parser.py`）

* **层级**：`第X节` > `一、` > `1、` > `(1)`，四级路径写入 `section_path`（`" > "` 连接）。
* **编号单调性**：同级标题编号必须递增（L1/L2/L3；L4 允许重复）。
* **锚点强制接受**：`合并财务报表项目注释` / `母公司财务报表项目注释` / `财务报表附注`
  命中即接受并**锁死 L2**，直到 `第X节` 切换。
  > 不这么做会被利润表行标签（`七、综合收益总额`、`八、每股收益`）把 L2 计数器顶高，
  > 导致真正的 `七、合并财务报表项目注释` 被单调性检查拒绝 —— 实测踩坑，附注归属全丢。
* **表格**：`page.find_tables()` 抽表 → Markdown → 按 y 坐标插回正文流（同页可能含多个科目）。
  `table_mode="core"` 只对核心区域抽表（约 0.4s/页）。
* **页码**：`page_num`（1-based 起始页）/ `page_end`；`page_label` 渲染为 `P.91` 或 `P.91-92`。
* **切片落盘归属**：`flush()` 必须使用**切换标题前**的快照，否则切片会被贴上下一节标题
  （实测踩坑：`货币资金` 的表格被标成 `拆出资金`）。
* **缓存**：`data/cache/rag/{stem}_chunks.json`，签名含 `PARSER_VERSION` + PDF 大小 + mtime +
  `table_mode`，任一变即失效。

### 6.4 检索契约（`retriever.py`）

两路融合，权重固定：

```
final = 0.62 × route + 0.38 × bm25_norm        (+0.06 表格问题命中含表切片, +0.04 note 切片)
```

* **route**（第 1 路）：`target_account` 命中 `section_path/section_title` → 1.0；
  同义词命中 → 0.85；正文前 300 字命中 → 0.55；全文命中 → 0.3。
* **bm25_norm**（第 2 路）：`jieba` 分词（注入 `_ACCOUNTING_WORDS` 会计词典）+
  `rank_bm25.BM25Okapi`，按本次查询集的最大分归一化到 [0,1]。
* **同义词表** `_ACCOUNT_SYNONYMS` 是精度关键（如"有息负债"→ 短期借款/长期借款/应付债券/
  一年内到期的非流动负债/租赁负债）。新增科目务必同步补充。
* 默认只检索 `core_chunks`（kind ∈ note/statement/mda），外围章节不参与，降低噪声。

### 6.5 `rag_evidences` 契约（Phase 4 前端直接消费）

```jsonc
{
  "pdf_period": "2025年报",
  "financial_period": "2025年报",
  "period_mismatch": false,
  "period_note": "",
  "evidences": [
    {
      "target_account": "存货",
      "verdict": "verified_benign",        // verified_benign | verified_risk | inconclusive
      "finding_summary": "…含附注具体数字…",
      "citations": [
        {"pdf_period": "2025年报", "page": "P.91", "page_num": 91,
         "quote": "在产品 25,664,652,201.49 …", "verified": true}
      ]
    }
  ]
}
```

`rag_meta` 记录 `pdf_path / pdf_period / page_count / chunks / note_chunks /
chunks_with_table / parse_cache_hit / retrieval_ms / top_pages`，供前端与排障使用。

### 6.6 确定性护栏（不依赖模型自觉）

1. **页码必须在证据范围内**：`citation.page` 解析出的页码若不在该科目检索命中的
   `page_num..page_end` 集合内 → `verified=false`，且该条结论**一律降级为 `inconclusive`**
   （无论原来是 benign 还是 risk）。
2. **报告必须引用页码**：`fact_check` 的 `required_page_labels` 收集所有 `verified=true`
   的页码，任一未出现在 Markdown 中即判错 → 触发条件边重写。
3. **缺失科目补齐**：模型漏答的 `target_account` 自动补一条 `inconclusive`，不静默丢弃。
4. **失败即退化**：`--no-rag`、无 PDF、解析失败、检索为空、LLM 失败 →
   `rag_evidences.evidences=[]` + `warnings` 说明原因，工作流继续。

### 6.7 性能基线（实测，2026-11-18，600519 年报）

| 环节 | 耗时 | 说明 |
|---|---|---|
| 巨潮下载 | 14.0s | 1.0 MB / 143 页（一次性，落 `data/pdfs/`） |
| 冷解析 | 33~52s | 143 页 → 321 切片（附注 164 / 经营 30 / 含表 176） |
| 缓存加载 | 38 ms | `*_chunks.json` |
| 建索引 | 1.7~3.9s | jieba 分词 197 个核心切片 + BM25 |
| 单条检索 | 2~5 ms | Top-3 |
| RAG 核查节点 | 10~11s / ~14k tokens | 8 条疑点一次 LLM 调用 |
| 完整工作流（含 RAG） | 42.9s / 63,572 tokens | 5 次 LLM 调用（专家 3 + 核查 1 + 撰稿 1） |

检索实测命中（Top-1）：存货 → `P.91 存货分类`；货币资金 → `P.84 货币资金`；
合同负债 → `P.102-103 合同负债情况`；营业收入确认 → `P.107-108 履约义务的说明`。

### 6.8 Phase 3 已踩坑（务必规避）

1. **`第X节` 与标题分行**：PDF 中 `第八节` 与 `财务报告` 常分两行 → 必须"挂起 + 合并"。
2. **表格行标签伪装标题**：`四、账面价值`/`一、账面原值` 会覆盖 L2 → 黑名单 + 编号单调性。
3. **利润表行标签顶高 L2 计数器**：`七、综合收益总额`/`八、每股收益` 使 `numbers[2]=8`，
   真正的 `七、合并财务报表项目注释` 被拒 → 锚点强制接受 + 锁死 L2。
4. **flush 时序错误**：先 `feed()` 更新层级再 `flush()`，切片会被贴上下一节标题（已修）。
5. **jieba 初始化写 stderr**：在 PowerShell 管道里会被误判为错误 → `jieba.setLogLevel(ERROR)`。
6. **`END_CCE` 与货币资金勾稽的联动**：Phase 1.1 的"财务公司列报差异"结论，
   在 P.84/P.102/P.116 得到附注原文证实（受限 72.5 亿系存放央行法定存款准备金、
   期末现金等价物 1264.26 亿含存放同业 741 亿 + 买入返售 84.7 亿）——
   这正是 RAG 的价值：把"规则猜测"升级为"附注实证"。

---

## 7. Phase 4 前端与导出契约（`app.py` + `src/ui/`）

> 与 §2、§5、§6 同等地位：修改 UI 数据流、HTML 模板或 `YearDuPont` 派生字段，
> 都必须同步更新本节与 `tests/test_ui_smoke.py`。

### 7.1 分层与职责

| 模块 | 职责 | 禁止事项 |
|---|---|---|
| `src/ui/theme.py` | 配色常量 + 全局 CSS（券商深蓝 `#1F3A5F` / 石板灰 / 琥珀金 `#C9A227` / 警示红 `#A93226`） | 不做业务逻辑 |
| `src/ui/charts.py` | 7 类 Plotly 图：成长柱+双轴折线、**杜邦三因子瀑布**、现金流含金量、M-Score 八变量、M-Score 雷达、M-Score 仪表、同业分组图 | **禁止重算财务指标**；`None` 必须跳过而非补 0 |
| `src/ui/exporter.py` | 自包含打印级 HTML（内联 CSS + `@media print` A4）；Markdown/JSON 下载载荷 | **禁止引入 weasyprint / wkhtmltopdf** |
| `src/ui/state.py` | 历史存档扫描、`analyze_stock` 报告重建（带缓存）、检索器缓存、派生统计 | 不解释财务语义 |
| `src/ui/components.py` | KPI 条、质检徽章、排雷规则卡片、附注证据卡片、可观测性面板 | 不做财务计算 |
| `app.py` | 双入口编排 + 5 标签页 | 不直接调 `analyze_stock`（统一走 `state`） |

### 7.2 双入口设计（演示友好）

* **历史实盘回放**：扫描 `data/reports/*_agent_trace.json`，配对 `*_tearsheet.md`；
  `trace` 不含完整 `FinancialReport`，因此用 `analyze_stock`（CSV 磁盘缓存）重建，
  实测 **6 ms**、整个 AppTest 渲染 4.1s，**零 Token**。
* **实时深度解构**：`AgentWorkflow.run(..., on_event=callback)` 走
  `graph.stream(stream_mode="updates")`，逐节点回调驱动 `st.status`；
  `_stream()` 手工合并状态时，`usage / trace / warnings` 必须**拼接**，其余键覆盖（与 reducer 语义一致）。

### 7.3 杜邦三因子贡献落库（铁律 1 的关键延伸）

瀑布图需要"净利率/周转率/权益乘数各自拉动多少 pct"。这些值**必须由引擎计算**：

* `financial_engine._dupont_effects(latest, previous)` 统一实现连环替代；
* `_compute_dupont` 把结果写入 `YearDuPont.net_margin_effect / asset_turnover_effect /
  equity_multiplier_effect`（小数口径，与全项目一致）；
* `_dupont_driver` 复用同一函数生成文案，**避免两处实现漂移**；
* 前端 `charts.dupont_waterfall_figure` 只读这三个字段 → `measure=[absolute, relative×3, total]`。

> 实测校验：三因子之和与 `roe_delta` 误差 `1e-6`（round_ratio 6 位小数导致）。

### 7.4 HTML 导出的四条硬要求

1. **自包含**：CSS 内联、无外链、无 CDN（`"https://" not in html` 是断言项）；
2. **打印优化**：`@page { size: A4; margin: 15mm 13mm 16mm 13mm; }`，表头 `break-after: avoid`、
   表格行 `break-inside: avoid`；
3. **四层一致**：HTML / Markdown / 事实包 / 页面四处显示必须完全相同，
   统一调用 `context_formatter` 的 `fmt_*`（不得在 UI 里自己写 `f"{x*100:.2f}%"`）；
4. **零密钥**：`sk-[A-Za-z0-9]{16,}` 扫描必须为空（注意 CSS class `risk-high` 会造成朴素 `"sk-"` 误报，
   断言要用正则而非子串）。

### 7.5 Streamlit 的四个坑（已修）

1. **`st.dataframe` 与 pyarrow**：同一列混用 `""` 与 `float` 会抛
   `ArrowInvalid: Could not convert '' ... to double` → 数值列统一用 `None` 占位。
2. **`st.metric` 与自定义 HTML**：KPI 条是 HTML 渲染，`AppTest` 里不会出现在 `at.metric` 中，
   断言应检查 `at.markdown` 文本而非 `at.metric`。
3. **`width="stretch"` 版本差异**：新版 Streamlit 用 `width`，旧版用 `use_container_width`，
   `components._dataframe` 做了 TypeError 回退。
4. **改 `src/` 下任何模块后必须重启服务**（实测踩坑）：Streamlit 热重载只重新执行主脚本
   `app.py`，`sys.modules` 里已加载的 `src.ui.*` / `src.tools.*` **不会重新导入**。
   于是会出现"磁盘上明明有这个函数，页面却报
   `AttributeError: module 'src.ui.state' has no attribute 'xxx'`"的假故障
   （本仓库真实发生过：Patch 4.2 新增 `adopt_local_pdfs` 后未重启）。
   处置：① 改动 `src/**` 后 `Ctrl+C` 再 `streamlit run app.py`；
   ② `app.py` 对新依赖用 `getattr(module, "name", None)` 降级并提示重启，
   禁止让整页崩溃；③ 排查顺序永远是"先确认进程新旧，再怀疑代码"（`state.__file__`
   + `hasattr` 一行即可判定）。

### 7.6 性能基线（实测）

| 场景 | 耗时 | 说明 |
|---|---|---|
| 历史回放（AppTest 全量渲染） | 4.1s | 含报告重建、7 图、5 标签页 |
| 报告重建（`analyze_stock` + 缓存） | 6 ms | 命中 CSV 缓存 |
| 图表构建（7 类合计） | < 0.3s | 纯 Plotly，无 IO |
| HTML 导出 | ~40 ms | 30 KB / 9 表格 |
| Streamlit 服务启动 | ~10s | `streamlit run app.py` |
| 附注即时检索 | 2~5 ms/次 | 复用 Phase 3 的 `HybridRetriever` |

---

## 8. 路线图（严格按序，未获批不得提前）

- **Phase 1（已完成）**：脚手架 + 数据层 + 计算引擎 + 实盘验证。
- **Phase 1.1（已完成）**：会计口径精修补丁 —— 受限货币资金勾稽 / 财务公司列报差异识别 /
  应收账款重要性门槛 / 非经常性损益方向与保壳扭亏识别 / 精度统一（见 §2.6）。
- **Phase 2（已完成）**：LangGraph 多智能体编排 —— `data_loader` → 三专家并行 → 撰稿 → 质检
  + 条件边重写（见 §5）。
- **Phase 3（已完成）**：年报 PDF 附注解析 + 穿透式混合 RAG（见 §6），
  闭环"触发预警 → 提追问 → 翻附注取证 → 结案"。
- **Phase 4（已完成）**：Streamlit 投研工作台 + 打印级 HTML 导出 + 旗舰级 README（见 §7）。
- **Patch 4.1（已完成）**：北交所 920 新号段兼容 + 新三板/旧号段友好降级（见 §2.7）。
- **Phase 5**：多维打分卡与同业样本池扩展（行业分类 + 市值分层动态生成）。

---

## 9. 已知待办（不影响验收）

1. **北交所旧号段（43/83/87/88）与新三板（87xxxx）无免费标准化三表**：已实测确认
   东方财富年度三表与财务摘要接口一律返回空（见 §2.7），当前仅能做业务级降级 +
   提示走 PDF 附注通道。若后续需要真正支持，需接入付费数据源或自建三表抽取
   （从年报 PDF 反抽资产负债表，工作量大且准确率难保证）。
2. `营业总收入` 与 `营业收入` 双口径并行输出，下游 Agent 需明确选用哪一个（已在字段描述中标注）。
3. `peer_comparison` 的同业池目前是硬编码字典，Phase 5 应改为「行业分类 + 市值分层」动态生成。
4. `fact_checker` 的锚点覆盖最新年度核心指标；若未来报告要求引用历史年度数字，
   需把锚点扩展为"每年份一组"，并同步调整容差。
5. 解析器目前只对"核心区域"抽表（`table_mode=core`）；若需覆盖全部页面，
   冷解析耗时将从 ~35s 升至 ~60s（143 页 × 0.4s），需权衡。
6. `_ACCOUNT_SYNONYMS` 已扩展到 26 组（覆盖研发费用/开发支出/固定资产/递延所得税/减值/
   应付票据/其他应收款等科技制造医药高频科目），但仍是白名单机制：
   非白名单科目只能靠 BM25 召回，新增行业时需按需补充。
7. 历史回放的图表由 `analyze_stock` 重建，若上游数据在该存档之后被刷新，
   页面数字可能与存档时点的 Markdown 存在细微差异（已在 UI 显式提示，未做数据版本快照）。
8. `src/ui/state.py` 的 `st.cache_data(ttl=1800)` 在长会话中会过期重算，
   若届时网络不可用且缓存失效，回放会退化为报错提示（可考虑加离线兜底）。
