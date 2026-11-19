"""FinSight-Agent 核心财务计算与会计排雷引擎（Phase 1 核心交付物）。

输入
----
6 位 A 股代码（如 ``"600519"``）→ 自动取数（带 CSV/JSON 本地缓存）→ 输出结构化
:class:`src.tools.schemas.FinancialReport`，可直接 ``model_dump_json()`` 喂给后续 Agent。

四大计算维度
------------
1. **核心成长与盈利趋势**：营业总收入/归母/扣非及 YoY、毛利率、净利率、区间 CAGR
2. **杜邦分析体系**：ROE = 销售净利率 × 总资产周转率 × 权益乘数，并用「连环替代法」
   量化三个因子各自对 ROE 变动的贡献
3. **盈余质量与现金流检验**：净现比、收现比、自由现金流（FCF）近似
4. **会计排雷**：Beneish M-Score 8 变量（中式科目映射）+ 6 条本土化硬规则

设计铁律（详见 AGENTS.md）
--------------------------
* 计算与推理严格分离：本模块是唯一允许出现财务数值逻辑的地方。
* 数据可追溯：每个输出都带报告期，原始科目完整保留在 ``raw_metrics``。
* 容错优先：缺失科目 / 分母为 0 → 返回 ``None`` 并登记警告，**绝不填充 0**。
* 金融类公司（``ORG_TYPE`` 非"通用"）自动降级：Beneish 与存货类规则标注"不适用"而非崩溃。

用法
----
>>> from src.tools.financial_engine import analyze_stock
>>> report = analyze_stock("600519", lookback_years=4)
>>> report.meta.stock_name
'贵州茅台'

命令行::

    python -m src.tools.financial_engine 600519 --indent 2
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Optional, Sequence

from src.config import (
    DEFAULT_CACHE_TTL_HOURS,
    DEFAULT_LOOKBACK_YEARS,
    ENGINE_VERSION,
    MAX_LOOKBACK_YEARS,
    MIN_REQUIRED_YEARS,
    REPORT_OUTPUT_DIR,
    YUAN_PER_YI,
    ZERO_DENOMINATOR_EPS,
    ensure_directories,
)
from src.tools.diagnostics import WarningCollector, finite_or_none, round_amount, round_ratio
from src.tools.financial_data import (
    DataUnavailableError,
    FinancialDataProvider,
    StatementBundle,
)
from src.tools.schemas import (
    BeneishAnalysis,
    BeneishVariable,
    CagrSummary,
    CashFlowQuality,
    CrossValidation,
    DataSourceMeta,
    DuPontAnalysis,
    EfficiencyMetrics,
    FinancialReport,
    ForensicAnalysis,
    GrowthProfitability,
    RawYearMetrics,
    ReportMeta,
    ReportSummary,
    RuleFlag,
    YearCashFlow,
    YearDuPont,
    YearEfficiency,
    YearGrowth,
)

# ===========================================================================
# 常量：原始科目映射、模型权重、规则阈值
# ===========================================================================
#: (报表键, 输出字段名, 数据源列名, 中文科目名) —— 数据可追溯性的字段清单
RAW_FIELD_SPEC: tuple[tuple[str, str, str, str], ...] = (
    # ---- 利润表 ----
    ("profit_sheet", "total_operating_revenue", "TOTAL_OPERATE_INCOME", "营业总收入"),
    ("profit_sheet", "operating_revenue", "OPERATE_INCOME", "营业收入"),
    ("profit_sheet", "operating_cost", "OPERATE_COST", "营业成本"),
    ("profit_sheet", "total_operating_cost", "TOTAL_OPERATE_COST", "营业总成本"),
    ("profit_sheet", "taxes_and_surcharges", "OPERATE_TAX_ADD", "税金及附加"),
    ("profit_sheet", "selling_expense", "SALE_EXPENSE", "销售费用"),
    ("profit_sheet", "admin_expense", "MANAGE_EXPENSE", "管理费用"),
    ("profit_sheet", "rnd_expense", "RESEARCH_EXPENSE", "研发费用"),
    ("profit_sheet", "finance_expense", "FINANCE_EXPENSE", "财务费用"),
    ("profit_sheet", "interest_income", "FE_INTEREST_INCOME", "财务费用-利息收入"),
    ("profit_sheet", "interest_expense", "FE_INTEREST_EXPENSE", "财务费用-利息费用"),
    ("profit_sheet", "asset_impairment_loss", "ASSET_IMPAIRMENT_LOSS", "资产减值损失"),
    ("profit_sheet", "credit_impairment_loss", "CREDIT_IMPAIRMENT_LOSS", "信用减值损失"),
    ("profit_sheet", "investment_income", "INVEST_INCOME", "投资收益"),
    ("profit_sheet", "other_income", "OTHER_INCOME", "其他收益"),
    ("profit_sheet", "operating_profit", "OPERATE_PROFIT", "营业利润"),
    ("profit_sheet", "total_profit", "TOTAL_PROFIT", "利润总额"),
    ("profit_sheet", "income_tax_expense", "INCOME_TAX", "所得税费用"),
    ("profit_sheet", "net_profit", "NETPROFIT", "净利润"),
    ("profit_sheet", "parent_net_profit", "PARENT_NETPROFIT", "归母净利润"),
    ("profit_sheet", "minority_profit", "MINORITY_INTEREST", "少数股东损益"),
    ("profit_sheet", "deducted_parent_net_profit", "DEDUCT_PARENT_NETPROFIT", "扣非归母净利润"),
    # ---- 资产负债表：资产 ----
    ("balance_sheet", "monetary_funds", "MONETARYFUNDS", "货币资金"),
    ("balance_sheet", "accounts_receivable", "ACCOUNTS_RECE", "应收账款"),
    ("balance_sheet", "notes_receivable", "NOTE_RECE", "应收票据"),
    ("balance_sheet", "inventory", "INVENTORY", "存货"),
    ("balance_sheet", "prepayments", "PREPAYMENT", "预付款项"),
    ("balance_sheet", "total_current_assets", "TOTAL_CURRENT_ASSETS", "流动资产合计"),
    ("balance_sheet", "fixed_assets", "FIXED_ASSET", "固定资产"),
    ("balance_sheet", "construction_in_progress", "CIP", "在建工程"),
    ("balance_sheet", "intangible_assets", "INTANGIBLE_ASSET", "无形资产"),
    ("balance_sheet", "goodwill", "GOODWILL", "商誉"),
    ("balance_sheet", "total_assets", "TOTAL_ASSETS", "资产总计"),
    # ---- 资产负债表：负债与权益 ----
    ("balance_sheet", "short_term_loans", "SHORT_LOAN", "短期借款"),
    (
        "balance_sheet",
        "current_portion_noncurrent_liabilities",
        "NONCURRENT_LIAB_1YEAR",
        "一年内到期的非流动负债",
    ),
    ("balance_sheet", "long_term_loans", "LONG_LOAN", "长期借款"),
    ("balance_sheet", "bonds_payable", "BOND_PAYABLE", "应付债券"),
    ("balance_sheet", "lease_liabilities", "LEASE_LIAB", "租赁负债"),
    ("balance_sheet", "short_term_bonds_payable", "SHORT_BOND_PAYABLE", "应付短期债券"),
    ("balance_sheet", "accounts_payable", "ACCOUNTS_PAYABLE", "应付账款"),
    ("balance_sheet", "contract_liabilities", "CONTRACT_LIAB", "合同负债"),
    ("balance_sheet", "total_current_liabilities", "TOTAL_CURRENT_LIAB", "流动负债合计"),
    ("balance_sheet", "total_liabilities", "TOTAL_LIABILITIES", "负债合计"),
    ("balance_sheet", "total_parent_equity", "TOTAL_PARENT_EQUITY", "归母所有者权益合计"),
    ("balance_sheet", "total_equity", "TOTAL_EQUITY", "所有者权益合计"),
    ("balance_sheet", "minority_equity", "MINORITY_EQUITY", "少数股东权益"),
    # ---- 现金流量表 ----
    ("cash_flow_sheet", "cash_from_sales", "SALES_SERVICES", "销售商品提供劳务收到的现金"),
    ("cash_flow_sheet", "net_cash_from_operating", "NETCASH_OPERATE", "经营活动现金流量净额"),
    ("cash_flow_sheet", "net_cash_from_investing", "NETCASH_INVEST", "投资活动现金流量净额"),
    ("cash_flow_sheet", "net_cash_from_financing", "NETCASH_FINANCE", "筹资活动现金流量净额"),
    ("cash_flow_sheet", "capex", "CONSTRUCT_LONG_ASSET", "购建长期资产支付的现金"),
    ("cash_flow_sheet", "ending_cash_and_equivalents", "END_CCE", "期末现金及现金等价物"),
    ("cash_flow_sheet", "depreciation_amortization", "FA_IR_DEPR", "固定资产折旧等"),
    ("cash_flow_sheet", "intangible_amortization", "IA_AMORTIZE", "无形资产摊销"),
)

#: 有息负债构成科目（"存贷双高"检验用）
INTEREST_BEARING_DEBT_ITEMS: tuple[tuple[str, str], ...] = (
    ("SHORT_LOAN", "短期借款"),
    ("LONG_LOAN", "长期借款"),
    ("BOND_PAYABLE", "应付债券"),
    ("NONCURRENT_LIAB_1YEAR", "一年内到期的非流动负债"),
    ("LEASE_LIAB", "租赁负债"),
    ("SHORT_BOND_PAYABLE", "应付短期债券"),
)

#: 判定金融类企业的 ORG_TYPE 取值（数据源自带，优于代码前缀猜测）
FINANCIAL_ORG_TYPES: frozenset[str] = frozenset(
    {"银行", "证券", "保险", "多元金融", "信托", "期货", "基金", "租赁", "资产管理"}
)

#: 工商企业报表必备的经营类科目（金融类报表结构缺乏这些列）
INDUSTRIAL_MARKER_COLUMNS: tuple[str, ...] = (
    "INVENTORY",
    "MONETARYFUNDS",
    "TOTAL_CURRENT_ASSETS",
)

#: Beneish M-Score 8 变量权重（Beneish, 1999）
BENEISH_WEIGHTS: dict[str, float] = {
    "DSRI": 0.920,
    "GMI": 0.528,
    "AQI": 0.404,
    "SGI": 0.892,
    "DEPI": 0.115,
    "SGAI": -0.172,
    "TATA": 4.679,
    "LVGI": -0.327,
}
BENEISH_INTERCEPT: float = -4.84
BENEISH_THRESHOLD: float = -1.78

DUPONT_FORMULA: str = (
    "ROE = 销售净利率 × 总资产周转率 × 权益乘数 = (净利润/营业收入) × (营业收入/平均总资产)"
    " × (平均总资产/平均所有者权益)。"
    "三个因子统一采用「营业收入」口径，使恒等式可精确成立、且与外部指标源可比。"
)


@dataclass(frozen=True)
class RuleThresholds:
    """本土化排雷规则的阈值（集中管理，便于按行业微调）。"""

    # 存贷双高
    cash_to_assets_high: float = 0.15
    interest_debt_to_assets_high: float = 0.20
    min_cash_ratio_for_rate_test: float = 0.10
    implied_deposit_rate_warn: float = 0.010
    implied_deposit_rate_watch: float = 0.015
    # 存贷双高：受限货币资金勾稽（Phase 1.1）
    #: 受限货币资金占货币资金比例超过该值视为"高受限风险"
    restricted_cash_ratio_high: float = 0.30
    #: 只有货币资金本身具备重要性（占总资产比例）时，受限比例才有分析意义
    restricted_cash_min_cash_ratio: float = 0.10
    #: 期末现金及现金等价物超过货币资金的倍数门槛，用于识别"合并财务公司"导致的列报差异
    cce_excess_ratio: float = 1.05
    # 纸面富贵
    net_cash_ratio_low: float = 0.70
    ar_growth_gap: float = 0.15
    #: 重要性原则：应收账款占营业收入低于该比例时，增速波动不具重要性，不触发预警
    min_receivables_to_revenue: float = 0.01
    # 存货积压
    inventory_share_min: float = 0.10
    # 审计与其他
    goodwill_to_parent_equity_high: float = 0.10
    nonrecurring_reliance_high: float = 0.30

    def to_dict(self) -> dict[str, float]:
        """导出为 ``RuleFlag.thresholds``。

        注意：新增阈值只会**追加**键，不会修改既有键名，保证对外 JSON 向后兼容。
        """
        return {key: float(value) for key, value in asdict(self).items()}


# ===========================================================================
# 语句视图：把三表 DataFrame 变成「按年度取数 + 自动登记缺失」的对象
# ===========================================================================
class _StatementView:
    """报表取数视图。

    职责：按 (报表, 列名, 年度) 取原始值（单位：元），并对**缺失**做统一登记。
    这样上层的每个计算公式都不需要重复写 ``if pd.isna(...)``。
    """

    def __init__(self, bundle: StatementBundle, collector: WarningCollector) -> None:
        self.bundle = bundle
        self.ctx = collector
        self._records: dict[str, dict[int, dict[str, Any]]] = {}
        self._columns: dict[str, set[str]] = {}
        for key, frame in bundle.frames.items():
            if frame.empty or "_report_year" not in frame.columns:
                self._records[key] = {}
                self._columns[key] = set(frame.columns)
                continue
            self._records[key] = {
                int(row["_report_year"]): row for _, row in frame.iterrows()
            }
            self._columns[key] = set(frame.columns)
        self.years: list[int] = bundle.report_years

    # -- 基础能力 -----------------------------------------------------------
    def has_column(self, table: str, column: str) -> bool:
        return column in self._columns.get(table, set())

    @staticmethod
    def period(year: int) -> str:
        return f"{year}年报"

    def raw(
        self,
        table: str,
        column: str,
        year: int,
        label: str = "",
        *,
        quiet: bool = False,
    ) -> Optional[float]:
        """取原始科目值（单位：元）；缺失或非有限值返回 None 并登记警告。"""
        period = self.period(year)
        if not self.has_column(table, column):
            if not quiet:
                self.ctx.add(
                    "missing_column",
                    f"数据源 {table} 不存在列 {label or column}({column})，"
                    f"依赖该科目的指标全部返回 null。",
                    period=period,
                    field=column,
                )
            return None
        record = self._records.get(table, {}).get(year)
        if record is None:
            # 早于数据源最早年度属于"超出数据覆盖范围"的预期行为，不刷警告
            out_of_range = bool(self.years) and year < min(self.years)
            if not quiet and not out_of_range:
                self.ctx.add(
                    "missing_period",
                    f"数据源 {table} 缺少报告期 {period} 的记录，相关指标返回 null。",
                    period=period,
                    field=column,
                )
            return None
        value = finite_or_none(record.get(column))
        if value is None and not quiet:
            self.ctx.missing(table, column, period, label=label)
        return value

    def amount(self, table: str, column: str, year: int, label: str = "") -> Optional[float]:
        """取原始科目值并换算为「亿元」（保留 4 位小数）。"""
        return round_amount(self._scale(table, column, year, label))

    def _scale(self, table: str, column: str, year: int, label: str = "") -> Optional[float]:
        value = self.raw(table, column, year, label)
        return None if value is None else value / YUAN_PER_YI

    def sum_amounts(
        self,
        table: str,
        items: Sequence[tuple[str, str]],
        year: int,
        label: str,
        *,
        all_missing_as_zero: bool = False,
    ) -> Optional[float]:
        """多个科目求和（亿元）。

        Parameters
        ----------
        all_missing_as_zero:
            仅用于「有息负债」这类场景：全部构成科目为空时视为 0（通常意味着无该项负债），
            但必须同权限写警告（AGENTS.md 铁律 3 唯一豁免）。
        """
        period = self.period(year)
        values: dict[str, Optional[float]] = {
            item_label: self._scale(table, column, year, item_label)
            for column, item_label in items
        }
        present = {key: value for key, value in values.items() if value is not None}
        missing = [key for key, value in values.items() if value is None]

        if not present:
            if all_missing_as_zero:
                self.ctx.add(
                    "items_all_missing_zeroed",
                    f"报告期 {period} 的「{label}」全部构成科目在数据源中为空，"
                    f"按 0 处理（通常表示无该项负债，但必须以年报附注复核）。",
                    period=period,
                    field=label,
                )
                return 0.0
            self.ctx.add(
                "items_all_missing",
                f"报告期 {period} 的「{label}」全部构成科目缺失，返回 null（不填充 0）。",
                period=period,
                field=label,
            )
            return None
        if missing:
            self.ctx.add(
                "items_partial_missing",
                f"报告期 {period} 的「{label}」缺少构成科目 {missing}，"
                f"按已披露科目求和，结果可能偏低。",
                period=period,
                field=label,
            )
        return round_amount(sum(present.values()))

    def total_revenue(self, year: int) -> Optional[float]:
        """营业总收入（亿元）。

        银行等金融类企业的利润表**没有** ``TOTAL_OPERATE_INCOME`` 列（实测：招商银行
        PL 仅 221 列，缺少该字段），此时退回 ``OPERATE_INCOME``（金融类两者等价）并登记警告，
        避免整个成长性与杜邦维度因为一个列名差异而全部返回 null。
        """
        value = self.amount("profit_sheet", "TOTAL_OPERATE_INCOME", year, "营业总收入")
        if value is None:
            fallback = self.amount("profit_sheet", "OPERATE_INCOME", year, "营业收入")
            if fallback is not None:
                self.ctx.add(
                    "total_revenue_fallback",
                    f"报告期 {self.period(year)} 数据源无「营业总收入」科目（金融类报表常见），"
                    f"已退回使用「营业收入」口径计算成长性指标。",
                    period=self.period(year),
                    field="TOTAL_OPERATE_INCOME",
                )
            return fallback
        return value

    def sales(self, year: int) -> Optional[float]:
        """营业收入（亿元）。Beneish 与各类比率统一采用「营业收入」口径。

        数据源 ``OPERATE_INCOME`` 为空时退回 ``TOTAL_OPERATE_INCOME``（并登记警告），
        因为两者在多数工商企业中等价。
        """
        value = self.amount("profit_sheet", "OPERATE_INCOME", year, "营业收入")
        if value is None:
            fallback = self.amount("profit_sheet", "TOTAL_OPERATE_INCOME", year, "营业总收入")
            if fallback is not None:
                self.ctx.add(
                    "revenue_fallback",
                    f"报告期 {self.period(year)} 营业收入为空，已退回使用营业总收入口径。",
                    period=self.period(year),
                    field="OPERATE_INCOME",
                )
            return fallback
        return value


# ===========================================================================
# 引擎
# ===========================================================================
class FinancialEngine:
    """财务计算与会计排雷引擎。

    Parameters
    ----------
    cache_dir:
        原始数据缓存目录，默认 ``data/cache/statements``。
    cache_ttl_hours:
        缓存有效期（小时）。
    thresholds:
        排雷规则阈值，默认取 :class:`RuleThresholds` 的行业通用取值。
    """

    def __init__(
        self,
        cache_dir: Optional[Path] = None,
        cache_ttl_hours: Optional[float] = None,
        thresholds: Optional[RuleThresholds] = None,
    ) -> None:
        self.provider = FinancialDataProvider(
            cache_dir=cache_dir,
            ttl_hours=cache_ttl_hours if cache_ttl_hours is not None else DEFAULT_CACHE_TTL_HOURS,
        )
        self.thresholds = thresholds or RuleThresholds()

    # ------------------------------------------------------------------
    # 公共入口
    # ------------------------------------------------------------------
    def analyze(
        self,
        code: str,
        *,
        lookback_years: int = DEFAULT_LOOKBACK_YEARS,
        force_refresh: bool = False,
        as_of: Optional[dt.datetime] = None,
        include_cross_validation: bool = False,
    ) -> FinancialReport:
        """分析一只 A 股股票，返回完整财务体检报告。"""
        collector = WarningCollector(str(code).strip())
        bundle = self.provider.load(
            code, force_refresh=force_refresh, as_of=as_of, collector=collector
        )
        view = _StatementView(bundle, collector)

        if len(view.years) < MIN_REQUIRED_YEARS:
            raise DataUnavailableError(
                f"{bundle.code} 可用于年度对比的报告期不足 {MIN_REQUIRED_YEARS} 个"
                f"（实际: {[view.period(y) for y in view.years]}），无法计算同比与杜邦拆解。"
            )

        lookback = max(1, min(int(lookback_years), MAX_LOOKBACK_YEARS))
        if lookback > len(view.years):
            collector.add(
                "lookback_shortened",
                f"请求回溯 {lookback} 个年度，但数据源仅有 {len(view.years)} 个完整年度，"
                f"已按 {len(view.years)} 个年度输出。",
                level="info",
            )
            lookback = len(view.years)

        display_years = view.years[:lookback]
        is_financial, financial_reason = self._detect_financial(view)

        raw_metrics = self._build_raw_metrics(view, display_years)
        growth = self._compute_growth(view, display_years, is_financial)
        dupont = self._compute_dupont(view, display_years)
        cashflow = self._compute_cashflow(view, display_years, is_financial)
        efficiency = self._compute_efficiency(view, display_years, is_financial)
        forensic = self._compute_forensic(view, display_years, is_financial)
        summary = self._build_summary(view, display_years, growth, dupont, forensic, raw_metrics)

        cross_validation: Optional[CrossValidation] = None
        if include_cross_validation:
            from src.tools.cross_validation import run_cross_validation

            cross_validation = run_cross_validation(
                stock_code=bundle.code,
                display_years=display_years,
                report=_partial_report(
                    growth=growth,
                    dupont=dupont,
                    cashflow=cashflow,
                    efficiency=efficiency,
                    raw_metrics=raw_metrics,
                ),
                collector=collector,
            )

        meta = ReportMeta(
            stock_code=bundle.code,
            stock_name=bundle.stock_name,
            market_prefix=bundle.market_prefix,
            org_type=bundle.org_type,
            is_financial=is_financial,
            financial_reason=financial_reason,
            latest_report_period=view.period(display_years[0]),
            report_periods=[view.period(year) for year in display_years],
            lookback_years=lookback,
            audit_opinions={
                view.period(year): bundle.audit_opinions.get(view.period(year))
                for year in display_years
            },
            unit_notes={
                "amount": "亿元（原始数据单位为元，已除以 1e8；保留 4 位小数）",
                "ratio": "小数（0.9193 表示 91.93%，未做 ×100）",
                "yoy": "小数（-0.0120 表示同比 -1.20%）",
                "turnover": "次（年度周转次数）",
                "period": "报告期为完整会计年度，如 2025年报",
            },
            engine_version=ENGINE_VERSION,
            generated_at=dt.datetime.now().isoformat(timespec="seconds"),
            data_source=self._build_data_source_meta(bundle),
        )

        return FinancialReport(
            meta=meta,
            raw_metrics=raw_metrics,
            growth_profitability=growth,
            dupont=dupont,
            cashflow_quality=cashflow,
            forensic=forensic,
            efficiency=efficiency,
            cross_validation=cross_validation,
            warnings=collector.items,
            summary=summary,
        )

    # ------------------------------------------------------------------
    # 元信息 / 金融类识别
    # ------------------------------------------------------------------
    @staticmethod
    def _build_data_source_meta(bundle: StatementBundle) -> DataSourceMeta:
        interfaces = {
            key: str(bundle.provenance.get(key, {}).get("interface", ""))
            for key in bundle.frames
        }
        fetched_at = {
            key: str(bundle.provenance.get(key, {}).get("fetched_at", "")) for key in bundle.frames
        }
        cache_hit = {
            key: bool(bundle.provenance.get(key, {}).get("cache_hit", False)) for key in bundle.frames
        }
        cache_paths = {
            key: str(bundle.provenance.get(key, {}).get("cache_path") or "") for key in bundle.frames
        }
        return DataSourceMeta(
            provider=bundle.provider,
            upstream=bundle.upstream,
            interfaces=interfaces,
            fetched_at=fetched_at,
            cache_hit=cache_hit,
            cache_paths=cache_paths,
            as_of_date=bundle.as_of.date().isoformat(),
        )

    @staticmethod
    def _detect_financial(view: _StatementView) -> tuple[bool, str]:
        """判定是否为金融类企业（决定 Beneish 与存货/收现比类规则是否适用）。"""
        org_type = view.bundle.org_type.strip()
        if org_type in FINANCIAL_ORG_TYPES:
            return True, f"数据源 ORG_TYPE='{org_type}'，属金融行业企业。"
        if org_type and org_type not in {"通用", "未知", ""}:
            return True, f"数据源 ORG_TYPE='{org_type}'，非'通用'工商企业口径，按金融类谨慎处理。"
        missing = [
            column
            for column in INDUSTRIAL_MARKER_COLUMNS
            if not view.has_column("balance_sheet", column)
        ]
        if len(missing) >= 2:
            return True, (
                "资产负债表缺少工商企业报表的必备科目 " f"{missing}，报表结构符合金融类口径。"
            )
        return False, f"数据源 ORG_TYPE='{org_type or '通用'}'，为工商企业口径。"

    # ------------------------------------------------------------------
    # 通用计算助手（全部遵守"分母为 0 → None + 警告"）
    # ------------------------------------------------------------------
    @staticmethod
    def _div(
        numerator: Optional[float],
        denominator: Optional[float],
        *,
        collector: WarningCollector,
        label: str,
        period: str,
        require_positive_denominator: bool = False,
        min_abs_denominator: float = ZERO_DENOMINATOR_EPS,
    ) -> Optional[float]:
        """安全除法：任一侧为 None / 分母近似 0 / 要求正分母时返回 None 并登记警告。"""
        if numerator is None or denominator is None:
            return None
        if require_positive_denominator and denominator <= min_abs_denominator:
            collector.add(
                "non_positive_denominator",
                f"报告期 {period} 的指标「{label}」分母为 {denominator:.6g}（非正数），"
                f"比率无经济意义，返回 null。",
                period=period,
                field=label,
            )
            return None
        if abs(denominator) < min_abs_denominator:
            collector.zero_denominator(label, period, denominator)
            return None
        return numerator / denominator

    @classmethod
    def _yoy(
        cls,
        current: Optional[float],
        previous: Optional[float],
        *,
        collector: WarningCollector,
        label: str,
        period: str,
    ) -> Optional[float]:
        """同比变动 = (本期 - 上期) / |上期|。

        用绝对值作分母是为了在上期为负时仍保持"改善为正、恶化为负"的方向含义
        （例如 -10 → 5 的同比应为 +150%，而不是 -150%）。
        """
        if current is None or previous is None:
            return None
        if abs(previous) < ZERO_DENOMINATOR_EPS:
            collector.zero_denominator(f"{label}同比", period, previous)
            return None
        return (current - previous) / abs(previous)

    @staticmethod
    def _mean2(first: Optional[float], second: Optional[float]) -> Optional[float]:
        """两点平均（用于期初期末均值）；任一缺失则返回 None（不做单点替代以免失真）。"""
        if first is None or second is None:
            return None
        return (first + second) / 2.0

    @classmethod
    def _cagr(
        cls,
        end: Optional[float],
        start: Optional[float],
        periods: int,
        *,
        collector: WarningCollector,
        label: str,
        period: str,
    ) -> Optional[float]:
        """复合年均增长率 = (期末/期初)^(1/n) - 1；期初必须为正。"""
        if end is None or start is None or periods <= 0:
            return None
        if start <= ZERO_DENOMINATOR_EPS:
            collector.add(
                "non_positive_base",
                f"「{label}」区间起点 {start:.6g}（非正数），复合增长率无意义，返回 null。",
                period=period,
                field=label,
            )
            return None
        if end <= 0:
            collector.add(
                "non_positive_end",
                f"「{label}」区间终点 {end:.6g}（非正数），复合增长率无意义，返回 null。",
                period=period,
                field=label,
            )
            return None
        return (end / start) ** (1.0 / periods) - 1.0

    # ------------------------------------------------------------------
    # 维度 0：原始科目快照
    # ------------------------------------------------------------------
    def _build_raw_metrics(
        self, view: _StatementView, display_years: Sequence[int]
    ) -> dict[str, RawYearMetrics]:
        raw: dict[str, RawYearMetrics] = {}
        for year in display_years:
            period = view.period(year)
            payload: dict[str, Optional[float]] = {}
            for table, field_name, column, label in RAW_FIELD_SPEC:
                payload[field_name] = view.amount(table, column, year, label)
            raw[period] = RawYearMetrics(
                report_period=period, report_year=year, **payload  # type: ignore[arg-type]
            )
        return raw

    # ------------------------------------------------------------------
    # 维度 1：核心成长与盈利趋势
    # ------------------------------------------------------------------
    def _compute_growth(
        self,
        view: _StatementView,
        display_years: Sequence[int],
        is_financial: bool,
    ) -> GrowthProfitability:
        ctx = view.ctx
        rows: list[YearGrowth] = []

        for year in display_years:
            period = view.period(year)

            total_revenue = view.total_revenue(year)
            total_revenue_prev = view.total_revenue(year - 1)
            revenue = view.sales(year)
            revenue_prev = view.sales(year - 1)
            cost = view.amount("profit_sheet", "OPERATE_COST", year, "营业成本")
            net_profit = view.amount("profit_sheet", "NETPROFIT", year, "净利润")
            parent_net_profit = view.amount("profit_sheet", "PARENT_NETPROFIT", year, "归母净利润")
            parent_net_profit_prev = view.amount(
                "profit_sheet", "PARENT_NETPROFIT", year - 1, "归母净利润"
            )
            deducted = view.amount(
                "profit_sheet", "DEDUCT_PARENT_NETPROFIT", year, "扣非归母净利润"
            )
            deducted_prev = view.amount(
                "profit_sheet", "DEDUCT_PARENT_NETPROFIT", year - 1, "扣非归母净利润"
            )
            net_profit_prev = view.amount("profit_sheet", "NETPROFIT", year - 1, "净利润")

            # 毛利率 =（营业收入 - 营业成本）/ 营业收入 —— 金融类企业无此概念
            if is_financial:
                gross_margin = None
                ctx.not_applicable("毛利率", period, "金融类企业不区分营业成本，毛利率指标无经济含义")
            else:
                gross_margin = (
                    None
                    if revenue is None or cost is None
                    else self._div(
                        revenue - cost,
                        revenue,
                        collector=ctx,
                        label="毛利率",
                        period=period,
                        require_positive_denominator=True,
                    )
                )

            # 净利率 = 净利润 / 营业收入
            net_margin = self._div(
                net_profit,
                revenue,
                collector=ctx,
                label="净利率",
                period=period,
                require_positive_denominator=True,
            )
            parent_net_margin = self._div(
                parent_net_profit,
                revenue,
                collector=ctx,
                label="归母净利率",
                period=period,
                require_positive_denominator=True,
            )

            reported_yoy = view.raw(
                "profit_sheet", "TOTAL_OPERATE_INCOME_YOY", year, "营业总收入同比(源)", quiet=True
            )
            reported_parent_yoy = view.raw(
                "profit_sheet", "PARENT_NETPROFIT_YOY", year, "归母净利润同比(源)", quiet=True
            )

            rows.append(
                YearGrowth(
                    report_period=period,
                    report_year=year,
                    total_operating_revenue=total_revenue,
                    total_operating_revenue_yoy=round_ratio(
                        self._yoy(
                            total_revenue,
                            total_revenue_prev,
                            collector=ctx,
                            label="营业总收入",
                            period=period,
                        )
                    ),
                    revenue_yoy_reported=(
                        None if reported_yoy is None else round_ratio(reported_yoy / 100.0)
                    ),
                    operating_revenue=revenue,
                    operating_revenue_yoy=round_ratio(
                        self._yoy(
                            revenue, revenue_prev, collector=ctx, label="营业收入", period=period
                        )
                    ),
                    parent_net_profit=parent_net_profit,
                    parent_net_profit_yoy=round_ratio(
                        self._yoy(
                            parent_net_profit,
                            parent_net_profit_prev,
                            collector=ctx,
                            label="归母净利润",
                            period=period,
                        )
                    ),
                    parent_net_profit_yoy_reported=(
                        None if reported_parent_yoy is None else round_ratio(reported_parent_yoy / 100.0)
                    ),
                    deducted_parent_net_profit=deducted,
                    deducted_parent_net_profit_yoy=round_ratio(
                        self._yoy(
                            deducted,
                            deducted_prev,
                            collector=ctx,
                            label="扣非归母净利润",
                            period=period,
                        )
                    ),
                    net_profit=net_profit,
                    net_profit_yoy=round_ratio(
                        self._yoy(
                            net_profit, net_profit_prev, collector=ctx, label="净利润", period=period
                        )
                    ),
                    gross_margin=round_ratio(gross_margin),
                    net_margin=round_ratio(net_margin),
                    parent_net_margin=round_ratio(parent_net_margin),
                )
            )

        # ---- 区间复合增长率 ----
        latest_year, earliest_year = display_years[0], display_years[-1]
        periods = latest_year - earliest_year
        cagr = CagrSummary(
            window_start=view.period(earliest_year),
            window_end=view.period(latest_year),
            periods=periods,
            revenue_cagr=round_ratio(
                self._cagr(
                    view.total_revenue(latest_year),
                    view.total_revenue(earliest_year),
                    periods,
                    collector=ctx,
                    label="营业总收入",
                    period=view.period(latest_year),
                )
            ),
            parent_net_profit_cagr=round_ratio(
                self._cagr(
                    view.amount("profit_sheet", "PARENT_NETPROFIT", latest_year, "归母净利润"),
                    view.amount("profit_sheet", "PARENT_NETPROFIT", earliest_year, "归母净利润"),
                    periods,
                    collector=ctx,
                    label="归母净利润",
                    period=view.period(latest_year),
                )
            ),
            deducted_net_profit_cagr=round_ratio(
                self._cagr(
                    view.amount("profit_sheet", "DEDUCT_PARENT_NETPROFIT", latest_year, "扣非归母净利润"),
                    view.amount("profit_sheet", "DEDUCT_PARENT_NETPROFIT", earliest_year, "扣非归母净利润"),
                    periods,
                    collector=ctx,
                    label="扣非归母净利润",
                    period=view.period(latest_year),
                )
            ),
        )

        return GrowthProfitability(
            years=rows,
            cagr=cagr,
            trend_observations=self._growth_observations(rows),
        )

    @staticmethod
    def _growth_observations(rows: Sequence[YearGrowth]) -> list[str]:
        """由数值规则自动生成趋势观察（确定性文本，非 LLM 推断）。"""
        if not rows:
            return []
        observations: list[str] = []
        latest = rows[0]

        if latest.total_operating_revenue_yoy is not None:
            observations.append(
                f"{latest.report_period} 营业总收入 {latest.total_operating_revenue} 亿元，"
                f"同比 {latest.total_operating_revenue_yoy:+.2%}。"
            )
        if latest.parent_net_profit_yoy is not None:
            observations.append(
                f"{latest.report_period} 归母净利润 {latest.parent_net_profit} 亿元，"
                f"同比 {latest.parent_net_profit_yoy:+.2%}。"
            )

        # 营收连续下滑
        recent = rows[:2]
        if len(recent) == 2 and all(
            row.total_operating_revenue_yoy is not None and row.total_operating_revenue_yoy < 0
            for row in recent
        ):
            observations.append(
                f"营业总收入连续两年同比下滑（{recent[1].report_period} "
                f"{recent[1].total_operating_revenue_yoy:+.2%}，{recent[0].report_period} "
                f"{recent[0].total_operating_revenue_yoy:+.2%}），需核查需求端与渠道库存。"
            )

        # 毛利率变动
        if len(rows) >= 2 and rows[0].gross_margin is not None and rows[1].gross_margin is not None:
            delta = rows[0].gross_margin - rows[1].gross_margin
            if abs(delta) >= 0.01:
                direction = "提升" if delta > 0 else "下滑"
                observations.append(
                    f"毛利率同比{direction} {abs(delta) * 100:.2f} 个百分点"
                    f"（{rows[1].gross_margin:.2%} → {rows[0].gross_margin:.2%}）。"
                )

        # 扣非占比：识别利润对非经常性损益的依赖
        if (
            latest.parent_net_profit is not None
            and latest.deducted_parent_net_profit is not None
            and abs(latest.parent_net_profit) > ZERO_DENOMINATOR_EPS
        ):
            ratio = latest.deducted_parent_net_profit / latest.parent_net_profit
            if ratio < 0.8:
                observations.append(
                    f"扣非归母净利润仅为归母净利润的 {ratio:.2%}，"
                    f"利润对非经常性损益依赖较高，需核查政府补助/资产处置等一次性项目。"
                )
        return observations

    # ------------------------------------------------------------------
    # 维度 2：杜邦分析
    # ------------------------------------------------------------------
    def _dupont_row(self, view: _StatementView, year: int) -> YearDuPont:
        ctx = view.ctx
        period = view.period(year)

        # 三个因子统一采用「营业收入」口径：净利率、周转率分子分母同源，
        # 使 ROE = 净利率 × 周转率 × 权益乘数 精确成立（收入被约掉，与口径选择无关）。
        revenue = view.sales(year)
        net_profit = view.amount("profit_sheet", "NETPROFIT", year, "净利润")
        parent_net_profit = view.amount("profit_sheet", "PARENT_NETPROFIT", year, "归母净利润")

        assets = view._scale("balance_sheet", "TOTAL_ASSETS", year, "资产总计")
        assets_prev = view._scale("balance_sheet", "TOTAL_ASSETS", year - 1, "资产总计")
        equity = view._scale("balance_sheet", "TOTAL_EQUITY", year, "所有者权益合计")
        equity_prev = view._scale("balance_sheet", "TOTAL_EQUITY", year - 1, "所有者权益合计")
        parent_equity = view._scale(
            "balance_sheet", "TOTAL_PARENT_EQUITY", year, "归母所有者权益合计"
        )

        avg_assets = self._mean2(assets, assets_prev)
        avg_equity = self._mean2(equity, equity_prev)
        if avg_assets is None:
            ctx.add(
                "missing_average",
                f"报告期 {period} 缺少期初或期末总资产，平均总资产无法计算，杜邦拆解返回 null。",
                period=period,
                field="average_total_assets",
            )
        if avg_equity is None:
            ctx.add(
                "missing_average",
                f"报告期 {period} 缺少期初或期末所有者权益，平均权益无法计算，杜邦拆解返回 null。",
                period=period,
                field="average_total_equity",
            )

        net_margin = self._div(
            net_profit,
            revenue,
            collector=ctx,
            label="销售净利率",
            period=period,
            require_positive_denominator=True,
        )
        turnover = self._div(
            revenue,
            avg_assets,
            collector=ctx,
            label="总资产周转率",
            period=period,
            require_positive_denominator=True,
        )
        multiplier = self._div(
            avg_assets,
            avg_equity,
            collector=ctx,
            label="权益乘数",
            period=period,
            require_positive_denominator=True,
        )

        if None in (net_margin, turnover, multiplier):
            roe_dupont: Optional[float] = None
        else:
            roe_dupont = net_margin * turnover * multiplier  # type: ignore[operator]
        roe_direct = self._div(
            net_profit,
            avg_equity,
            collector=ctx,
            label="ROE(直接法)",
            period=period,
            require_positive_denominator=True,
        )
        identity_error = (
            None
            if roe_dupont is None or roe_direct is None
            else roe_dupont - roe_direct
        )

        roe_parent = self._div(
            parent_net_profit,
            parent_equity,
            collector=ctx,
            label="归母摊薄ROE",
            period=period,
            require_positive_denominator=True,
        )

        return YearDuPont(
            report_period=period,
            report_year=year,
            net_margin=round_ratio(net_margin),
            total_asset_turnover=round_ratio(turnover),
            equity_multiplier=round_ratio(multiplier),
            roe_dupont=round_ratio(roe_dupont),
            roe_direct=round_ratio(roe_direct),
            identity_check_error=round_ratio(identity_error),
            roe_parent_diluted=round_ratio(roe_parent),
            average_total_assets=round_amount(avg_assets),
            average_total_equity=round_amount(avg_equity),
        )

    def _compute_dupont(
        self, view: _StatementView, display_years: Sequence[int]
    ) -> DuPontAnalysis:
        cache: dict[int, YearDuPont] = {}

        def get(year: int) -> Optional[YearDuPont]:
            if year not in cache:
                if year - 1 not in view.years:
                    return None
                cache[year] = self._dupont_row(view, year)
            return cache[year]

        rows: list[YearDuPont] = []
        for year in display_years:
            current = get(year)
            if current is None:
                continue
            previous = get(year - 1)
            if previous is not None:
                effects = self._dupont_effects(current, previous) or {}
                current = current.model_copy(
                    update={
                        "net_margin_delta": round_ratio(
                            _diff(current.net_margin, previous.net_margin)
                        ),
                        "asset_turnover_delta": round_ratio(
                            _diff(current.total_asset_turnover, previous.total_asset_turnover)
                        ),
                        "equity_multiplier_delta": round_ratio(
                            _diff(current.equity_multiplier, previous.equity_multiplier)
                        ),
                        "roe_delta": round_ratio(_diff(current.roe_dupont, previous.roe_dupont)),
                        # 三因子贡献落库：前端瀑布图直接读取，避免 UI 层重算（铁律 1）
                        "net_margin_effect": round_ratio(effects.get("销售净利率")),
                        "asset_turnover_effect": round_ratio(effects.get("总资产周转率")),
                        "equity_multiplier_effect": round_ratio(effects.get("权益乘数")),
                    }
                )
            rows.append(current)

        dominant_driver, driver_note = self._dupont_driver(rows)
        return DuPontAnalysis(
            formula=DUPONT_FORMULA,
            years=rows,
            dominant_driver=dominant_driver,
            driver_note=driver_note,
        )

    @staticmethod
    def _dupont_effects(
        latest: YearDuPont, previous: YearDuPont
    ) -> Optional[dict[str, float]]:
        """连环替代法：把 ROE 变动完整归因到三个因子（无残差）。

        连环替代法（教材标准做法）：
            ROE0 = nm0 × to0 × em0
            R1 = nm1 × to0 × em0   → 净利率贡献   = R1 - ROE0
            R2 = nm1 × to1 × em0   → 周转率贡献   = R2 - R1
            R3 = nm1 × to1 × em1   → 权益乘数贡献 = R3 - R2
            三者之和恰好等于 ROE1 - ROE0。

        返回值为**小数**（0.0123 表示 1.23 个百分点），与全项目比率口径一致。
        """
        factors = (latest.net_margin, latest.total_asset_turnover, latest.equity_multiplier)
        base = (previous.net_margin, previous.total_asset_turnover, previous.equity_multiplier)
        if any(value is None for value in factors + base):
            return None

        nm1, to1, em1 = (float(value) for value in factors)  # type: ignore[arg-type]
        nm0, to0, em0 = (float(value) for value in base)  # type: ignore[arg-type]

        r0 = nm0 * to0 * em0
        r1 = nm1 * to0 * em0
        r2 = nm1 * to1 * em0
        r3 = nm1 * to1 * em1

        return {
            "销售净利率": r1 - r0,
            "总资产周转率": r2 - r1,
            "权益乘数": r3 - r2,
            "_roe_from": r0,
            "_roe_to": r3,
        }

    @classmethod
    def _dupont_driver(cls, rows: Sequence[YearDuPont]) -> tuple[Optional[str], Optional[str]]:
        """基于三因子贡献生成"主导因子 + 归因说明"文本。"""
        if len(rows) < 2:
            return None, None
        latest, previous = rows[0], rows[1]
        effects = cls._dupont_effects(latest, previous)
        if effects is None:
            return None, None

        dominant = max(
            ("销售净利率", "总资产周转率", "权益乘数"),
            key=lambda key: abs(effects[key]),
        )
        r0 = effects["_roe_from"]
        r3 = effects["_roe_to"]
        total = r3 - r0
        note = (
            f"{latest.report_period} ROE 为 {r3:.2%}，较 {previous.report_period} 的 {r0:.2%} "
            f"变动 {total * 100:+.2f} 个百分点。按连环替代法归因："
            f"销售净利率 {effects['销售净利率'] * 100:+.2f}pct、"
            f"总资产周转率 {effects['总资产周转率'] * 100:+.2f}pct、"
            f"权益乘数 {effects['权益乘数'] * 100:+.2f}pct，"
            f"主导因子为「{dominant}」。"
        )
        return dominant, note

    # ------------------------------------------------------------------
    # 维度 3：盈余质量与现金流检验
    # ------------------------------------------------------------------
    def _compute_cashflow(
        self,
        view: _StatementView,
        display_years: Sequence[int],
        is_financial: bool,
    ) -> CashFlowQuality:
        ctx = view.ctx
        rows: list[YearCashFlow] = []
        notes: list[str] = []

        for year in display_years:
            period = view.period(year)
            ocf = view.amount("cash_flow_sheet", "NETCASH_OPERATE", year, "经营活动现金流量净额")
            net_profit = view.amount("profit_sheet", "NETPROFIT", year, "净利润")
            total_assets = view._scale("balance_sheet", "TOTAL_ASSETS", year, "资产总计")
            cash_from_sales = view.amount(
                "cash_flow_sheet", "SALES_SERVICES", year, "销售商品提供劳务收到的现金"
            )
            revenue = view.sales(year)
            capex = view.amount("cash_flow_sheet", "CONSTRUCT_LONG_ASSET", year, "购建长期资产支付的现金")

            # 净现比：净利润必须为正才有经济意义（净利润为负时"比值"会误导方向）
            if net_profit is not None and net_profit <= 0:
                net_cash_ratio = None
                ctx.add(
                    "net_profit_non_positive",
                    f"报告期 {period} 净利润为 {net_profit:.4f} 亿元（非正），"
                    f"净现比无经济意义，返回 null。",
                    period=period,
                    field="净现比",
                )
            else:
                net_cash_ratio = self._div(
                    ocf,
                    net_profit,
                    collector=ctx,
                    label="净现比",
                    period=period,
                    require_positive_denominator=True,
                )

            collection_ratio = self._div(
                cash_from_sales,
                revenue,
                collector=ctx,
                label="收现比",
                period=period,
                require_positive_denominator=True,
            )

            fcf = None if ocf is None or capex is None else ocf - capex
            fcf_margin = self._div(
                fcf,
                revenue,
                collector=ctx,
                label="FCF/营业收入",
                period=period,
                require_positive_denominator=True,
            )
            ocf_to_revenue = self._div(
                ocf,
                revenue,
                collector=ctx,
                label="经营现金流/营业收入",
                period=period,
                require_positive_denominator=True,
            )
            accrual = self._div(
                (None if net_profit is None or ocf is None else net_profit - ocf),
                total_assets,
                collector=ctx,
                label="应计利润比率",
                period=period,
                require_positive_denominator=True,
            )

            if (
                collection_ratio is not None
                and collection_ratio > 1.0
                and not is_financial
                and year == display_years[0]
            ):
                ctx.add(
                    "collection_ratio_above_one",
                    f"报告期 {period} 收现比 {collection_ratio:.2%} 大于 1：该指标分子含增值税，"
                    f"13% 税率下理论基准约 1.13，超 1 属正常；同时反映预收/合同负债变动。",
                    level="info",
                    period=period,
                    field="收现比",
                )

            rows.append(
                YearCashFlow(
                    report_period=period,
                    report_year=year,
                    net_cash_from_operating=round_amount(ocf),
                    net_profit=round_amount(net_profit),
                    net_cash_ratio=round_ratio(net_cash_ratio),
                    cash_from_sales=round_amount(cash_from_sales),
                    operating_revenue=round_amount(revenue),
                    cash_collection_ratio=round_ratio(collection_ratio),
                    capex=round_amount(capex),
                    free_cash_flow=round_amount(fcf),
                    fcf_margin=round_ratio(fcf_margin),
                    operating_cash_to_revenue=round_ratio(ocf_to_revenue),
                    accrual_ratio=round_ratio(accrual),
                )
            )

        notes.append(
            "收现比 = 销售商品、提供劳务收到的现金 / 营业收入。分子含增值税，"
            "13% 税率下持续低于 1.13 说明回款质量偏弱；该比值 > 1 不代表异常。"
        )
        notes.append(
            "FCF ≈ 经营活动现金流量净额 - 购建固定资产/无形资产及其他长期资产支付的现金；"
            "为简化口径，未扣除非经常性资本开支与并购支出。"
        )
        if any(row.net_cash_ratio is not None and row.net_cash_ratio < 0 for row in rows):
            notes.append("存在净现比为负的年度：说明当期经营活动净流出，需结合扩张节奏与营运资本占用判断。")
        return CashFlowQuality(years=rows, notes=notes)

    # ------------------------------------------------------------------
    # 辅助维度：营运效率
    # ------------------------------------------------------------------
    def _compute_efficiency(
        self,
        view: _StatementView,
        display_years: Sequence[int],
        is_financial: bool,
    ) -> EfficiencyMetrics:
        ctx = view.ctx
        rows: list[YearEfficiency] = []

        if is_financial:
            ctx.not_applicable(
                "存货/应收/应付周转率",
                view.period(display_years[0]),
                "金融类企业不存在存货与经营性应收应付的周转概念",
            )
            return EfficiencyMetrics(years=[])

        for year in display_years:
            period = view.period(year)
            cost = view.amount("profit_sheet", "OPERATE_COST", year, "营业成本")
            revenue = view.sales(year)

            inventory = view._scale("balance_sheet", "INVENTORY", year, "存货")
            inventory_prev = view._scale("balance_sheet", "INVENTORY", year - 1, "存货")
            receivables = view._scale("balance_sheet", "ACCOUNTS_RECE", year, "应收账款")
            receivables_prev = view._scale("balance_sheet", "ACCOUNTS_RECE", year - 1, "应收账款")
            payables = view._scale("balance_sheet", "ACCOUNTS_PAYABLE", year, "应付账款")
            payables_prev = view._scale("balance_sheet", "ACCOUNTS_PAYABLE", year - 1, "应付账款")
            current_assets = view._scale(
                "balance_sheet", "TOTAL_CURRENT_ASSETS", year, "流动资产合计"
            )

            inventory_turnover = self._div(
                cost,
                self._mean2(inventory, inventory_prev),
                collector=ctx,
                label="存货周转率",
                period=period,
                require_positive_denominator=True,
            )
            receivables_turnover = self._div(
                revenue,
                self._mean2(receivables, receivables_prev),
                collector=ctx,
                label="应收账款周转率",
                period=period,
                require_positive_denominator=True,
            )
            payables_turnover = self._div(
                cost,
                self._mean2(payables, payables_prev),
                collector=ctx,
                label="应付账款周转率",
                period=period,
                require_positive_denominator=True,
            )

            # 应收账款规模过小时，周转率数值虽可计算但缺乏分析意义
            avg_ar = self._mean2(receivables, receivables_prev)
            if (
                avg_ar is not None
                and revenue is not None
                and revenue > ZERO_DENOMINATOR_EPS
                and avg_ar / revenue < 0.005
                and receivables_turnover is not None
            ):
                ctx.add(
                    "receivables_turnover_not_meaningful",
                    f"报告期 {period} 平均应收账款仅占营业收入 {avg_ar / revenue:.3%}，"
                    f"应收账款周转率 {receivables_turnover:.1f} 次（{365.0 / receivables_turnover:.0f} 天）"
                    f"数值虽可计算但缺乏分析意义（多为先款后货的商业模式）。",
                    level="info",
                    period=period,
                    field="应收账款周转率",
                )

            inventory_days = _days(inventory_turnover)
            receivables_days = _days(receivables_turnover)
            payables_days = _days(payables_turnover)
            ccc = (
                None
                if None in (inventory_days, receivables_days, payables_days)
                else inventory_days + receivables_days - payables_days  # type: ignore[operator]
            )

            rows.append(
                YearEfficiency(
                    report_period=period,
                    report_year=year,
                    inventory_turnover=round_ratio(inventory_turnover),
                    inventory_days=round_amount(inventory_days),
                    receivables_turnover=round_ratio(receivables_turnover),
                    receivables_days=round_amount(receivables_days),
                    payables_turnover=round_ratio(payables_turnover),
                    payables_days=round_amount(payables_days),
                    cash_conversion_cycle=round_amount(ccc),
                    inventory_to_current_assets=round_ratio(
                        self._div(
                            inventory,
                            current_assets,
                            collector=ctx,
                            label="存货占流动资产比重",
                            period=period,
                            require_positive_denominator=True,
                        )
                    ),
                )
            )
        return EfficiencyMetrics(years=rows)

    # ------------------------------------------------------------------
    # 维度 4：会计排雷
    # ------------------------------------------------------------------
    def _compute_forensic(
        self,
        view: _StatementView,
        display_years: Sequence[int],
        is_financial: bool,
    ) -> ForensicAnalysis:
        beneish = self._compute_beneish(view, display_years, is_financial)

        flags: list[RuleFlag] = [
            self._rule_cash_and_debt_double_high(view, display_years, is_financial),
            self._rule_paper_profit(view, display_years, is_financial),
            self._rule_inventory_pileup(view, display_years, is_financial),
            self._rule_audit_opinion(view, display_years),
            self._rule_goodwill_risk(view, display_years, is_financial),
            self._rule_nonrecurring_reliance(view, display_years),
        ]

        triggered = [flag for flag in flags if flag.triggered]
        high = [flag for flag in triggered if flag.severity == "high"]
        if high:
            risk_level = "high"
        elif triggered:
            risk_level = "medium"
        elif any(not flag.evaluated for flag in flags):
            risk_level = "low"
        else:
            risk_level = "low"

        ordered = sorted(
            triggered,
            key=lambda flag: {"high": 0, "medium": 1, "info": 2}[flag.severity],
        )
        findings: list[str] = [f"【{flag.name}】{flag.summary}" for flag in ordered]

        return ForensicAnalysis(
            beneish=beneish,
            rule_flags=flags,
            triggered_count=len(triggered),
            triggered_high_count=len(high),
            risk_level=risk_level,  # type: ignore[arg-type]
            key_findings=findings,
        )

    # -- Beneish M-Score ---------------------------------------------------
    def _compute_beneish(
        self,
        view: _StatementView,
        display_years: Sequence[int],
        is_financial: bool,
    ) -> BeneishAnalysis:
        ctx = view.ctx
        latest_year = display_years[0]
        latest_period = view.period(latest_year)
        prior_period = view.period(latest_year - 1)

        if is_financial:
            return BeneishAnalysis(
                applicable=False,
                reason=(
                    "Beneish M-Score 以工商企业的应收账款、存货、营业成本、固定资产折旧为输入，"
                    "金融类企业（银行/证券/保险）报表结构不包含这些科目，模型不适用。"
                ),
                latest_period=latest_period,
                prior_period=prior_period,
                variables=[],
                m_score=None,
                threshold=BENEISH_THRESHOLD,
                is_high_risk=None,
                risk_text="不适用：金融类企业请改用拨备覆盖率、不良率、流动性覆盖率等监管指标。",
                model_formula=_beneish_formula_text(),
                cautions=[
                    "金融类企业的舞弊识别应关注：不良贷款率与关注类贷款迁徙、拨备覆盖率、"
                    "非息收入占比、同业与表外业务规模。",
                ],
            )

        if latest_year - 1 not in view.years:
            return BeneishAnalysis(
                applicable=False,
                reason=f"缺少 {prior_period} 数据，无法计算 t 与 t-1 的同比类指数。",
                latest_period=latest_period,
                prior_period=prior_period,
                variables=[],
                m_score=None,
                threshold=BENEISH_THRESHOLD,
                is_high_risk=None,
                risk_text="不适用：数据不足。",
                model_formula=_beneish_formula_text(),
                cautions=[],
            )

        t, p = latest_year, latest_year - 1
        sales_t, sales_p = view.sales(t), view.sales(p)
        cost_t = view.amount("profit_sheet", "OPERATE_COST", t, "营业成本")
        cost_p = view.amount("profit_sheet", "OPERATE_COST", p, "营业成本")
        ar_t = view._scale("balance_sheet", "ACCOUNTS_RECE", t, "应收账款")
        ar_p = view._scale("balance_sheet", "ACCOUNTS_RECE", p, "应收账款")
        ca_t = view._scale("balance_sheet", "TOTAL_CURRENT_ASSETS", t, "流动资产合计")
        ca_p = view._scale("balance_sheet", "TOTAL_CURRENT_ASSETS", p, "流动资产合计")
        ppe_t = view._scale("balance_sheet", "FIXED_ASSET", t, "固定资产")
        ppe_p = view._scale("balance_sheet", "FIXED_ASSET", p, "固定资产")
        ta_t = view._scale("balance_sheet", "TOTAL_ASSETS", t, "资产总计")
        ta_p = view._scale("balance_sheet", "TOTAL_ASSETS", p, "资产总计")
        dep_t = view._scale("cash_flow_sheet", "FA_IR_DEPR", t, "固定资产折旧等")
        dep_p = view._scale("cash_flow_sheet", "FA_IR_DEPR", p, "固定资产折旧等")
        sga_t = _add_optional(
            view._scale("profit_sheet", "SALE_EXPENSE", t, "销售费用"),
            view._scale("profit_sheet", "MANAGE_EXPENSE", t, "管理费用"),
        )
        sga_p = _add_optional(
            view._scale("profit_sheet", "SALE_EXPENSE", p, "销售费用"),
            view._scale("profit_sheet", "MANAGE_EXPENSE", p, "管理费用"),
        )
        tcl_t = view._scale("balance_sheet", "TOTAL_CURRENT_LIAB", t, "流动负债合计")
        tcl_p = view._scale("balance_sheet", "TOTAL_CURRENT_LIAB", p, "流动负债合计")
        ltd_t = view.sum_amounts(
            "balance_sheet",
            (("LONG_LOAN", "长期借款"), ("BOND_PAYABLE", "应付债券"), ("LEASE_LIAB", "租赁负债")),
            t,
            "长期负债",
            all_missing_as_zero=True,
        )
        ltd_p = view.sum_amounts(
            "balance_sheet",
            (("LONG_LOAN", "长期借款"), ("BOND_PAYABLE", "应付债券"), ("LEASE_LIAB", "租赁负债")),
            p,
            "长期负债",
            all_missing_as_zero=True,
        )
        net_profit_t = view.amount("profit_sheet", "NETPROFIT", t, "净利润")
        ocf_t = view.amount("cash_flow_sheet", "NETCASH_OPERATE", t, "经营活动现金流量净额")

        # ---- DSRI：应收账款指数 = (AR_t/Sales_t) / (AR_{t-1}/Sales_{t-1}) ----
        ar_ratio_t = self._div(
            ar_t, sales_t, collector=ctx, label="DSRI-本期应收/收入", period=latest_period
        )
        ar_ratio_p = self._div(
            ar_p, sales_p, collector=ctx, label="DSRI-上期应收/收入", period=prior_period
        )
        dsri = self._div(
            ar_ratio_t, ar_ratio_p, collector=ctx, label="DSRI", period=latest_period
        )

        # ---- GMI：毛利率指数 = 上期毛利率 / 本期毛利率 ----
        gm_t = (
            None
            if sales_t is None or cost_t is None
            else self._div(
                sales_t - cost_t, sales_t, collector=ctx, label="本期毛利率", period=latest_period
            )
        )
        gm_p = (
            None
            if sales_p is None or cost_p is None
            else self._div(
                sales_p - cost_p, sales_p, collector=ctx, label="上期毛利率", period=prior_period
            )
        )
        gmi = self._div(gm_p, gm_t, collector=ctx, label="GMI", period=latest_period)

        # ---- AQI：资产质量指数 = [1-(CA+PPE)/TA]_t / [1-(CA+PPE)/TA]_{t-1} ----
        aq_t = _asset_quality(ca_t, ppe_t, ta_t)
        aq_p = _asset_quality(ca_p, ppe_p, ta_p)
        aqi = self._div(aq_t, aq_p, collector=ctx, label="AQI", period=latest_period)

        # ---- SGI：营业收入指数 = Sales_t / Sales_{t-1} ----
        sgi = self._div(sales_t, sales_p, collector=ctx, label="SGI", period=latest_period)

        # ---- DEPI：折旧率指数 = DepRate_{t-1} / DepRate_t ----
        dep_rate_t = _depreciation_rate(dep_t, ppe_t)
        dep_rate_p = _depreciation_rate(dep_p, ppe_p)
        depi = self._div(dep_rate_p, dep_rate_t, collector=ctx, label="DEPI", period=latest_period)

        # ---- SGAI：销售管理费用指数 ----
        sga_ratio_t = self._div(sga_t, sales_t, collector=ctx, label="SGAI-本期", period=latest_period)
        sga_ratio_p = self._div(sga_p, sales_p, collector=ctx, label="SGAI-上期", period=prior_period)
        sgai = self._div(sga_ratio_t, sga_ratio_p, collector=ctx, label="SGAI", period=latest_period)

        # ---- LVGI：杠杆指数 = [(TCL+LTD)/TA]_t / [(TCL+LTD)/TA]_{t-1} ----
        lev_t = _leverage_ratio(tcl_t, ltd_t, ta_t)
        lev_p = _leverage_ratio(tcl_p, ltd_p, ta_p)
        lvgi = self._div(lev_t, lev_p, collector=ctx, label="LVGI", period=latest_period)

        # ---- TATA：总应计项目 = (净利润 - 经营现金流) / 总资产 ----
        tata = self._div(
            None if net_profit_t is None or ocf_t is None else net_profit_t - ocf_t,
            ta_t,
            collector=ctx,
            label="TATA",
            period=latest_period,
            require_positive_denominator=True,
        )

        variables: list[BeneishVariable] = [
            _beneish_var(
                code="DSRI",
                name_cn="应收账款指数",
                name_en="Days Sales in Receivables Index",
                value=dsri,
                formula="(应收账款_t / 营业收入_t) ÷ (应收账款_t-1 / 营业收入_t-1)",
                inputs={"应收账款_t": ar_t, "应收账款_t-1": ar_p, "营业收入_t": sales_t,
                        "营业收入_t-1": sales_p},
                interpretation="> 1 说明应收账款相对收入扩张更快，可能通过放宽信用政策虚增收入。",
                note="中式报表仅取'应收账款'净额，未纳入应收票据（银行承兑为主，信用风险显著更低）。",
            ),
            _beneish_var(
                code="GMI",
                name_cn="毛利率指数",
                name_en="Gross Margin Index",
                value=gmi,
                formula="毛利率_t-1 ÷ 毛利率_t",
                inputs={"毛利率_t": gm_t, "毛利率_t-1": gm_p},
                interpretation="> 1 说明毛利率下滑，前景恶化时管理层更容易产生粉饰动机。",
            ),
            _beneish_var(
                code="AQI",
                name_cn="资产质量指数",
                name_en="Asset Quality Index",
                value=aqi,
                formula="[1-(流动资产+固定资产)/总资产]_t ÷ [1-(流动资产+固定资产)/总资产]_t-1",
                inputs={
                    "流动资产_t": ca_t,
                    "固定资产_t": ppe_t,
                    "总资产_t": ta_t,
                    "流动资产_t-1": ca_p,
                    "固定资产_t-1": ppe_p,
                    "总资产_t-1": ta_p,
                },
                interpretation="> 1 说明可摊销/减值性资产（无形、在建、其他长期资产）占比上升，存在递延费用资本化嫌疑。",
                note="中式固定资产取资产负债表'固定资产'净额；因缺少原值口径，AQI 为近似值。",
            ),
            _beneish_var(
                code="SGI",
                name_cn="营业收入指数",
                name_en="Sales Growth Index",
                value=sgi,
                formula="营业收入_t ÷ 营业收入_t-1",
                inputs={"营业收入_t": sales_t, "营业收入_t-1": sales_p},
                interpretation="> 1 表示收入增长；高增长公司面临更高的持续增长压力，舞弊动机更强。",
            ),
            _beneish_var(
                code="DEPI",
                name_cn="折旧率指数",
                name_en="Depreciation Index",
                value=depi,
                formula="折旧率_t-1 ÷ 折旧率_t，其中折旧率 = 折旧 /(折旧 + 固定资产净额)",
                inputs={"折旧_t": dep_t, "固定资产_t": ppe_t, "折旧_t-1": dep_p, "固定资产_t-1": ppe_p},
                interpretation="> 1 说明折旧率下降（延长折旧年限或调低折旧），可能虚增当期利润。",
                note="折旧取现金流量表'固定资产折旧、油气资产折耗、生产性生物资产折旧'；分母为净额近似。",
            ),
            _beneish_var(
                code="SGAI",
                name_cn="销售管理费用指数",
                name_en="SG&A Index",
                value=sgai,
                formula="(销售费用+管理费用)/营业收入 的 t 期 ÷ t-1 期",
                inputs={"销售费用+管理费用_t": sga_t, "营业收入_t": sales_t,
                        "销售费用+管理费用_t-1": sga_p, "营业收入_t-1": sales_p},
                interpretation="> 1 说明费用率上升、管理效率下降，属负面信号。",
                note="研发费用按新准则单列，不并入管理费用；如需与旧口径可比应另行加回。",
            ),
            _beneish_var(
                code="TATA",
                name_cn="总应计项目",
                name_en="Total Accruals to Total Assets",
                value=tata,
                formula="(净利润_t - 经营活动现金流量净额_t) ÷ 总资产_t",
                inputs={"净利润_t": net_profit_t, "经营现金流净额_t": ocf_t, "总资产_t": ta_t},
                interpretation="应计利润占比越高，盈余的现金支撑越弱，盈余管理概率越大。",
                note="这是 M-Score 中权重最大的变量（+4.679），对结果影响最显著。",
            ),
            _beneish_var(
                code="LVGI",
                name_cn="杠杆指数",
                name_en="Leverage Index",
                value=lvgi,
                formula="[(流动负债+长期负债)/总资产]_t ÷ [(流动负债+长期负债)/总资产]_t-1",
                inputs={
                    "流动负债_t": tcl_t,
                    "长期负债_t": ltd_t,
                    "总资产_t": ta_t,
                    "流动负债_t-1": tcl_p,
                    "长期负债_t-1": ltd_p,
                    "总资产_t-1": ta_p,
                },
                interpretation="> 1 说明杠杆上升，可能触及债务契约红线，从而产生操纵利润的动机。",
                note="中式长期负债取'长期借款+应付债券+租赁负债'。",
            ),
        ]

        contributions = {
            var.code: (None if var.value is None else var.value * var.weight) for var in variables
        }
        variables = [
            var.model_copy(update={"contribution": round_ratio(contributions[var.code])})
            for var in variables
        ]

        if any(value is None for value in contributions.values()):
            m_score = None
            missing = [code for code, value in contributions.items() if value is None]
            ctx.add(
                "beneish_incomplete",
                f"Beneish M-Score 有 {len(missing)} 个变量无法计算（{missing}），"
                f"综合得分返回 null，不做部分变量替代以免系统性高估/低估风险。",
                period=latest_period,
                field="M-Score",
            )
            is_high_risk = None
            risk_text = "无法判定：关键变量缺失，请补齐年报科目后重算。"
        else:
            m_score = BENEISH_INTERCEPT + sum(
                float(value) for value in contributions.values()  # type: ignore[arg-type]
            )
            is_high_risk = m_score > BENEISH_THRESHOLD
            risk_text = (
                f"M-Score = {m_score:.4f} > 阈值 {BENEISH_THRESHOLD}，"
                f"存在盈余操纵嫌疑（模型的统计含义为：舞弊样本概率显著高于非舞弊样本）。"
                if is_high_risk
                else f"M-Score = {m_score:.4f} ≤ 阈值 {BENEISH_THRESHOLD}，未触发作弊嫌疑区间。"
            )

        return BeneishAnalysis(
            applicable=True,
            reason=None,
            latest_period=latest_period,
            prior_period=prior_period,
            variables=variables,
            m_score=round_ratio(m_score),
            threshold=BENEISH_THRESHOLD,
            is_high_risk=is_high_risk,
            risk_text=risk_text,
            model_formula=_beneish_formula_text(),
            cautions=[
                "M-Score 是统计显著性模型而非定性结论，高分只代表'需要进一步核查'，不能直接认定造假。",
                "该模型以美国 1980-1990 年代样本训练，中式报表科目已做映射，绝对阈值 -1.78 仅供参考，"
                "实务中更应关注同一公司跨年度的分值趋势。",
                "金融类企业不适用；房地产、农业等特殊行业需结合行业特性另行调整。",
            ],
        )

    # -- 规则 1：存贷双高 ------------------------------------------------
    def _rule_cash_and_debt_double_high(
        self,
        view: _StatementView,
        display_years: Sequence[int],
        is_financial: bool,
    ) -> RuleFlag:
        ctx = view.ctx
        year = display_years[0]
        period = view.period(year)
        thresholds = self.thresholds

        if is_financial:
            return RuleFlag(
                code="CASH_LOAN_DOUBLE_HIGH",
                name="存贷双高嫌疑",
                category="存贷双高",
                evaluated=False,
                triggered=False,
                severity="info",
                summary="不适用：金融类企业吸收存款属于经营主业，'存贷双高'口径无意义。",
                skip_reason="金融类企业应改用存贷比、流动性覆盖率(LCR)、净稳定资金比例(NSFR)等监管指标。",
                thresholds=thresholds.to_dict(),
            )

        cash = view._scale("balance_sheet", "MONETARYFUNDS", year, "货币资金")
        cash_prev = view._scale("balance_sheet", "MONETARYFUNDS", year - 1, "货币资金")
        total_assets = view._scale("balance_sheet", "TOTAL_ASSETS", year, "资产总计")
        debt = view.sum_amounts(
            "balance_sheet", INTEREST_BEARING_DEBT_ITEMS, year, "有息负债", all_missing_as_zero=True
        )
        interest_income = view._scale("profit_sheet", "FE_INTEREST_INCOME", year, "利息收入")
        interest_expense = view._scale("profit_sheet", "FE_INTEREST_EXPENSE", year, "利息费用")
        avg_cash = self._mean2(cash, cash_prev)

        # ------------------------------------------------------------------
        # Phase 1.1 增强①：现金流量表「期末现金及现金等价物」与货币资金交叉勾稽
        # ------------------------------------------------------------------
        # 会计原理：资产负债表的「货币资金」包含库存现金、银行存款、其他货币资金；
        # 而现金流量表的「期末现金及现金等价物」只包含可随时支取的现金及 3 个月内到期、
        # 价值变动风险极小的现金等价物。因此：
        #   * 货币资金 > 期末现金等价物 → 差额近似为「受限货币资金」
        #     （保证金、质押/冻结存款、定期存款、被大股东占用资金等）
        #   * 期末现金等价物 > 货币资金 → 差额通常来自**合并范围内的财务公司/财务性子公司**
        #     将存放央行及同业的款项列入"拆出资金""存放同业款项"等科目，而非"货币资金"。
        #     这是报表列报口径差异，不是异常，必须与"受限资金"区分开，否则误判。
        ending_cce = view._scale("cash_flow_sheet", "END_CCE", year, "期末现金及现金等价物")
        total_revenue = view.total_revenue(year)
        operating_revenue = view.sales(year)
        revenue_gap = (
            None
            if total_revenue is None or operating_revenue is None
            else total_revenue - operating_revenue
        )

        cash_ratio = self._div(
            cash, total_assets, collector=ctx, label="货币资金/总资产", period=period,
            require_positive_denominator=True,
        )
        debt_ratio = self._div(
            debt, total_assets, collector=ctx, label="有息负债/总资产", period=period,
            require_positive_denominator=True,
        )
        implied_rate = self._div(
            interest_income, avg_cash, collector=ctx, label="隐含存款利率", period=period,
            require_positive_denominator=True,
        ) if (cash_ratio is not None and cash_ratio >= thresholds.min_cash_ratio_for_rate_test) else None

        # ---- 受限货币资金测算 ----
        restricted_cash: Optional[float] = None
        implied_restricted_cash_ratio: Optional[float] = None
        if (
            cash is not None
            and ending_cce is not None
            and cash > ZERO_DENOMINATOR_EPS
            and cash > ending_cce
        ):
            restricted_cash = cash - ending_cce
            implied_restricted_cash_ratio = self._div(
                restricted_cash, cash, collector=ctx, label="受限货币资金占比", period=period,
                require_positive_denominator=True,
            )

        restricted_cash_high = (
            implied_restricted_cash_ratio is not None
            and implied_restricted_cash_ratio > thresholds.restricted_cash_ratio_high
            and cash_ratio is not None
            and cash_ratio >= thresholds.restricted_cash_min_cash_ratio
        )
        # ---- "期末现金等价物 > 货币资金"：合并财务公司导致的列报口径差异 ----
        cce_exceeds_cash = (
            cash is not None
            and ending_cce is not None
            and cash > ZERO_DENOMINATOR_EPS
            and ending_cce > cash * thresholds.cce_excess_ratio
        )
        financial_company_hint = bool(
            cce_exceeds_cash and revenue_gap is not None and revenue_gap > ZERO_DENOMINATOR_EPS
        )

        high_cash = cash_ratio is not None and cash_ratio >= thresholds.cash_to_assets_high
        high_debt = debt_ratio is not None and debt_ratio >= thresholds.interest_debt_to_assets_high
        low_yield = (
            implied_rate is not None
            and cash_ratio is not None
            and cash_ratio >= thresholds.min_cash_ratio_for_rate_test
            and implied_rate < thresholds.implied_deposit_rate_warn
        )
        watch_yield = (
            implied_rate is not None
            and cash_ratio is not None
            and cash_ratio >= thresholds.min_cash_ratio_for_rate_test
            and thresholds.implied_deposit_rate_warn
            <= implied_rate
            < thresholds.implied_deposit_rate_watch
        )

        detail: list[str] = []
        if cash_ratio is not None:
            detail.append(
                f"{period} 货币资金 {cash:.4f} 亿元，占总资产 {cash_ratio:.2%}"
                f"（阈值 {thresholds.cash_to_assets_high:.0%}）。"
            )
        if debt_ratio is not None:
            detail.append(
                f"有息负债合计 {debt:.4f} 亿元，占总资产 {debt_ratio:.2%}"
                f"（阈值 {thresholds.interest_debt_to_assets_high:.0%}）。"
            )
        if implied_rate is not None:
            detail.append(
                f"利息收入 {interest_income:.4f} 亿元 / 平均货币资金 {avg_cash:.4f} 亿元"
                f" ≈ 隐含存款收益率 {implied_rate:.2%}"
                f"（预警线 {thresholds.implied_deposit_rate_warn:.1%}，观察线 "
                f"{thresholds.implied_deposit_rate_watch:.1%}）。"
            )

        # ---- 受限资金 / 财务公司勾稽结论写入 detail ----
        if implied_restricted_cash_ratio is not None and restricted_cash is not None:
            detail.append(
                f"{period} 货币资金 {cash:.4f} 亿元 > 期末现金及现金等价物 {ending_cce:.4f} 亿元，"
                f"差额 {restricted_cash:.4f} 亿元可近似视为「受限货币资金」，"
                f"占货币资金 {implied_restricted_cash_ratio:.2%}"
                f"（警戒线 {thresholds.restricted_cash_ratio_high:.0%}；"
                f"受限/库存现金、质押冻结、保证金等以年报附注为准）。"
            )
        elif cce_exceeds_cash and cash is not None and ending_cce is not None:
            gap_text = (
                f"，同时营业总收入与营业收入差额 {revenue_gap:.4f} 亿元（主要为利息收入等非主营收入）"
                if revenue_gap is not None
                else ""
            )
            detail.append(
                f"{period} 期末现金及现金等价物 {ending_cce:.4f} 亿元显著大于货币资金科目 "
                f"{cash:.4f} 亿元（{ending_cce / cash:.2f} 倍）{gap_text}："
                f"通常系合并集团财务公司（存放央行及同业款项未列入货币资金科目所致），"
                f"属合并范围内的列报口径差异，不应据此判定为资金异常或受限资金，"
                f"但仍需核对财务公司的资金归集与上存情况。"
            )
        elif cash is not None and ending_cce is not None:
            detail.append(
                f"{period} 期末现金及现金等价物 {ending_cce:.4f} 亿元 与 货币资金 "
                f"{cash:.4f} 亿元基本匹配，未出现'货币资金大于可动用现金'的受限迹象。"
            )

        # 只有当有息负债具备一定规模时，"一边存款一边付息"的描述才有意义
        material_debt = (
            debt is not None
            and total_assets is not None
            and total_assets > ZERO_DENOMINATOR_EPS
            and debt / total_assets >= 0.01
        )
        if interest_expense is not None and interest_expense > 0 and material_debt:
            detail.append(
                f"同期利息费用 {interest_expense:.4f} 亿元：一边持有大额货币资金、一边支付利息费用，"
                f"需核查资金是否被占用或存在受限资金。"
            )

        triggered = bool((high_cash and high_debt) or low_yield or restricted_cash_high)
        severity: str = (
            "high"
            if ((high_cash and high_debt) or restricted_cash_high)
            else ("medium" if low_yield else "info")
        )

        fired: list[str] = []
        if high_cash and high_debt:
            fired.append(
                f"货币资金占总资产 {cash_ratio:.2%}（>15%）且有息负债占比 {debt_ratio:.2%}（>20%），"
                f"构成'存贷双高'"
            )
        if low_yield:
            fired.append(
                f"隐含存款收益率仅 {implied_rate:.2%}"
                f"（<{thresholds.implied_deposit_rate_warn:.1%}），大额存款收益异常"
            )
        if restricted_cash_high:
            fired.append(
                f"受限货币资金占货币资金 {implied_restricted_cash_ratio:.2%}"
                f"（>{thresholds.restricted_cash_ratio_high:.0%}）且货币资金占总资产 "
                f"{cash_ratio:.2%}，资金可动用性存疑"
            )

        if fired:
            summary = "触发：" + "；".join(fired) + "。"
        elif watch_yield:
            summary = (
                f"观察级：隐含存款收益率 {implied_rate:.2%} 略低于合理区间"
                f"（{thresholds.implied_deposit_rate_warn:.1%}~{thresholds.implied_deposit_rate_watch:.1%}），"
                f"尚不构成预警，建议持续跟踪。"
            )
            severity = "info"
        else:
            summary = (
                f"未触发：货币资金占总资产 {cash_ratio if cash_ratio is None else format(cash_ratio, '.2%')}、"
                f"有息负债占比 {debt_ratio if debt_ratio is None else format(debt_ratio, '.2%')}，"
                f"隐含存款收益率 {implied_rate if implied_rate is None else format(implied_rate, '.2%')}。"
            )

        if financial_company_hint and not restricted_cash_high:
            summary += "期末现金等价物高于货币资金，疑似合并财务公司口径差异，已排除受限资金判定。"

        return RuleFlag(
            code="CASH_LOAN_DOUBLE_HIGH",
            name="存贷双高嫌疑",
            category="存贷双高",
            evaluated=True,
            triggered=triggered,
            severity=severity,  # type: ignore[arg-type]
            summary=summary,
            detail=detail,
            evidence={
                "monetary_funds": round_amount(cash),
                "total_assets": round_amount(total_assets),
                "cash_to_assets": round_ratio(cash_ratio),
                "interest_bearing_debt": round_amount(debt),
                "interest_debt_to_assets": round_ratio(debt_ratio),
                "interest_income": round_amount(interest_income),
                "average_monetary_funds": round_amount(avg_cash),
                "implied_deposit_rate": round_ratio(implied_rate),
                # ---- Phase 1.1 新增（追加键，不改变既有键语义）----
                "ending_cash_and_equivalents": round_amount(ending_cce),
                "restricted_cash": round_amount(restricted_cash),
                "implied_restricted_cash_ratio": round_ratio(implied_restricted_cash_ratio),
                "restricted_cash_high": restricted_cash_high,
                "cce_exceeds_cash": cce_exceeds_cash,
                "financial_company_hint": financial_company_hint,
                "revenue_gap_total_minus_operating": round_amount(revenue_gap),
                "debt_composition": {
                    label: round_amount(view._scale("balance_sheet", column, year, label))
                    for column, label in INTEREST_BEARING_DEBT_ITEMS
                },
            },
            thresholds=thresholds.to_dict(),
            interpretation_hint=(
                "若触发，请核查：①货币资金受限情况（保证金/质押/冻结，见附注'货币资金'明细）；"
                "②是否存在大股东资金占用或存贷挂钩的强制存款；"
                "③利息收入与货币资金规模、期限结构是否匹配（活期 vs 定期）；"
                "④若期末现金等价物大于货币资金，优先确认为合并财务公司的列报差异而非资金风险。"
            ),
        )

    # -- 规则 2：纸面富贵 ------------------------------------------------
    def _rule_paper_profit(
        self,
        view: _StatementView,
        display_years: Sequence[int],
        is_financial: bool,
    ) -> RuleFlag:
        ctx = view.ctx
        year = display_years[0]
        period = view.period(year)
        thresholds = self.thresholds
        detail: list[str] = []

        # 子条件 1：连续两年净利润为正但净现比 < 0.7
        ratios: list[tuple[str, Optional[float], Optional[float], Optional[float]]] = []
        for y in display_years[:2]:
            ocf = view._scale("cash_flow_sheet", "NETCASH_OPERATE", y, "经营活动现金流量净额")
            net_profit = view._scale("profit_sheet", "NETPROFIT", y, "净利润")
            ratio = (
                self._div(ocf, net_profit, collector=ctx, label="净现比", period=view.period(y),
                          require_positive_denominator=True)
                if net_profit is not None and net_profit > 0
                else None
            )
            ratios.append((view.period(y), net_profit, ocf, ratio))

        cash_profit_flag = (
            len(ratios) == 2
            and all(item[1] is not None and item[1] > 0 for item in ratios)
            and all(
                item[3] is not None and item[3] < thresholds.net_cash_ratio_low for item in ratios
            )
        )
        for label, net_profit, ocf, ratio in ratios:
            if net_profit is None or net_profit <= 0:
                continue
            detail.append(
                f"{label} 净利润 {net_profit:.4f} 亿元为正，经营现金流净额 "
                f"{'缺失' if ocf is None else f'{ocf:.4f} 亿元'}，"
                f"净现比 {'无法计算' if ratio is None else f'{ratio:.2%}'}"
                f"（阈值 {thresholds.net_cash_ratio_low:.0%}）。"
            )

        # 子条件 2：营收增长但应收账款增速显著高于营收增速
        revenue = view.sales(year)
        revenue_prev = view.sales(year - 1)
        receivables = view._scale("balance_sheet", "ACCOUNTS_RECE", year, "应收账款")
        receivables_prev = view._scale("balance_sheet", "ACCOUNTS_RECE", year - 1, "应收账款")
        revenue_growth = self._yoy(
            revenue, revenue_prev, collector=ctx, label="营业收入", period=period
        )
        receivables_growth = self._yoy(
            receivables, receivables_prev, collector=ctx, label="应收账款", period=period
        )
        gap = (
            None
            if revenue_growth is None or receivables_growth is None
            else receivables_growth - revenue_growth
        )

        # 重要性原则（Materiality）：应收账款必须达到营业收入的 1% 才有分析意义。
        # 先款后货的商业模式（如白酒）应收账款基数极小（茅台仅占营收 0.006%），
        # 任何微小的绝对波动都会放大为百倍增速，属统计噪音而非财务异常，故前置过滤。
        receivables_to_revenue = self._div(
            receivables,
            revenue,
            collector=ctx,
            label="应收账款/营业收入",
            period=period,
            require_positive_denominator=True,
        )
        material_receivables = (
            receivables_to_revenue is not None
            and receivables_to_revenue >= thresholds.min_receivables_to_revenue
        )
        growth_gap_exceeded = gap is not None and gap > thresholds.ar_growth_gap
        receivable_flag = bool(
            revenue_growth is not None
            and revenue_growth > 0
            and growth_gap_exceeded
            and material_receivables
        )

        if revenue_growth is not None and receivables_growth is not None:
            detail.append(
                f"{period} 营业收入同比 {revenue_growth:+.2%}，应收账款同比 {receivables_growth:+.2%}，"
                f"增速差 {gap * 100:+.2f} 个百分点（预警线 {thresholds.ar_growth_gap * 100:.0f}pct）。"
            )
        if receivables_to_revenue is not None:
            detail.append(
                f"{period} 应收账款 {receivables:.4f} 亿元，占营业收入 "
                f"{_pct_text(receivables_to_revenue)}"
                f"（重要性门槛 {thresholds.min_receivables_to_revenue:.0%}）。"
            )
        if growth_gap_exceeded and not material_receivables:
            detail.append(
                f"增速差虽已超过 {thresholds.ar_growth_gap * 100:.0f} 个百分点，但应收账款仅占营业收入 "
                f"{_pct_text(receivables_to_revenue)}，"
                f"低于重要性水平 {thresholds.min_receivables_to_revenue:.0%}，"
                f"按重要性原则不触发预警（避免先款后货企业因极小应收基数波动误报）。"
            )

        triggered = cash_profit_flag or receivable_flag
        if is_financial:
            severity = "info"
        else:
            severity = "high" if cash_profit_flag else ("medium" if receivable_flag else "info")

        if triggered:
            parts = []
            if cash_profit_flag:
                parts.append("连续两年净利润为正但净现比低于 0.7，利润缺乏现金支撑")
            if receivable_flag:
                if revenue_growth is not None and receivables_growth is not None:
                    parts.append(
                        f"营收增长 {revenue_growth:+.2%} 而应收账款增速 "
                        f"{receivables_growth:+.2%}，增速差超 "
                        f"{thresholds.ar_growth_gap * 100:.0f} 个百分点"
                    )
                else:
                    parts.append("应收账款增速显著高于营收增速")
            summary = "触发：" + "；".join(parts) + "（纸面富贵特征）。"
        else:
            summary = "未触发：净现比与应收账款增速均未出现'纸面富贵'组合特征。"

        return RuleFlag(
            code="PAPER_PROFIT",
            name="纸面富贵预警",
            category="盈余质量",
            evaluated=True,
            triggered=triggered,
            severity=severity,  # type: ignore[arg-type]
            summary=summary,
            detail=detail,
            evidence={
                "net_cash_ratio_by_year": {
                    item[0]: round_ratio(item[3]) for item in ratios
                },
                "revenue_growth": round_ratio(revenue_growth),
                "receivables_growth": round_ratio(receivables_growth),
                "growth_gap_pp": None if gap is None else round(gap, 6),
                "receivables": round_amount(receivables),
                # ---- Phase 1.1 新增：重要性原则判定要素 ----
                "receivables_to_revenue": round_ratio(receivables_to_revenue),
                "material_receivables": material_receivables,
                "growth_gap_exceeded": growth_gap_exceeded,
                "min_receivables_to_revenue": thresholds.min_receivables_to_revenue,
            },
            thresholds=thresholds.to_dict(),
            interpretation_hint=(
                "若触发，请核查：①应收账款账龄结构与坏账计提比例是否突变；"
                "②是否通过放宽信用政策换取收入；③经销商/客户集中度与期后回款情况。"
                "（注：应收账款占营业收入低于重要性门槛时不预警，属统计噪音。）"
            ),
        )

    # -- 规则 3：存货积压 ------------------------------------------------
    def _rule_inventory_pileup(
        self,
        view: _StatementView,
        display_years: Sequence[int],
        is_financial: bool,
    ) -> RuleFlag:
        ctx = view.ctx
        year = display_years[0]
        period = view.period(year)
        thresholds = self.thresholds

        if is_financial:
            return RuleFlag(
                code="INVENTORY_PILEUP",
                name="存货积压预警",
                category="资产质量",
                evaluated=False,
                triggered=False,
                severity="info",
                summary="不适用：金融类企业无存货科目。",
                skip_reason="金融类报表结构不含存货，该规则不适用。",
                thresholds=thresholds.to_dict(),
            )

        years_needed = [year, year - 1, year - 2]
        turnovers: dict[str, Optional[float]] = {}
        shares: dict[str, Optional[float]] = {}
        detail: list[str] = []

        for y in years_needed:
            p = view.period(y)
            cost = view._scale("profit_sheet", "OPERATE_COST", y, "营业成本")
            inv = view._scale("balance_sheet", "INVENTORY", y, "存货")
            inv_prev = view._scale("balance_sheet", "INVENTORY", y - 1, "存货")
            current_assets = view._scale("balance_sheet", "TOTAL_CURRENT_ASSETS", y, "流动资产合计")
            turnovers[p] = self._div(
                cost, self._mean2(inv, inv_prev), collector=ctx, label="存货周转率",
                period=p, require_positive_denominator=True,
            )
            shares[p] = self._div(
                inv, current_assets, collector=ctx, label="存货占流动资产比重",
                period=p, require_positive_denominator=True,
            )

        ordered_periods = [view.period(y) for y in years_needed]
        turn_values = [turnovers[p] for p in ordered_periods]
        share_values = [shares[p] for p in ordered_periods]

        turnover_declining = (
            all(value is not None for value in turn_values)
            and turn_values[0] < turn_values[1] < turn_values[2]  # type: ignore[operator]
        )
        share_rising = (
            all(value is not None for value in share_values)
            and share_values[0] > share_values[1] > share_values[2]  # type: ignore[operator]
        )
        material = share_values[0] is not None and share_values[0] >= thresholds.inventory_share_min
        triggered = bool(turnover_declining and share_rising and material)

        for p in ordered_periods:
            turnover = turnovers[p]
            share = shares[p]
            detail.append(
                f"{p} 存货周转率 {'缺失' if turnover is None else f'{turnover:.4f} 次'}"
                f"（{'-' if turnover in (None, 0) else f'{365.0 / turnover:.0f}'} 天），"
                f"存货占流动资产 {'缺失' if share is None else f'{share:.2%}'}。"
            )

        if triggered:
            summary = (
                f"触发：存货周转率连续两年下滑（{turn_values[2]:.4f} → {turn_values[1]:.4f} → "
                f"{turn_values[0]:.4f} 次），同时存货占流动资产比重连续抬升"
                f"（{share_values[2]:.2%} → {share_values[1]:.2%} → {share_values[0]:.2%}）。"
            )
        elif not all(value is not None for value in turn_values):
            summary = "无法完整判定：存货周转率所需的成本或存货科目存在缺失。"
        else:
            summary = "未触发：存货周转率与存货占流动资产比重未同时出现'下滑 + 抬升'组合。"

        return RuleFlag(
            code="INVENTORY_PILEUP",
            name="存货积压预警",
            category="资产质量",
            evaluated=True,
            triggered=triggered,
            severity="medium" if triggered else "info",
            summary=summary,
            detail=detail,
            evidence={
                "inventory_turnover_by_year": {p: round_ratio(v) for p, v in turnovers.items()},
                "inventory_share_by_year": {p: round_ratio(v) for p, v in shares.items()},
                "turnover_declining": turnover_declining,
                "share_rising": share_rising,
                "material_share": material,
            },
            thresholds=thresholds.to_dict(),
            interpretation_hint=(
                "若触发，请区分'主动战略备货'与'被动积压'：核查年报存货明细"
                "（原材料/在产品/库存商品占比）、跌价准备计提比例、以及产销量与在手订单。"
                "白酒（基酒陈化）、资源品、芯片等行业存货增长可能属正常经营节奏。"
            ),
        )

    # -- 规则 4：审计意见 ------------------------------------------------
    def _rule_audit_opinion(
        self, view: _StatementView, display_years: Sequence[int]
    ) -> RuleFlag:
        period = view.period(display_years[0])
        opinion = view.bundle.audit_opinions.get(period)

        if opinion is None:
            return RuleFlag(
                code="AUDIT_OPINION_NON_STANDARD",
                name="非标准审计意见",
                category="审计与其他",
                evaluated=False,
                triggered=False,
                severity="info",
                summary="无法判定：数据源未提供该年度的审计意见类型。",
                skip_reason="数据源 OPINION_TYPE 为空，请以年报'审计报告'章节为准。",
            )

        non_standard = "标准无保留" not in opinion
        return RuleFlag(
            code="AUDIT_OPINION_NON_STANDARD",
            name="非标准审计意见",
            category="审计与其他",
            evaluated=True,
            triggered=non_standard,
            severity="high" if non_standard else "info",
            summary=(
                f"触发：{period} 审计意见为「{opinion}」，非标准无保留意见。"
                if non_standard
                else f"未触发：{period} 审计意见为「{opinion}」。"
            ),
            detail=[f"{period} 审计意见类型：{opinion}。"],
            evidence={"audit_opinion": opinion},
            interpretation_hint=(
                "非标意见（保留/无法表示/否定）通常伴随持续经营或重大错报风险，"
                "需精读审计报告的'形成审计意见的基础'段落及关键审计事项(KAM)。"
                if non_standard
                else None
            ),
        )

    # -- 规则 5：商誉减值风险 --------------------------------------------
    def _rule_goodwill_risk(
        self,
        view: _StatementView,
        display_years: Sequence[int],
        is_financial: bool,
    ) -> RuleFlag:
        ctx = view.ctx
        year = display_years[0]
        period = view.period(year)
        threshold = self.thresholds.goodwill_to_parent_equity_high

        goodwill = view._scale("balance_sheet", "GOODWILL", year, "商誉")
        parent_equity = view._scale(
            "balance_sheet", "TOTAL_PARENT_EQUITY", year, "归母所有者权益合计"
        )
        ratio = self._div(
            goodwill, parent_equity, collector=ctx, label="商誉/归母净资产", period=period,
            require_positive_denominator=True,
        )
        triggered = ratio is not None and ratio > threshold

        if goodwill is None:
            summary = (
                f"未触发：{period} 数据源商誉科目为空（多数工商企业意味着无商誉敞口），"
                f"建议以年报附注复核。"
            )
        elif triggered:
            summary = (
                f"触发：商誉 {goodwill:.4f} 亿元，占归母净资产 {ratio:.2%}"
                f"（阈值 {threshold:.0%}），存在减值风险。"
            )
        else:
            summary = (
                f"未触发：商誉 {goodwill:.4f} 亿元，占归母净资产 "
                f"{'无法计算' if ratio is None else f'{ratio:.2%}'}（阈值 {threshold:.0%}）。"
            )

        goodwill_text = "缺失" if goodwill is None else f"{goodwill:.4f} 亿元"
        equity_text = "缺失" if parent_equity is None else f"{parent_equity:.4f} 亿元"

        return RuleFlag(
            code="GOODWILL_IMPAIRMENT_RISK",
            name="商誉减值风险",
            category="资产质量",
            evaluated=True,
            triggered=triggered,
            severity="medium" if triggered else "info",
            summary=summary,
            detail=[
                f"{period} 商誉 {goodwill_text}；归母净资产 {equity_text}；"
                f"商誉/归母净资产 {'无法计算' if ratio is None else f'{ratio:.2%}'}"
                f"（阈值 {threshold:.0%}）。"
            ],
            evidence={
                "goodwill": round_amount(goodwill),
                "parent_equity": round_amount(parent_equity),
                "goodwill_to_parent_equity": round_ratio(ratio),
                "is_financial": is_financial,
            },
            thresholds={"goodwill_to_parent_equity_high": threshold},
            interpretation_hint=(
                "商誉占比偏高时，重点核查：①被并购标的业绩承诺完成情况；"
                "②减值测试的关键假设（增长率、折现率）；③是否存在通过'大洗澡'一次性计提的动机。"
                if triggered
                else None
            ),
        )

    # -- 规则 6：非经常性损益依赖 ----------------------------------------
    def _rule_nonrecurring_reliance(
        self, view: _StatementView, display_years: Sequence[int]
    ) -> RuleFlag:
        ctx = view.ctx
        year = display_years[0]
        period = view.period(year)
        threshold = self.thresholds.nonrecurring_reliance_high

        parent_net_profit = view._scale("profit_sheet", "PARENT_NETPROFIT", year, "归母净利润")
        deducted = view._scale(
            "profit_sheet", "DEDUCT_PARENT_NETPROFIT", year, "扣非归母净利润"
        )

        if parent_net_profit is None or deducted is None:
            return RuleFlag(
                code="NONRECURRING_RELIANCE",
                name="非经常性损益依赖",
                category="盈余质量",
                evaluated=False,
                triggered=False,
                severity="info",
                summary="无法判定：归母净利润或扣非净利润缺失。",
                skip_reason="缺少 DEDUCT_PARENT_NETPROFIT 或 PARENT_NETPROFIT 科目。",
                thresholds={"nonrecurring_reliance_high": threshold},
            )

        nonrecurring = parent_net_profit - deducted
        # 依赖度按绝对值计算（保留原 `reliance` 键语义，向后兼容），
        # 但"方向"必须单独判定：非经常性净收益是风险，非经常性净支出反而是主业扎实的证据。
        reliance = self._div(
            abs(nonrecurring), abs(parent_net_profit), collector=ctx,
            label="非经常性损益依赖度", period=period, require_positive_denominator=True,
        )

        if nonrecurring > ZERO_DENOMINATOR_EPS:
            direction = "income"
        elif nonrecurring < -ZERO_DENOMINATOR_EPS:
            direction = "expense"
        else:
            direction = "zero"

        # ---- 三类情形的会计判定（Phase 1.1：修正正负方向）----
        # ① 保壳扭亏：归母为正但扣非为负 → 主营实际亏损，靠非经常性损益"做成"盈利
        reverse_turnaround = parent_net_profit > 0 > deducted
        # ② 非经常性净支出：扣非 ≥ 归母且归母为正 → 主业盈利扎实，被一次性支出拖累，不应预警
        nonrecurring_net_expense = direction == "expense" and parent_net_profit > 0
        # ③ 常规依赖：非经常性净收益且占归母净利润超过阈值
        normal_reliance = (
            direction == "income"
            and parent_net_profit > 0
            and reliance is not None
            and reliance > threshold
        )
        # ④ 亏损被非经常性净收益掩盖：亏损年份同样要提示，否则"越亏越靠补助"会被漏判
        loss_masked = (
            parent_net_profit <= 0
            and direction == "income"
            and reliance is not None
            and reliance > threshold
        )

        triggered = bool(reverse_turnaround or normal_reliance or loss_masked)
        severity = "high" if reverse_turnaround else ("medium" if triggered else "info")

        if reverse_turnaround:
            summary = (
                f"触发：归母净利润 {parent_net_profit:.4f} 亿元为正，但扣非归母净利润 "
                f"{deducted:.4f} 亿元为负——主营业务实际亏损，靠 {nonrecurring:.4f} 亿元"
                f"非经常性损益'扭亏为盈'，存在明显的保壳/规避退市动机。"
            )
        elif normal_reliance:
            summary = (
                f"触发：非经常性损益（净收益）{nonrecurring:.4f} 亿元，占归母净利润 "
                f"{reliance:.2%}（阈值 {threshold:.0%}），主业盈利质量需打折看待。"
            )
        elif loss_masked:
            summary = (
                f"提示：归母净利润 {parent_net_profit:.4f} 亿元为负，其中非经常性净收益 "
                f"{nonrecurring:.4f} 亿元掩盖了部分亏损（相当于亏损额的 {reliance:.2%}），"
                f"亏损本质未改变。"
            )
        elif nonrecurring_net_expense:
            summary = (
                f"未触发：扣非归母净利润 {deducted:.4f} 亿元 ≥ 归母净利润 "
                f"{parent_net_profit:.4f} 亿元，非经常性损益为**净支出** {abs(nonrecurring):.4f} 亿元"
                f"——主业盈利扎实，是一次性支出拖累了报表利润，不构成'主业质量打折'预警。"
            )
        elif direction == "income":
            summary = (
                f"未触发：非经常性损益（净收益）{nonrecurring:.4f} 亿元，占归母净利润 "
                f"{'无法计算' if reliance is None else f'{reliance:.2%}'}（阈值 {threshold:.0%}）。"
            )
        else:
            summary = (
                f"未触发：归母净利润 {parent_net_profit:.4f} 亿元与扣非归母净利润 "
                f"{deducted:.4f} 亿元同向为负，非经常性损益为净支出 {abs(nonrecurring):.4f} 亿元。"
            )

        judgment = {
            "income": "非经常性净收益（正向影响利润，需评估可持续性）",
            "expense": "非经常性净支出（负向影响利润，主业实则更强）",
            "zero": "无非经常性损益影响",
        }[direction]

        return RuleFlag(
            code="NONRECURRING_RELIANCE",
            name="非经常性损益依赖",
            category="盈余质量",
            evaluated=True,
            triggered=triggered,
            severity=severity,  # type: ignore[arg-type]
            summary=summary,
            detail=[
                f"{period} 归母净利润 {parent_net_profit:.4f} 亿元，"
                f"扣非归母净利润 {deducted:.4f} 亿元，差额（非经常性损益） {nonrecurring:.4f} 亿元。",
                f"方向判定：{judgment}；占归母净利润绝对值 "
                f"{'无法计算' if reliance is None else f'{reliance:.2%}'}（常规阈值 {threshold:.0%}）。",
                (
                    f"保壳扭亏判定：归母为正、扣非为负 → 主营业务实质亏损，"
                    f"报表盈利完全来自非经常性损益。"
                    if reverse_turnaround
                    else "保壳扭亏判定：未出现'归母为正而扣非为负'的组合。"
                ),
            ],
            evidence={
                "parent_net_profit": round_amount(parent_net_profit),
                "deducted_parent_net_profit": round_amount(deducted),
                "nonrecurring_amount": round_amount(nonrecurring),
                "reliance": round_ratio(reliance),
                # ---- Phase 1.1 新增（追加键）----
                "nonrecurring_direction": direction,
                "is_reverse_turnaround": reverse_turnaround,
                "is_nonrecurring_expense": nonrecurring_net_expense,
                "loss_masked": loss_masked,
            },
            thresholds={"nonrecurring_reliance_high": threshold},
            interpretation_hint=(
                "保壳扭亏是最典型的盈余操纵信号：请核查非经常性损益明细"
                "（政府补助/债务重组/资产处置/公允价值变动）、补助的可持续性，"
                "以及公司是否已触及退市风险警示或再融资的财务指标红线。"
                if reverse_turnaround
                else (
                    "需拆解非经常性损益构成：政府补助、资产处置、公允价值变动、债务重组等，"
                    "判断其可持续性及是否被用于平滑利润。"
                    if triggered
                    else None
                )
            ),
        )

    # ------------------------------------------------------------------
    # 汇总
    # ------------------------------------------------------------------
    def _build_summary(
        self,
        view: _StatementView,
        display_years: Sequence[int],
        growth: GrowthProfitability,
        dupont: DuPontAnalysis,
        forensic: ForensicAnalysis,
        raw_metrics: dict[str, RawYearMetrics],
    ) -> ReportSummary:
        total_fields = len(RAW_FIELD_SPEC) * len(display_years)
        missing_fields = sum(
            1
            for metrics in raw_metrics.values()
            for _table, field_name, _column, _label in RAW_FIELD_SPEC
            if getattr(metrics, field_name, None) is None
        )
        completeness = 0.0 if total_fields == 0 else 1.0 - missing_fields / total_fields

        findings: list[str] = []
        if dupont.years and dupont.years[0].roe_dupont is not None and not dupont.driver_note:
            findings.append(
                f"{dupont.years[0].report_period} ROE（杜邦）{dupont.years[0].roe_dupont:.2%}。"
            )
        if dupont.driver_note:
            findings.append(dupont.driver_note)
        findings.extend(growth.trend_observations[:2])
        findings.extend(forensic.key_findings)

        return ReportSummary(
            risk_level=forensic.risk_level,
            triggered_flags=forensic.triggered_count,
            high_severity_flags=forensic.triggered_high_count,
            data_quality=(
                f"原始科目完整度 {completeness:.1%}（{missing_fields}/{total_fields} 项缺失）；"
                f"共 {len(view.ctx)} 条数据质量警告。"
            ),
            missing_field_count=missing_fields,
            risk_disclaimer=(
                "风险等级仅由触发的规则数量与严重度机械推导，是尽调线索而非结论；"
                "规则命中的业务成因（如白酒基酒陈化导致的存货周转下降）必须结合年报附注与行业特性人工判断。"
            ),
            key_findings=findings,
        )


# ===========================================================================
# 纯函数辅助
# ===========================================================================
def _diff(current: Optional[float], previous: Optional[float]) -> Optional[float]:
    if current is None or previous is None:
        return None
    return current - previous


def _days(turnover: Optional[float]) -> Optional[float]:
    """周转率 → 周转天数（365 / 周转率）；周转率非正时无意义。"""
    if turnover is None or turnover <= ZERO_DENOMINATOR_EPS:
        return None
    return 365.0 / turnover


def _pct_text(value: Optional[float], missing: str = "无法计算") -> str:
    """百分比文本：极小比例自动提高精度，避免出现"占营业收入 0.00%"这类失真表述。"""
    if value is None:
        return missing
    if 0 < abs(value) < 0.0001:  # 小于 0.01% 时保留到 0.0001%
        return f"{value:.4%}"
    return f"{value:.2%}"


def _add_optional(first: Optional[float], second: Optional[float]) -> Optional[float]:
    """两个可选金额相加：任一缺失则返回 None（不做单边求和以免低估）。"""
    if first is None or second is None:
        return None
    return first + second


def _asset_quality(
    current_assets: Optional[float], fixed_assets: Optional[float], total_assets: Optional[float]
) -> Optional[float]:
    """AQI 的分子：1 -（流动资产 + 固定资产）/ 总资产。"""
    if current_assets is None or fixed_assets is None or total_assets is None:
        return None
    if abs(total_assets) < ZERO_DENOMINATOR_EPS:
        return None
    return 1.0 - (current_assets + fixed_assets) / total_assets


def _depreciation_rate(
    depreciation: Optional[float], fixed_assets: Optional[float]
) -> Optional[float]:
    """折旧率 = 折旧 /（折旧 + 固定资产净额）。"""
    if depreciation is None or fixed_assets is None:
        return None
    denominator = depreciation + fixed_assets
    if abs(denominator) < ZERO_DENOMINATOR_EPS:
        return None
    return depreciation / denominator


def _leverage_ratio(
    current_liabilities: Optional[float], long_term_debt: Optional[float], total_assets: Optional[float]
) -> Optional[float]:
    """杠杆 =（流动负债 + 长期负债）/ 总资产。"""
    if current_liabilities is None or long_term_debt is None or total_assets is None:
        return None
    if abs(total_assets) < ZERO_DENOMINATOR_EPS:
        return None
    return (current_liabilities + long_term_debt) / total_assets


def _beneish_var(
    *,
    code: str,
    name_cn: str,
    name_en: str,
    value: Optional[float],
    formula: str,
    inputs: dict[str, Optional[float]],
    interpretation: str,
    note: Optional[str] = None,
) -> BeneishVariable:
    return BeneishVariable(
        code=code,
        name_cn=name_cn,
        name_en=name_en,
        value=round_ratio(value),
        weight=BENEISH_WEIGHTS[code],
        contribution=None,
        formula=formula,
        inputs={key: round_amount(item) for key, item in inputs.items()},
        interpretation=interpretation,
        note=note,
    )


def _beneish_formula_text() -> str:
    return (
        "M = -4.84 + 0.920·DSRI + 0.528·GMI + 0.404·AQI + 0.892·SGI "
        "+ 0.115·DEPI - 0.172·SGAI + 4.679·TATA - 0.327·LVGI"
    )


def _partial_report(
    *,
    growth: GrowthProfitability,
    dupont: DuPontAnalysis,
    cashflow: CashFlowQuality,
    efficiency: EfficiencyMetrics,
    raw_metrics: dict[str, RawYearMetrics],
) -> dict[str, Any]:
    """给交叉校验模块用的轻量视图（只暴露它需要的已算结果）。"""
    return {
        "growth": growth,
        "dupont": dupont,
        "cashflow": cashflow,
        "efficiency": efficiency,
        "raw_metrics": raw_metrics,
    }


# ===========================================================================
# 对外便捷接口
# ===========================================================================
def analyze_stock(
    code: str,
    *,
    lookback_years: int = DEFAULT_LOOKBACK_YEARS,
    force_refresh: bool = False,
    include_cross_validation: bool = False,
    cache_dir: Optional[Path] = None,
    cache_ttl_hours: Optional[float] = None,
    as_of: Optional[dt.datetime] = None,
) -> FinancialReport:
    """一键分析（模块级便捷函数，等价于 ``FinancialEngine().analyze(...)``）。"""
    engine = FinancialEngine(cache_dir=cache_dir, cache_ttl_hours=cache_ttl_hours)
    return engine.analyze(
        code,
        lookback_years=lookback_years,
        force_refresh=force_refresh,
        as_of=as_of,
        include_cross_validation=include_cross_validation,
    )


def report_to_json(report: FinancialReport, indent: int = 2) -> str:
    """序列化为严格合法的 JSON（``allow_nan=False``，任何 NaN 都会暴露为错误）。"""
    payload = report.model_dump(mode="json")
    return json.dumps(payload, ensure_ascii=False, indent=indent, allow_nan=False)


def save_report(report: FinancialReport, output_dir: Optional[Path] = None) -> Path:
    """把报告写入 ``data/reports/{code}_{latest_period}_report.json``。"""
    ensure_directories()
    directory = Path(output_dir) if output_dir is not None else REPORT_OUTPUT_DIR
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{report.meta.stock_code}_{report.meta.latest_report_period}_report.json"
    target.write_text(report_to_json(report), encoding="utf-8")
    return target


# ===========================================================================
# 命令行入口
# ===========================================================================
def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m src.tools.financial_engine",
        description="FinSight-Agent 财务体检引擎：输入 6 位 A 股代码，输出结构化 JSON 报告。",
    )
    parser.add_argument("code", help="6 位 A 股代码，如 600519")
    parser.add_argument("--lookback", type=int, default=DEFAULT_LOOKBACK_YEARS, help="回溯年度数（默认 4）")
    parser.add_argument("--indent", type=int, default=2, help="JSON 缩进（默认 2）")
    parser.add_argument("--refresh", action="store_true", help="忽略本地缓存强制拉取")
    parser.add_argument("--cross-validation", action="store_true", help="启用与外部指标源的交叉校验")
    parser.add_argument("--output", type=Path, default=None, help="JSON 输出路径（默认仅打印）")
    parser.add_argument("--save", action="store_true", help="同时保存到 data/reports/")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _build_arg_parser().parse_args(argv)
    try:
        report = analyze_stock(
            args.code,
            lookback_years=args.lookback,
            force_refresh=args.refresh,
            include_cross_validation=args.cross_validation,
        )
    except (DataUnavailableError, ValueError) as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1

    payload = report_to_json(report, indent=args.indent)
    print(payload)

    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload, encoding="utf-8")
        print(f"[saved] {args.output}", file=sys.stderr)
    if args.save:
        print(f"[saved] {save_report(report)}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
