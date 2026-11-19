"""轻量稳健的同业横向对标工具（Phase 2）。

设计原则
--------
* **零新增数据源**：完全复用 Phase 1 已实盘验证的 :func:`analyze_stock`，自动享受本地
  CSV 缓存（TTL 12h），因此对标 3 家同业通常只需几秒。
* **禁止不稳定爬虫**：遵循 AGENTS.md §2.4 第 6 条——行业信息接口
  (`stock_individual_info_em`) 会触发 ProxyError，本模块**不使用**它，
  同业池由内置字典 + 调用方显式传入构成。
* **单点失败不影响整体**：任一同业取数失败只记入 ``skipped``，不抛错。

用法
----
>>> cmp = compare_peers("600519")                       # 用内置默认池
>>> cmp = compare_peers("600519", ["000858", "000568"])  # 手动指定
>>> cmp = compare_peers("600519", [])                    # 跳过同业对比
"""

from __future__ import annotations

import datetime as dt
from typing import Iterable, Optional, Sequence

from pydantic import BaseModel, ConfigDict, Field

from src.tools.financial_data import DataUnavailableError
from src.tools.financial_engine import analyze_stock
from src.tools.schemas import FinancialReport, RawYearMetrics

Amount = Optional[float]
Ratio = Optional[float]

#: 单次对标最多包含的同业数量（避免 Token 与耗时失控）
MAX_PEERS: int = 3

#: 营收同比超出该区间即视为"数据待核实"（疑似数据源口径异常 / 追溯重述），
#: 只做提示、不删数据（铁律 3：不静默丢弃证据）。
#: 已实测触发：000858 五粮液 2025 年报 891.75 亿 → 405.29 亿（-54.55%），
#: 且 2024 年报行同步被重述，明显不符合白酒行业常识，须先核实再用于对标结论。
PEER_YOY_ANOMALY: float = 0.40

#: 跨表勾稽上限：销售商品收到的现金 / 营业收入 的正常上限。
#: 分子含增值税，13% 税率下理论上限约 1.13；再叠加预收款/合同负债变动，1.6 已是极宽松上界。
#: 实测 000858 2025 年报该比值 2.26（914.00 / 405.29），说明利润表与现金流量表**同期不匹配**，
#: 属于比"同比异常"更硬的数据质量证据。
CASH_REVENUE_MISMATCH_MAX: float = 1.60

#: 内置典型行业可比池（代码 → 默认同业）。仅作为便捷回退，调用方可随时覆盖。
DEFAULT_PEER_POOLS: dict[str, list[str]] = {
    # 白酒
    "600519": ["000858", "000568"],  # 五粮液、泸州老窖
    "000858": ["600519", "000568"],
    "000568": ["600519", "000858"],
    "600809": ["000858", "000568"],  # 山西汾酒
    # 动力电池 / 新能源车
    "300750": ["002594", "300014"],  # 比亚迪、亿纬锂能
    "002594": ["300750", "601633"],  # 长城汽车
    "300014": ["300750", "002074"],  # 国轩高科
    # 家电
    "000333": ["000651", "600690"],  # 美的 ↔ 格力、海尔
    "000651": ["000333", "600690"],
    "600690": ["000333", "000651"],
    # 保险
    "601318": ["601601", "601628"],  # 中国平安 ↔ 太保、国寿
    # 医药
    "600276": ["000538", "600196"],  # 恒瑞医药 ↔ 云南白药、复星医药
}

#: 有息负债构成科目（与引擎保持一致，取自已验证的 self 口径）
_DEBT_FIELDS: tuple[str, ...] = (
    "short_term_loans",
    "long_term_loans",
    "bonds_payable",
    "current_portion_noncurrent_liabilities",
    "lease_liabilities",
    "short_term_bonds_payable",
)


class PeerMetricRow(BaseModel):
    """单家公司的核心对标指标（最新完整年度）。"""

    model_config = ConfigDict(extra="forbid")

    stock_code: str = Field(description="6 位代码")
    stock_name: str = Field(description="简称")
    is_target: bool = Field(description="是否为对标主体")
    latest_report_period: str = Field(description="最新完整年度，如 2025年报")
    revenue: Amount = Field(default=None, description="营业总收入（亿元）")
    revenue_yoy: Ratio = Field(default=None, description="营业总收入同比（小数）")
    gross_margin: Ratio = Field(default=None, description="毛利率（小数）")
    net_margin: Ratio = Field(default=None, description="净利率（小数）")
    roe_dupont: Ratio = Field(default=None, description="ROE（杜邦乘积，小数）")
    dupont_net_margin: Ratio = Field(default=None, description="杜邦因子①销售净利率（小数）")
    dupont_asset_turnover: Ratio = Field(default=None, description="杜邦因子②总资产周转率（次）")
    dupont_equity_multiplier: Ratio = Field(default=None, description="杜邦因子③权益乘数（倍）")
    net_cash_ratio: Ratio = Field(default=None, description="净现比（无量纲）")
    sale_cash_to_revenue: Ratio = Field(
        default=None, description="销售商品收到的现金 / 营业收入（跨表勾稽，正常上限约 1.13~1.6）"
    )
    inventory_days: Ratio = Field(default=None, description="存货周转天数（天）")
    receivables_days: Ratio = Field(default=None, description="应收账款周转天数（天）")
    interest_debt_ratio: Ratio = Field(default=None, description="有息负债 / 总资产（小数）")
    m_score: Ratio = Field(default=None, description="Beneish M-Score（分值）")
    risk_level: str = Field(description="综合风险等级")
    triggered_flags: int = Field(description="触发规则数")
    data_caution: Optional[str] = Field(
        default=None, description="数据可信度提示（如营收同比异常波动，疑似口径异常/重述）"
    )


class PeerComparison(BaseModel):
    """同业对标结果。"""

    model_config = ConfigDict(extra="forbid")

    target_code: str = Field(description="对标主体代码")
    lookback_years: int = Field(description="回溯年度数")
    peer_codes: list[str] = Field(description="实际参与对比的同业代码")
    pool_source: str = Field(description="同业池来源：内置默认 / 调用方指定")
    rows: list[PeerMetricRow] = Field(description="第一行为对标主体，其后为同业")
    skipped: list[str] = Field(default_factory=list, description="取数失败被跳过的同业及原因")
    markdown_table: str = Field(description="可直接嵌入报告的 Markdown 对标表（含单位）")
    generated_at: str = Field(description="生成时间（ISO 8601）")

    @property
    def available(self) -> bool:
        """是否至少有一家可比同业。"""
        return len(self.rows) > 1

    def fact_pack_section(self) -> str:
        """渲染为事实包的"同业对标"章节。"""
        if not self.available:
            return (
                "## 9. 同业横向对标\n"
                "- 本次未提供可比同业数据（--no-peers 或同业取数失败），"
                "请仅基于单公司历史序列评估，禁止编造行业均值或同业数据。"
            )
        notes = "\n".join(f"- 跳过：{item}" for item in self.skipped)
        return f"""## 9. 同业横向对标（最新完整年度，单位已标注）

{self.markdown_table}

- 同业池来源：{self.pool_source}
{notes}"""


def _fmt_amount(value: Optional[float]) -> str:
    return "数据缺失" if value is None else f"{value:.2f}"


def _fmt_pct(value: Optional[float]) -> str:
    return "数据缺失" if value is None else f"{value * 100:.2f}%"


def _fmt_num(value: Optional[float], digits: int = 2) -> str:
    return "数据缺失" if value is None else f"{value:.{digits}f}"


def _latest(rows: Iterable, period: str):
    for row in rows:
        if getattr(row, "report_period", None) == period:
            return row
    return None


def _interest_bearing_debt(metrics: RawYearMetrics) -> float:
    """有息负债合计（亿元）：已披露科目求和；全部为空按 0 处理（与引擎口径一致）。"""
    values = [getattr(metrics, field, None) for field in _DEBT_FIELDS]
    present = [value for value in values if value is not None]
    return float(sum(present)) if present else 0.0


def _build_row(report: FinancialReport) -> PeerMetricRow:
    period = report.meta.latest_report_period
    growth = _latest(report.growth_profitability.years, period)
    dupont = _latest(report.dupont.years, period)
    cash = _latest(report.cashflow_quality.years, period)
    efficiency = _latest(report.efficiency.years, period)
    raw = report.raw_metrics.get(period)

    debt_ratio: Optional[float] = None
    if raw is not None and raw.total_assets:
        debt_ratio = _interest_bearing_debt(raw) / raw.total_assets

    # 数据可信度护栏：两条互补的证据链
    # ① 同比异常（横向常识）② 跨表勾稽失败（纵向会计恒等）——后者更硬
    caution_parts: list[str] = []
    if (
        growth is not None
        and growth.total_operating_revenue_yoy is not None
        and abs(growth.total_operating_revenue_yoy) > PEER_YOY_ANOMALY
    ):
        caution_parts.append(
            f"营收同比 {growth.total_operating_revenue_yoy * 100:+.2f}% 超出常规波动区间"
            f"（±{PEER_YOY_ANOMALY:.0%}）"
        )

    sale_cash_ratio: Optional[float] = None
    if (
        cash is not None
        and cash.operating_revenue
        and cash.cash_from_sales is not None
        and cash.operating_revenue > 0
    ):
        sale_cash_ratio = cash.cash_from_sales / cash.operating_revenue
        if sale_cash_ratio > CASH_REVENUE_MISMATCH_MAX:
            caution_parts.append(
                f"销售商品收到的现金为营业收入的 {sale_cash_ratio:.2f} 倍"
                f"（正常含税上限约 1.13，容忍上界 {CASH_REVENUE_MISMATCH_MAX:.1f}），"
                f"利润表与现金流量表同期不匹配，疑为口径/期次错配"
            )

    caution = "；".join(caution_parts) + "，对标结论须先核实该年度报表" if caution_parts else None

    return PeerMetricRow(
        stock_code=report.meta.stock_code,
        stock_name=report.meta.stock_name,
        is_target=False,
        latest_report_period=period,
        revenue=None if growth is None else growth.total_operating_revenue,
        revenue_yoy=None if growth is None else growth.total_operating_revenue_yoy,
        gross_margin=None if growth is None else growth.gross_margin,
        net_margin=None if growth is None else growth.net_margin,
        roe_dupont=None if dupont is None else dupont.roe_dupont,
        dupont_net_margin=None if dupont is None else dupont.net_margin,
        dupont_asset_turnover=None if dupont is None else dupont.total_asset_turnover,
        dupont_equity_multiplier=None if dupont is None else dupont.equity_multiplier,
        net_cash_ratio=None if cash is None else cash.net_cash_ratio,
        sale_cash_to_revenue=sale_cash_ratio,
        inventory_days=None if efficiency is None else efficiency.inventory_days,
        receivables_days=None if efficiency is None else efficiency.receivables_days,
        interest_debt_ratio=debt_ratio,
        m_score=report.forensic.beneish.m_score,
        risk_level=report.forensic.risk_level,
        triggered_flags=report.forensic.triggered_count,
        data_caution=caution,
    )


def _markdown_table(rows: Sequence[PeerMetricRow]) -> str:
    """转置布局：指标为行、公司为列（更利于横向对比与 LLM 阅读）。"""
    columns = [f"{row.stock_code} {row.stock_name}" for row in rows]
    header = "| 指标 | " + " | ".join(columns) + " |"
    divider = "|---" * (len(columns) + 1) + "|"

    def line(label: str, values: Sequence[str]) -> str:
        return f"| {label} | " + " | ".join(values) + " |"

    metrics: list[tuple[str, Sequence[str]]] = [
        ("报告期", [row.latest_report_period for row in rows]),
        ("营业总收入（亿元）", [_fmt_amount(row.revenue) for row in rows]),
        ("营业总收入同比", [_fmt_pct(row.revenue_yoy) for row in rows]),
        ("毛利率", [_fmt_pct(row.gross_margin) for row in rows]),
        ("净利率", [_fmt_pct(row.net_margin) for row in rows]),
        ("ROE（杜邦）", [_fmt_pct(row.roe_dupont) for row in rows]),
        ("　└ 销售净利率", [_fmt_pct(row.dupont_net_margin) for row in rows]),
        ("　└ 总资产周转率（次）", [_fmt_num(row.dupont_asset_turnover, 4) for row in rows]),
        ("　└ 权益乘数（倍）", [_fmt_num(row.dupont_equity_multiplier, 4) for row in rows]),
        ("净现比", [_fmt_num(row.net_cash_ratio, 4) for row in rows]),
        ("收现/营收（跨表勾稽）", [_fmt_num(row.sale_cash_to_revenue, 2) for row in rows]),
        ("存货周转天数（天）", [_fmt_num(row.inventory_days, 1) for row in rows]),
        ("应收周转天数（天）", [_fmt_num(row.receivables_days, 1) for row in rows]),
        ("有息负债率", [_fmt_pct(row.interest_debt_ratio) for row in rows]),
        ("Beneish M-Score", [_fmt_num(row.m_score, 4) for row in rows]),
        ("综合风险等级", [row.risk_level for row in rows]),
        ("触发规则数", [str(row.triggered_flags) for row in rows]),
    ]
    body = "\n".join(line(label, values) for label, values in metrics)
    table = "\n".join([header, divider, body])

    cautions = [
        f"- **数据提示（{row.stock_code} {row.stock_name}）**：{row.data_caution}。"
        for row in rows
        if row.data_caution
    ]
    if cautions:
        table += (
            "\n\n> 同业数据可信度提示（来自确定性护栏，非模型判断）：\n"
            + "\n".join(cautions)
            + "\n> 涉及上述同业时，仅可作为「待核实参考」，不得据此得出排名或优劣结论。"
        )
    return table


def resolve_peer_codes(target_code: str, peer_codes: Optional[Sequence[str]]) -> tuple[list[str], str]:
    """解析同业代码列表并返回 (代码列表, 来源说明)。"""
    target = str(target_code).strip()
    if peer_codes is None:
        pool = DEFAULT_PEER_POOLS.get(target, [])
        source = (
            f"内置默认池（{target} → {', '.join(pool)}）" if pool else "内置默认池（该标的暂无预设，已跳过）"
        )
    else:
        pool = [str(code).strip() for code in peer_codes if str(code).strip()]
        source = "调用方指定"

    deduped: list[str] = []
    for code in pool:
        if code and code != target and code not in deduped:
            deduped.append(code)
    return deduped[:MAX_PEERS], source


def compare_peers(
    target_code: str,
    peer_codes: Optional[list[str]] = None,
    lookback_years: int = 3,
) -> PeerComparison:
    """生成目标公司与同业的最新年度核心指标对标表。

    Parameters
    ----------
    target_code:
        对标主体 6 位代码。
    peer_codes:
        同业代码列表。``None`` → 使用内置默认池；``[]`` → 跳过同业对比。
    lookback_years:
        回溯年度数（同业表只用最新年度，但需回溯以计算均值与周转率）。
    """
    codes, source = resolve_peer_codes(target_code, peer_codes)
    rows: list[PeerMetricRow] = []
    skipped: list[str] = []

    target_report = analyze_stock(target_code, lookback_years=max(2, lookback_years))
    target_row = _build_row(target_report)
    rows.append(target_row.model_copy(update={"is_target": True}))

    for code in codes:
        try:
            report = analyze_stock(code, lookback_years=max(2, lookback_years))
        except DataUnavailableError as exc:
            # 业务级提示（如新三板未收录）已是中文可读文案，不再叠加异常类名
            skipped.append(f"{code}：{exc}")
            continue
        except Exception as exc:  # noqa: BLE001 - 单家同业失败不影响整体
            skipped.append(f"{code}：{type(exc).__name__}: {exc}")
            continue
        rows.append(_build_row(report))

    return PeerComparison(
        target_code=target_report.meta.stock_code,
        lookback_years=max(2, lookback_years),
        peer_codes=[row.stock_code for row in rows[1:]],
        pool_source=source,
        rows=rows,
        skipped=skipped,
        markdown_table=_markdown_table(rows),
        generated_at=dt.datetime.now().isoformat(timespec="seconds"),
    )
