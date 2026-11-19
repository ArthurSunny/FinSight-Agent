"""FinSight-Agent 结构化财务模型（Pydantic v2）。

设计原则（与 AGENTS.md 铁律一一对应）
--------------------------------------
1. **单位统一**（铁律 2 / 4.1）
   所有金额字段统一为「亿元」（保留 4 位小数）；所有比率字段统一为「小数」
   （``0.9193`` 表示 91.93%，不做 ×100）。报告根节点 ``meta.unit_notes`` 会再次显式声明，
   防止下游 LLM 误读数量级。
2. **可追溯**（铁律 2）
   ``raw_metrics`` 按报告期保留每一个原始科目数值；``meta.data_source`` 记录接口名、
   抓取时间与缓存命中状态；每条年度指标都带 ``report_period`` / ``report_year``。
3. **缺失即 null**（铁律 3）
   任何缺失或无法计算的指标一律为 ``None``（JSON ``null``），并在 ``warnings`` 中说明原因，
   绝不静默填充为 0。

工程约定
--------
* 模型全部 ``extra="forbid"``：宁可报错也不让拼写错误的字段静默丢失。
* 所有模型均可 ``model_dump_json()`` 直接喂给 LLM Agent，不会出现 NaN/Infinity。
"""

from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

# ---------------------------------------------------------------------------
# 类型别名：Amount = 亿元金额；Ratio = 小数比率
# ---------------------------------------------------------------------------
Amount = Optional[float]
Ratio = Optional[float]
Severity = Literal["high", "medium", "info"]
RiskLevel = Literal["high", "medium", "low", "unknown"]


class _Model(BaseModel):
    """所有模型的基类：禁止多余字段，保证 schema 严格。"""

    model_config = ConfigDict(extra="forbid")


# ===========================================================================
# 0. 元信息与可追溯性
# ===========================================================================
class EngineWarning(_Model):
    """结构化警告：缺失科目 / 分母为 0 / 口径不适用的统一出口。"""

    code: str = Field(description="警告类型码，如 missing_column / zero_denominator / not_applicable")
    level: Literal["warning", "info"] = Field(default="warning", description="级别")
    period: Optional[str] = Field(default=None, description="相关报告期，如 2024年报")
    field: Optional[str] = Field(default=None, description="相关科目或指标名")
    message: str = Field(description="中文说明：为什么这个指标是 null")


class DataSourceMeta(_Model):
    """数据来源标识（防幻觉：每个数字都能追到接口与抓取时间）。"""

    provider: str = Field(default="akshare", description="数据源封装库")
    upstream: str = Field(default="东方财富 Choice 数据接口", description="上游数据提供方")
    interfaces: dict[str, str] = Field(
        default_factory=dict, description="各报表使用的接口函数名，如 balance_sheet -> stock_balance_sheet_by_yearly_em"
    )
    fetched_at: dict[str, str] = Field(default_factory=dict, description="各报表抓取时间（ISO 8601）")
    cache_hit: dict[str, bool] = Field(default_factory=dict, description="各报表是否命中本地缓存")
    cache_paths: dict[str, str] = Field(default_factory=dict, description="各报表本地 CSV 缓存路径")
    as_of_date: str = Field(description="数据可见性截止日（用于剔除尚未披露的年报占位行）")


class ReportMeta(_Model):
    """报告元信息。"""

    stock_code: str = Field(description="6 位 A 股代码")
    stock_name: str = Field(description="股票简称")
    market_prefix: str = Field(description="数据源市场前缀，如 SH / SZ / BJ")
    org_type: str = Field(description="数据源自带企业类型：通用 / 银行 / 证券 / 保险 …")
    is_financial: bool = Field(description="是否为金融类企业（决定 Beneish 与部分规则是否适用）")
    financial_reason: Optional[str] = Field(default=None, description="判定为金融类的依据说明")
    latest_report_period: str = Field(description="最新完整会计年度，如 2025年报")
    report_periods: list[str] = Field(description="本次分析覆盖的报告期，按时间倒序")
    lookback_years: int = Field(description="展示的完整会计年度数量")
    audit_opinions: dict[str, Optional[str]] = Field(
        default_factory=dict, description="各年度审计意见类型"
    )
    amount_unit: str = Field(default="亿元", description="金额单位")
    ratio_unit: str = Field(default="小数", description="比率单位")
    unit_notes: dict[str, str] = Field(
        default_factory=dict, description="单位口径说明，供下游 Agent 读取"
    )
    engine_version: str = Field(description="引擎版本")
    generated_at: str = Field(description="报告生成时间（ISO 8601）")
    data_source: DataSourceMeta


# ===========================================================================
# 1. 原始科目快照（可追溯性核心）
# ===========================================================================
class RawYearMetrics(_Model):
    """单一年度的原始科目快照（单位：亿元）。

    这是整个报告的"事实底座"：任何比率都可以由这些科目复算出来，
    出现争议时以年报原文为准。
    """

    report_period: str = Field(description="报告期，如 2025年报")
    report_year: int = Field(description="报告年度，如 2025")

    # ---- 利润表 ----
    total_operating_revenue: Amount = Field(default=None, description="营业总收入（亿元）")
    operating_revenue: Amount = Field(default=None, description="营业收入（亿元）")
    operating_cost: Amount = Field(default=None, description="营业成本（亿元）")
    total_operating_cost: Amount = Field(default=None, description="营业总成本（亿元）")
    taxes_and_surcharges: Amount = Field(default=None, description="税金及附加（亿元）")
    selling_expense: Amount = Field(default=None, description="销售费用（亿元）")
    admin_expense: Amount = Field(default=None, description="管理费用（亿元）")
    rnd_expense: Amount = Field(default=None, description="研发费用（亿元）")
    finance_expense: Amount = Field(default=None, description="财务费用（亿元，负数代表净利息收入）")
    interest_income: Amount = Field(default=None, description="财务费用明细-利息收入（亿元）")
    interest_expense: Amount = Field(default=None, description="财务费用明细-利息费用（亿元）")
    asset_impairment_loss: Amount = Field(default=None, description="资产减值损失（亿元）")
    credit_impairment_loss: Amount = Field(default=None, description="信用减值损失（亿元）")
    investment_income: Amount = Field(default=None, description="投资收益（亿元）")
    other_income: Amount = Field(default=None, description="其他收益（亿元，多为政府补助）")
    operating_profit: Amount = Field(default=None, description="营业利润（亿元）")
    total_profit: Amount = Field(default=None, description="利润总额（亿元）")
    income_tax_expense: Amount = Field(default=None, description="所得税费用（亿元）")
    net_profit: Amount = Field(default=None, description="净利润（含少数股东损益，亿元）")
    parent_net_profit: Amount = Field(default=None, description="归属于母公司股东的净利润（亿元）")
    minority_profit: Amount = Field(default=None, description="少数股东损益（亿元）")
    deducted_parent_net_profit: Amount = Field(default=None, description="扣除非经常性损益后归母净利润（亿元）")

    # ---- 资产负债表：资产 ----
    monetary_funds: Amount = Field(default=None, description="货币资金（亿元）")
    accounts_receivable: Amount = Field(default=None, description="应收账款（亿元）")
    notes_receivable: Amount = Field(default=None, description="应收票据（亿元）")
    inventory: Amount = Field(default=None, description="存货（亿元）")
    prepayments: Amount = Field(default=None, description="预付款项（亿元）")
    total_current_assets: Amount = Field(default=None, description="流动资产合计（亿元）")
    fixed_assets: Amount = Field(default=None, description="固定资产（亿元）")
    construction_in_progress: Amount = Field(default=None, description="在建工程（亿元）")
    intangible_assets: Amount = Field(default=None, description="无形资产（亿元）")
    goodwill: Amount = Field(default=None, description="商誉（亿元）")
    total_assets: Amount = Field(default=None, description="资产总计（亿元）")

    # ---- 资产负债表：负债与权益 ----
    short_term_loans: Amount = Field(default=None, description="短期借款（亿元）")
    current_portion_noncurrent_liabilities: Amount = Field(
        default=None, description="一年内到期的非流动负债（亿元）"
    )
    long_term_loans: Amount = Field(default=None, description="长期借款（亿元）")
    bonds_payable: Amount = Field(default=None, description="应付债券（亿元）")
    lease_liabilities: Amount = Field(default=None, description="租赁负债（亿元）")
    short_term_bonds_payable: Amount = Field(default=None, description="应付短期债券（亿元）")
    accounts_payable: Amount = Field(default=None, description="应付账款（亿元）")
    contract_liabilities: Amount = Field(default=None, description="合同负债（亿元，旧准则预收账款）")
    total_current_liabilities: Amount = Field(default=None, description="流动负债合计（亿元）")
    total_liabilities: Amount = Field(default=None, description="负债合计（亿元）")
    total_parent_equity: Amount = Field(default=None, description="归属于母公司所有者权益合计（亿元）")
    total_equity: Amount = Field(default=None, description="所有者权益合计（亿元）")
    minority_equity: Amount = Field(default=None, description="少数股东权益（亿元）")

    # ---- 现金流量表 ----
    cash_from_sales: Amount = Field(default=None, description="销售商品、提供劳务收到的现金（亿元）")
    net_cash_from_operating: Amount = Field(default=None, description="经营活动产生的现金流量净额（亿元）")
    net_cash_from_investing: Amount = Field(default=None, description="投资活动产生的现金流量净额（亿元）")
    net_cash_from_financing: Amount = Field(default=None, description="筹资活动产生的现金流量净额（亿元）")
    capex: Amount = Field(
        default=None, description="购建固定资产、无形资产和其他长期资产支付的现金（亿元，资本开支近似值）"
    )
    ending_cash_and_equivalents: Amount = Field(default=None, description="期末现金及现金等价物余额（亿元）")
    depreciation_amortization: Amount = Field(
        default=None, description="固定资产折旧、油气资产折耗、生产性生物资产折旧（亿元）"
    )
    intangible_amortization: Amount = Field(default=None, description="无形资产摊销（亿元）")


# ===========================================================================
# 2. 维度一：核心成长与盈利趋势
# ===========================================================================
class YearGrowth(_Model):
    """单一年度的成长与盈利指标。"""

    report_period: str
    report_year: int
    total_operating_revenue: Amount = Field(description="营业总收入（亿元）")
    total_operating_revenue_yoy: Ratio = Field(description="营业总收入同比（小数，-0.0120 表示 -1.20%）")
    revenue_yoy_reported: Ratio = Field(
        default=None, description="数据源自带 TOTAL_OPERATE_INCOME_YOY 换算值，仅作对照（÷100）"
    )
    operating_revenue: Amount = Field(description="营业收入（亿元）")
    operating_revenue_yoy: Ratio = Field(description="营业收入同比（小数）")
    parent_net_profit: Amount = Field(description="归母净利润（亿元）")
    parent_net_profit_yoy: Ratio = Field(description="归母净利润同比（小数）")
    parent_net_profit_yoy_reported: Ratio = Field(default=None, description="数据源自带同比，仅作对照")
    deducted_parent_net_profit: Amount = Field(description="扣非归母净利润（亿元）")
    deducted_parent_net_profit_yoy: Ratio = Field(description="扣非归母净利润同比（小数）")
    net_profit: Amount = Field(description="净利润（亿元）")
    net_profit_yoy: Ratio = Field(description="净利润同比（小数）")
    gross_margin: Ratio = Field(description="毛利率 =（营业收入-营业成本）/营业收入；金融类为 null")
    net_margin: Ratio = Field(description="净利率 = 净利润/营业收入")
    parent_net_margin: Ratio = Field(description="归母净利率 = 归母净利润/营业收入")


class CagrSummary(_Model):
    """区间复合增长率（CAGR）。"""

    window_start: str = Field(description="起始报告期")
    window_end: str = Field(description="结束报告期")
    periods: int = Field(description="区间年数")
    revenue_cagr: Ratio = Field(description="营业总收入 CAGR（小数）")
    parent_net_profit_cagr: Ratio = Field(description="归母净利润 CAGR（小数）")
    deducted_net_profit_cagr: Ratio = Field(description="扣非归母净利润 CAGR（小数）")


class GrowthProfitability(_Model):
    """维度一：核心成长与盈利趋势。"""

    years: list[YearGrowth] = Field(description="按时间倒序排列的年度指标")
    cagr: CagrSummary
    trend_observations: list[str] = Field(
        default_factory=list, description="由数值规则自动生成的趋势观察（非 LLM 推断）"
    )


# ===========================================================================
# 3. 维度二：杜邦分析
# ===========================================================================
class YearDuPont(_Model):
    """单一年度杜邦拆解。"""

    report_period: str
    report_year: int
    net_margin: Ratio = Field(description="销售净利率 = 净利润 / 营业总收入")
    total_asset_turnover: Ratio = Field(description="总资产周转率 = 营业总收入 / 平均总资产")
    equity_multiplier: Ratio = Field(description="权益乘数 = 平均总资产 / 平均所有者权益")
    roe_dupont: Ratio = Field(description="ROE（杜邦乘积）= 净利率 × 周转率 × 权益乘数")
    roe_direct: Ratio = Field(description="ROE（直接法）= 净利润 / 平均所有者权益，用于校验恒等式")
    identity_check_error: Ratio = Field(
        description="恒等式校验误差 = roe_dupont - roe_direct，正常应接近 0"
    )
    roe_parent_diluted: Ratio = Field(
        description="归母摊薄 ROE = 归母净利润 / 期末归母权益（与外部指标源同口径）"
    )
    average_total_assets: Amount = Field(description="平均总资产（亿元）=（期初+期末）/2")
    average_total_equity: Amount = Field(description="平均所有者权益（亿元）")
    net_margin_delta: Ratio = Field(default=None, description="销售净利率同比变动（小数，绝对差）")
    asset_turnover_delta: Ratio = Field(default=None, description="总资产周转率同比变动（次）")
    equity_multiplier_delta: Ratio = Field(default=None, description="权益乘数同比变动")
    roe_delta: Ratio = Field(default=None, description="ROE 同比变动（小数，绝对差）")
    # ---- 连环替代法三因子贡献（Phase 4 追加：供前端瀑布图直接读取，UI 不再自行计算）----
    net_margin_effect: Ratio = Field(
        default=None, description="销售净利率对 ROE 变动的贡献（小数，即百分点/100）"
    )
    asset_turnover_effect: Ratio = Field(
        default=None, description="总资产周转率对 ROE 变动的贡献（小数）"
    )
    equity_multiplier_effect: Ratio = Field(
        default=None, description="权益乘数对 ROE 变动的贡献（小数）"
    )


class DuPontAnalysis(_Model):
    """维度二：杜邦分析体系。"""

    formula: str = Field(description="杜邦恒等式文字说明")
    years: list[YearDuPont] = Field(description="按时间倒序排列")
    dominant_driver: Optional[str] = Field(
        default=None, description="最新年度 ROE 的主导驱动因子判定（由变动贡献量化得出）"
    )
    driver_note: Optional[str] = Field(default=None, description="主导驱动因子的中文解释")


# ===========================================================================
# 4. 维度三：盈余质量与现金流检验
# ===========================================================================
class YearCashFlow(_Model):
    """单一年度现金流质量指标。"""

    report_period: str
    report_year: int
    net_cash_from_operating: Amount = Field(description="经营活动现金流量净额（亿元）")
    net_profit: Amount = Field(description="净利润（亿元）")
    net_cash_ratio: Ratio = Field(
        description="净现比 = 经营活动现金流量净额 / 净利润；净利润≤0 时为 null（无经济意义）"
    )
    cash_from_sales: Amount = Field(description="销售商品、提供劳务收到的现金（亿元）")
    operating_revenue: Amount = Field(description="营业收入（亿元）")
    cash_collection_ratio: Ratio = Field(
        description="收现比 = 销售商品提供劳务收到的现金 / 营业收入；注意含增值税，>1 属正常"
    )
    capex: Amount = Field(description="资本开支近似值（购建长期资产支付的现金，亿元）")
    free_cash_flow: Amount = Field(description="自由现金流 FCF ≈ 经营现金流净额 - 资本开支（亿元）")
    fcf_margin: Ratio = Field(description="FCF / 营业收入")
    operating_cash_to_revenue: Ratio = Field(description="经营现金流净额 / 营业收入")
    accrual_ratio: Ratio = Field(
        description="应计利润比率 =（净利润 - 经营现金流净额）/ 总资产；越高盈余质量越差"
    )


class CashFlowQuality(_Model):
    """维度三：盈余质量与现金流检验。"""

    years: list[YearCashFlow] = Field(description="按时间倒序排列")
    notes: list[str] = Field(default_factory=list, description="口径提示（增值税、净利润为负等）")


# ===========================================================================
# 5. 维度四：会计排雷
# ===========================================================================
class BeneishVariable(_Model):
    """Beneish M-Score 单个子指标。"""

    code: str = Field(description="指标代号，如 DSRI")
    name_cn: str = Field(description="中文名称")
    name_en: str = Field(description="英文名称")
    value: Ratio = Field(description="指标值；无法计算时为 null")
    weight: float = Field(description="在 M-Score 线性组合中的系数")
    contribution: Ratio = Field(default=None, description="该指标对 M-Score 的贡献 = 系数 × 指标值")
    formula: str = Field(description="公式说明（中式科目映射后）")
    inputs: dict[str, Amount] = Field(default_factory=dict, description="代入公式的原始数值（亿元或比率）")
    interpretation: str = Field(description="指标的经济含义与方向性解读")
    note: Optional[str] = Field(default=None, description="中式报表映射的特别说明")


class BeneishAnalysis(_Model):
    """Beneish M-Score 舞弊预警模型（8 变量版）。"""

    applicable: bool = Field(description="是否适用于该公司（金融类不适用）")
    reason: Optional[str] = Field(default=None, description="不适用时的原因说明")
    latest_period: Optional[str] = Field(default=None, description="最新报告期 t")
    prior_period: Optional[str] = Field(default=None, description="对比报告期 t-1")
    variables: list[BeneishVariable] = Field(default_factory=list)
    m_score: Ratio = Field(default=None, description="M-Score 综合得分")
    threshold: float = Field(default=-1.78, description="判读阈值：M > -1.78 视为存在盈余操纵嫌疑")
    is_high_risk: Optional[bool] = Field(default=None, description="是否高于阈值")
    risk_text: str = Field(default="", description="判读结论文字")
    intercept: float = Field(default=-4.84, description="模型截距项")
    model_formula: str = Field(default="", description="M-Score 线性组合的完整公式")
    cautions: list[str] = Field(default_factory=list, description="模型适用性提醒")


class RuleFlag(_Model):
    """本土化财务异常规则（硬检验）结果。"""

    code: str = Field(description="规则代号，如 CASH_LOAN_DOUBLE_HIGH")
    name: str = Field(description="规则中文名")
    category: str = Field(description="所属类别：存贷双高 / 盈余质量 / 资产质量 / 审计与其他")
    evaluated: bool = Field(description="是否执行了该规则的检验（金融类等场景可能跳过）")
    triggered: bool = Field(default=False, description="是否触发预警")
    severity: Severity = Field(default="info", description="触发时的严重程度")
    summary: str = Field(description="一句话结论（中文，可直接展示）")
    detail: list[str] = Field(default_factory=list, description="逐条证据说明，便于尽调复核")
    evidence: dict[str, Any] = Field(default_factory=dict, description="支撑判定用的具体数值")
    thresholds: dict[str, float] = Field(default_factory=dict, description="本次使用的阈值")
    interpretation_hint: Optional[str] = Field(
        default=None, description="触发后的业务追查方向 / 需要区分的特殊情形"
    )
    skip_reason: Optional[str] = Field(default=None, description="未执行时的原因")


class ForensicAnalysis(_Model):
    """维度四：专业会计审计排雷。"""

    beneish: BeneishAnalysis
    rule_flags: list[RuleFlag] = Field(description="全部规则检验结果（含未触发的）")
    triggered_count: int = Field(description="触发的规则数量")
    triggered_high_count: int = Field(description="其中高严重度数量")
    risk_level: RiskLevel = Field(description="综合风险等级")
    key_findings: list[str] = Field(default_factory=list, description="关键发现摘要（按严重度排序）")


# ===========================================================================
# 6. 辅助：营运效率（供存货积压规则与交叉校验使用）
# ===========================================================================
class YearEfficiency(_Model):
    """单一年度营运效率指标。"""

    report_period: str
    report_year: int
    inventory_turnover: Ratio = Field(description="存货周转率（次）= 营业成本 / 平均存货")
    inventory_days: Ratio = Field(description="存货周转天数 = 365 / 存货周转率")
    receivables_turnover: Ratio = Field(description="应收账款周转率（次）= 营业收入 / 平均应收账款")
    receivables_days: Ratio = Field(description="应收账款周转天数")
    payables_turnover: Ratio = Field(description="应付账款周转率（次）= 营业成本 / 平均应付账款")
    payables_days: Ratio = Field(description="应付账款周转天数")
    cash_conversion_cycle: Ratio = Field(
        description="现金循环周期（天）= 存货周转天数 + 应收周转天数 - 应付周转天数"
    )
    inventory_to_current_assets: Ratio = Field(description="存货 / 流动资产（结构占比）")


class EfficiencyMetrics(_Model):
    """辅助维度：营运效率（不属于四大核心维度，但为存货积压规则提供依据）。"""

    years: list[YearEfficiency] = Field(description="按时间倒序排列")


# ===========================================================================
# 7. 交叉校验（可选）
# ===========================================================================
class ValidationCheck(_Model):
    """单条交叉校验记录。"""

    metric: str = Field(description="指标名")
    report_period: str = Field(description="报告期")
    engine_value: Ratio = Field(description="引擎计算值")
    source_value: Ratio = Field(description="外部数据源值")
    diff: Ratio = Field(description="差值（引擎 - 外部源）")
    tolerance: float = Field(description="允许绝对误差")
    passed: Optional[bool] = Field(description="是否通过；任一侧缺失时为 null")


class CrossValidation(_Model):
    """与外部财务指标接口的独立比对，用于自证引擎计算正确性。"""

    available: bool = Field(description="交叉校验是否成功执行")
    source: str = Field(description="外部数据源接口")
    reason: Optional[str] = Field(default=None, description="未能执行的原因")
    checks: list[ValidationCheck] = Field(default_factory=list)
    passed_count: int = Field(default=0)
    failed_count: int = Field(default=0)
    skipped_count: int = Field(default=0)
    note: str = Field(default="")


# ===========================================================================
# 8. 汇总与根节点
# ===========================================================================
class ReportSummary(_Model):
    """报告摘要：给下游 Agent 的最短必读信息。"""

    risk_level: RiskLevel = Field(description="综合风险等级")
    triggered_flags: int = Field(description="触发预警的规则数")
    high_severity_flags: int = Field(description="高严重度预警数")
    data_quality: str = Field(description="数据完整度描述")
    missing_field_count: int = Field(description="缺失科目/指标计数")
    risk_disclaimer: str = Field(
        default="",
        description="风险等级口径声明：等级仅由触发的规则数量与严重度推导，不构成投资结论",
    )
    key_findings: list[str] = Field(default_factory=list, description="关键结论（确定性文本，非 LLM 生成）")


class FinancialReport(_Model):
    """财务体检报告根节点：可直接 ``model_dump_json()`` 喂给后续 Agent。"""

    meta: ReportMeta
    raw_metrics: dict[str, RawYearMetrics] = Field(
        description="原始科目快照，键为报告期（如 2025年报），值为亿元口径科目"
    )
    growth_profitability: GrowthProfitability = Field(description="维度一：核心成长与盈利趋势")
    dupont: DuPontAnalysis = Field(description="维度二：杜邦分析体系")
    cashflow_quality: CashFlowQuality = Field(description="维度三：盈余质量与现金流检验")
    forensic: ForensicAnalysis = Field(description="维度四：专业会计审计排雷")
    efficiency: EfficiencyMetrics = Field(description="辅助维度：营运效率")
    cross_validation: Optional[CrossValidation] = Field(
        default=None, description="交叉校验结果（include_cross_validation=True 时存在）"
    )
    warnings: list[EngineWarning] = Field(default_factory=list, description="全部缺失值/口径警告")
    summary: ReportSummary
