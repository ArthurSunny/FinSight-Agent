"""零歧义事实包（Fact Pack）格式化层 —— 消除"小数 / 百分数"误读幻觉。

为什么需要这一层（铁律 1 的延伸）
--------------------------------
Phase 1 输出的 JSON 中所有比率都是**小数**（`-0.0120` 表示 -1.20%）。直接把这些
JSON 丢给 LLM，会有相当高的概率被误读为 `-0.012%`（差 100 倍），这是最典型的
"格式性幻觉"——数字没错、引擎没错，错在表达。

本模块是**纯 Python 的确定性格式化层**，把 :class:`FinancialReport` 转成给 LLM 的
唯一事实来源：

* 所有比率 / YoY / CAGR / 杜邦贡献值 → 显式带 `%` 或 `pct`；
* 所有金额 → 显式带 `"亿元"`；
* 周转率 → `"次"`；天数 → `"天"`；M-Score → 明确标注阈值；
* 每条指标都带**报告期标签**（如 `2025年报`）；
* ``warnings`` 做**去重聚合**（把"近 4 年短期借款/长期借款/商誉…为空"压成一行），
  既省 Token，也避免几十条同质警告分散模型注意力。

同时导出 :class:`Anchor` 锚点列表，供 :mod:`src.agents.fact_check` 做数值防幻觉质检
——LLM 写出来的数字必须能在事实包里找到对应锚点。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal, Optional, Sequence

from src.tools.schemas import (
    EngineWarning,
    FinancialReport,
    RawYearMetrics,
    RuleFlag,
)

# ---------------------------------------------------------------------------
# 格式化原语（唯一入口，禁止在别处手写格式）
# ---------------------------------------------------------------------------
MISSING = "数据缺失"


def fmt_amount(value: Optional[float]) -> str:
    """金额 → ``"1720.5417 亿元"``。"""
    return MISSING if value is None else f"{value:.4f} 亿元"


def fmt_pct(value: Optional[float], *, signed: bool = False) -> str:
    """小数比率 → 百分数字符串。

    ``signed=True`` 用于同比/变动（``"-1.20%"``）；
    ``signed=False`` 用于水平值（``"91.18%"``）。
    """
    if value is None:
        return MISSING
    return f"{value * 100:+.2f}%" if signed else f"{value * 100:.2f}%"


def fmt_pct_point(value: Optional[float]) -> str:
    """百分点差（ROE 变动、贡献度）→ ``"-3.97pct"``。"""
    return MISSING if value is None else f"{value * 100:+.2f}pct"


def fmt_turnover(value: Optional[float]) -> str:
    """周转率 → ``"0.5602 次"``。"""
    return MISSING if value is None else f"{value:.4f} 次"


def fmt_days(value: Optional[float]) -> str:
    """天数 → ``"1419 天"``。"""
    return MISSING if value is None else f"{value:.1f} 天"


def fmt_multiple(value: Optional[float]) -> str:
    """倍数（权益乘数等）→ ``"1.2154 倍"``。"""
    return MISSING if value is None else f"{value:.4f} 倍"


def fmt_score(value: Optional[float]) -> str:
    """模型分值 → ``"-2.835126"``（4~6 位小数，不换算）。"""
    return MISSING if value is None else f"{value:.6f}"


def fmt_ratio(value: Optional[float]) -> str:
    """无量纲比值（净现比、收现比）→ 原样 4 位小数，避免被当成百分数。"""
    return MISSING if value is None else f"{value:.4f}"


# ---------------------------------------------------------------------------
# 锚点：供确定性 fact checker 使用
# ---------------------------------------------------------------------------
AnchorKind = Literal["amount", "percent", "ratio", "score", "turnover", "days"]


@dataclass(frozen=True)
class Anchor:
    """一个"必须出现且必须正确"的核心数值。

    Attributes
    ----------
    key:
        内部键名，便于定位。
    label:
        中文标签（报告中出现该标签或该数值即视为已引用）。
    raw:
        引擎原始值（金额=亿元；比率=小数）。
    kind:
        决定"报告中应该写成什么形态"。
    period:
        报告期标签，如 ``2025年报``。
    """

    key: str
    label: str
    raw: float
    kind: AnchorKind
    period: str
    tolerance_abs: float = 0.01

    @property
    def expected_number(self) -> float:
        """报告中应当出现的**数字字面量**（比率已 ×100）。"""
        return self.raw * 100 if self.kind == "percent" else self.raw

    def display(self) -> str:
        if self.kind == "amount":
            return fmt_amount(self.raw)
        if self.kind == "percent":
            return fmt_pct(self.raw)
        if self.kind == "turnover":
            return fmt_turnover(self.raw)
        if self.kind == "days":
            return fmt_days(self.raw)
        if self.kind == "score":
            return fmt_score(self.raw)
        return fmt_ratio(self.raw)


# ---------------------------------------------------------------------------
# 事实包
# ---------------------------------------------------------------------------
@dataclass
class FactPack:
    """给 LLM 的零歧义事实包。"""

    stock_code: str
    stock_name: str
    latest_period: str
    periods: list[str]
    text: str
    anchors: list[Anchor] = field(default_factory=list)
    peer_text: str = ""
    warning_summary: list[str] = field(default_factory=list)

    @property
    def estimated_tokens(self) -> int:
        """粗估 Token（中文按 1.6 字符/token）——仅用于成本观察。"""
        return int(len(self.text) / 1.6)

    def anchor_dicts(self) -> list[dict]:
        """序列化锚点，便于放进 LangGraph State。"""
        return [
            {
                "key": a.key,
                "label": a.label,
                "raw": a.raw,
                "expected_number": a.expected_number,
                "kind": a.kind,
                "period": a.period,
                "tolerance_abs": a.tolerance_abs,
                "display": a.display(),
            }
            for a in self.anchors
        ]


# ---------------------------------------------------------------------------
# warnings 去重聚合
# ---------------------------------------------------------------------------
_LABEL_RE = re.compile(r"科目\s+(.+?)\(([A-Z0-9_]+)\)")


def aggregate_warnings(warnings: Sequence[EngineWarning], max_lines: int = 12) -> list[str]:
    """把几十条同质警告压缩成几行摘要（省 Token、防注意力分散）。"""
    buckets: dict[str, list[EngineWarning]] = {}
    for item in warnings:
        buckets.setdefault(item.code, []).append(item)

    lines: list[str] = []

    missing = buckets.get("missing_item", []) + buckets.get("missing_column", [])
    if missing:
        labels: dict[str, set[str]] = {}
        for item in missing:
            label = item.field or "未知科目"
            matched = _LABEL_RE.search(item.message)
            if matched:
                label = matched.group(1)
            labels.setdefault(label, set()).add(item.period or "-")
        names = sorted(labels)
        shown = "、".join(names[:14]) + ("…" if len(names) > 14 else "")
        lines.append(
            f"· 科目为空（{len(names)} 个科目 / {len(missing)} 条记录）：{shown}。"
            f"相关指标一律返回 null，未以 0 填充，也不做单边求和。"
        )

    for code, items in buckets.items():
        if code in {"missing_item", "missing_column"}:
            continue
        suffix = f"（同类共 {len(items)} 条，已合并）" if len(items) > 1 else ""
        lines.append(f"· [{code}] {items[0].message}{suffix}")

    if len(lines) > max_lines:
        lines = lines[:max_lines] + [f"· 其余 {len(lines) - max_lines} 类提示已省略。"]
    return lines


# ---------------------------------------------------------------------------
# 构建器
# ---------------------------------------------------------------------------
_RAW_GROUPS: tuple[tuple[str, tuple[tuple[str, str], ...]], ...] = (
    (
        "利润表",
        (
            ("total_operating_revenue", "营业总收入"),
            ("operating_revenue", "营业收入"),
            ("operating_cost", "营业成本"),
            ("taxes_and_surcharges", "税金及附加"),
            ("selling_expense", "销售费用"),
            ("admin_expense", "管理费用"),
            ("rnd_expense", "研发费用"),
            ("finance_expense", "财务费用"),
            ("interest_income", "利息收入"),
            ("other_income", "其他收益"),
            ("investment_income", "投资收益"),
            ("operating_profit", "营业利润"),
            ("net_profit", "净利润"),
            ("parent_net_profit", "归母净利润"),
            ("deducted_parent_net_profit", "扣非归母净利润"),
        ),
    ),
    (
        "资产",
        (
            ("monetary_funds", "货币资金"),
            ("accounts_receivable", "应收账款"),
            ("notes_receivable", "应收票据"),
            ("inventory", "存货"),
            ("total_current_assets", "流动资产合计"),
            ("fixed_assets", "固定资产"),
            ("construction_in_progress", "在建工程"),
            ("goodwill", "商誉"),
            ("total_assets", "资产总计"),
        ),
    ),
    (
        "负债与权益",
        (
            ("short_term_loans", "短期借款"),
            ("long_term_loans", "长期借款"),
            ("bonds_payable", "应付债券"),
            ("current_portion_noncurrent_liabilities", "一年内到期的非流动负债"),
            ("lease_liabilities", "租赁负债"),
            ("accounts_payable", "应付账款"),
            ("contract_liabilities", "合同负债"),
            ("total_current_liabilities", "流动负债合计"),
            ("total_liabilities", "负债合计"),
            ("total_parent_equity", "归母所有者权益"),
            ("total_equity", "所有者权益合计"),
        ),
    ),
    (
        "现金流量表",
        (
            ("cash_from_sales", "销售商品提供劳务收到的现金"),
            ("net_cash_from_operating", "经营活动现金流量净额"),
            ("net_cash_from_investing", "投资活动现金流量净额"),
            ("net_cash_from_financing", "筹资活动现金流量净额"),
            ("capex", "资本开支（购建长期资产）"),
            ("ending_cash_and_equivalents", "期末现金及现金等价物"),
            ("depreciation_amortization", "固定资产折旧等"),
        ),
    ),
)

#: 规则证据里需要显式带单位的键
_EVIDENCE_UNITS: dict[str, str] = {
    "monetary_funds": "亿元",
    "total_assets": "亿元",
    "interest_bearing_debt": "亿元",
    "interest_income": "亿元",
    "average_monetary_funds": "亿元",
    "ending_cash_and_equivalents": "亿元",
    "restricted_cash": "亿元",
    "revenue_gap_total_minus_operating": "亿元",
    "receivables": "亿元",
    "goodwill": "亿元",
    "parent_equity": "亿元",
    "parent_net_profit": "亿元",
    "deducted_parent_net_profit": "亿元",
    "nonrecurring_amount": "亿元",
    "cash_to_assets": "%",
    "interest_debt_to_assets": "%",
    "implied_deposit_rate": "%",
    "implied_restricted_cash_ratio": "%",
    "receivables_to_revenue": "%",
    "revenue_growth": "%",
    "receivables_growth": "%",
    "reliance": "%",
    "goodwill_to_parent_equity": "%",
    "growth_gap_pp": "pct",
}


class FactPackBuilder:
    """把 :class:`FinancialReport` 渲染成零歧义事实包。"""

    def __init__(self, *, include_raw: bool = True, include_peer: bool = True) -> None:
        self.include_raw = include_raw
        self.include_peer = include_peer

    # -- 对外 ---------------------------------------------------------------
    def build(self, report: FinancialReport, peer_text: str = "") -> FactPack:
        meta = report.meta
        periods = list(meta.report_periods)
        latest = meta.latest_report_period

        blocks: list[str] = []
        blocks.append(self._header(report))
        blocks.append(self._section_growth(report))
        blocks.append(self._section_dupont(report))
        blocks.append(self._section_cashflow(report))
        blocks.append(self._section_efficiency(report))
        if self.include_raw:
            blocks.append(self._section_raw(report))
        blocks.append(self._section_beneish(report))
        blocks.append(self._section_rules(report))
        blocks.append(self._section_quality(report))
        if self.include_peer and peer_text:
            blocks.append(peer_text)

        text = "\n\n".join(block for block in blocks if block.strip())
        return FactPack(
            stock_code=meta.stock_code,
            stock_name=meta.stock_name,
            latest_period=latest,
            periods=periods,
            text=text,
            anchors=self._build_anchors(report),
            peer_text=peer_text,
            warning_summary=aggregate_warnings(report.warnings),
        )

    # -- 各区块 -------------------------------------------------------------
    @staticmethod
    def _header(report: FinancialReport) -> str:
        meta = report.meta
        opinions = "；".join(
            f"{period}={value or '数据缺失'}" for period, value in meta.audit_opinions.items()
        )
        cross = report.cross_validation
        cross_text = (
            f"交叉校验 {cross.passed_count} 项通过 / {cross.failed_count} 项不通过"
            if cross is not None and cross.available
            else "交叉校验未启用"
        )
        return f"""# FinSight 事实包（唯一数值事实来源，禁止外推与心算）

## 0. 元信息
- 标的：{meta.stock_code} {meta.stock_name}（市场 {meta.market_prefix}）
- 企业类型：{meta.org_type}；是否金融类：{"是" if meta.is_financial else "否"}
- 最新完整会计年度：**{meta.latest_report_period}**
- 覆盖报告期（时间倒序）：{"、".join(meta.report_periods)}
- 审计意见：{opinions}
- 数据源：{meta.data_source.provider} / {meta.data_source.upstream}；{cross_text}
- **单位声明（务必严格遵守）**：金额=亿元；比率=百分数（已换算，如 91.18%）；同比=百分数；
  周转率=次；周转天数=天；M-Score=分值（阈值 -1.78，越高越可疑）
- 口径提醒：以下所有数字均由 Python 引擎确定性计算，**不得改写、不得反推、不得跨期外推**。"""

    @staticmethod
    def _section_growth(report: FinancialReport) -> str:
        rows = report.growth_profitability.years
        header = (
            "| 报告期 | 营业总收入 | 营收同比 | 归母净利润 | 归母同比 | 扣非归母净利润 "
            "| 扣非同比 | 毛利率 | 净利率 | 归母净利率 |\n"
            "|---|---|---|---|---|---|---|---|---|---|"
        )
        body = "\n".join(
            "| {p} | {rev} | {rev_yoy} | {np} | {np_yoy} | {ded} | {ded_yoy} | {gm} | {nm} | {pnm} |".format(
                p=row.report_period,
                rev=fmt_amount(row.total_operating_revenue),
                rev_yoy=fmt_pct(row.total_operating_revenue_yoy, signed=True),
                np=fmt_amount(row.parent_net_profit),
                np_yoy=fmt_pct(row.parent_net_profit_yoy, signed=True),
                ded=fmt_amount(row.deducted_parent_net_profit),
                ded_yoy=fmt_pct(row.deducted_parent_net_profit_yoy, signed=True),
                gm=fmt_pct(row.gross_margin),
                nm=fmt_pct(row.net_margin),
                pnm=fmt_pct(row.parent_net_margin),
            )
            for row in rows
        )
        cagr = report.growth_profitability.cagr
        observations = "\n".join(
            f"- {item}" for item in report.growth_profitability.trend_observations
        )
        return f"""## 1. 核心业绩（近 {len(rows)} 个完整会计年度，金额单位：亿元）

{header}
{body}

- 区间 CAGR（{cagr.window_start} → {cagr.window_end}，{cagr.periods} 年）：
  营收 {fmt_pct(cagr.revenue_cagr, signed=True)}；归母净利润 {fmt_pct(cagr.parent_net_profit_cagr, signed=True)}；
  扣非归母净利润 {fmt_pct(cagr.deducted_net_profit_cagr, signed=True)}
- 引擎自动趋势观察（确定性文本）：
{observations or "- 无"}"""

    @staticmethod
    def _section_dupont(report: FinancialReport) -> str:
        rows = report.dupont.years
        if not rows:
            return "## 2. 杜邦拆解\n- 数据缺失，无法拆解。"
        header = (
            "| 报告期 | 销售净利率 | 总资产周转率 | 权益乘数 | ROE(杜邦) | ROE 同比变动 "
            "| 净利率变动 | 周转率变动 | 权益乘数变动 | 归母摊薄ROE | 恒等式误差 |\n"
            "|---|---|---|---|---|---|---|---|---|---|---|"
        )
        body = "\n".join(
            "| {p} | {nm} | {to} | {em} | {roe} | {roe_d} | {d1} | {d2} | {d3} | {roe_p} | {err} |".format(
                p=row.report_period,
                nm=fmt_pct(row.net_margin),
                to=fmt_turnover(row.total_asset_turnover),
                em=fmt_multiple(row.equity_multiplier),
                roe=fmt_pct(row.roe_dupont),
                roe_d=fmt_pct_point(row.roe_delta),
                d1=fmt_pct_point(row.net_margin_delta),
                d2=(
                    MISSING if row.asset_turnover_delta is None else f"{row.asset_turnover_delta:+.4f} 次"
                ),
                d3=(
                    MISSING
                    if row.equity_multiplier_delta is None
                    else f"{row.equity_multiplier_delta:+.4f} 倍"
                ),
                roe_p=fmt_pct(row.roe_parent_diluted),
                err=f"{row.identity_check_error:.6f}" if row.identity_check_error is not None else MISSING,
            )
            for row in rows
        )
        driver = report.dupont.dominant_driver or "数据不足"
        note = report.dupont.driver_note or "数据不足，无法归因。"
        return f"""## 2. 杜邦拆解（ROE = 销售净利率 × 总资产周转率 × 权益乘数）

{header}
{body}

- **最新年度 ROE 主导驱动因子：{driver}**
- 连环替代法归因原文（三因子各自贡献多少个百分点 ROE 变动，以此为准）：{note}
- 恒等式校验：ROE(杜邦) 与 ROE(直接法) 的误差应为 0.000000（表中最后一列）。"""

    @staticmethod
    def _section_cashflow(report: FinancialReport) -> str:
        rows = report.cashflow_quality.years
        header = (
            "| 报告期 | 经营现金流净额 | 净利润 | 净现比 | 收现比 | 资本开支 | 自由现金流(FCF) "
            "| FCF/营收 | 应计比率 |\n|---|---|---|---|---|---|---|---|---|"
        )
        body = "\n".join(
            "| {p} | {ocf} | {np} | {ncr} | {ccr} | {capex} | {fcf} | {fm} | {ar} |".format(
                p=row.report_period,
                ocf=fmt_amount(row.net_cash_from_operating),
                np=fmt_amount(row.net_profit),
                ncr=fmt_ratio(row.net_cash_ratio),
                ccr=fmt_ratio(row.cash_collection_ratio),
                capex=fmt_amount(row.capex),
                fcf=fmt_amount(row.free_cash_flow),
                fm=fmt_pct(row.fcf_margin),
                ar=fmt_ratio(row.accrual_ratio),
            )
            for row in rows
        )
        notes = "\n".join(f"- {item}" for item in report.cashflow_quality.notes)
        return f"""## 3. 现金流含金量（金额单位：亿元）

{header}
{body}

- 净现比 / 收现比 / 应计比率均为**无量纲比值**（非百分数），>1 表示现金覆盖利润。
- 口径提示：
{notes or "- 无"}"""

    @staticmethod
    def _section_efficiency(report: FinancialReport) -> str:
        rows = report.efficiency.years
        if not rows:
            return "## 4. 营运效率\n- 不适用（金融类企业无存货与经营性应收应付周转概念）。"
        header = (
            "| 报告期 | 存货周转率 | 存货周转天数 | 应收周转天数 | 应付周转天数 | 现金循环周期 "
            "| 存货/流动资产 |\n|---|---|---|---|---|---|---|"
        )
        body = "\n".join(
            "| {p} | {it} | {id} | {ard} | {apd} | {ccc} | {ic} |".format(
                p=row.report_period,
                it=fmt_turnover(row.inventory_turnover),
                id=fmt_days(row.inventory_days),
                ard=fmt_days(row.receivables_days),
                apd=fmt_days(row.payables_days),
                ccc=fmt_days(row.cash_conversion_cycle),
                ic=fmt_pct(row.inventory_to_current_assets),
            )
            for row in rows
        )
        return f"""## 4. 营运效率

{header}
{body}"""

    @staticmethod
    def _section_raw(report: FinancialReport) -> str:
        parts = ["## 5. 原始科目快照（亿元；仅列有值科目，null 已省略）"]
        for period, metrics in report.raw_metrics.items():
            parts.append(f"### {period}")
            for group_name, items in _RAW_GROUPS:
                cells: list[str] = []
                for attr, label in items:
                    value = getattr(metrics, attr, None)
                    if value is None:
                        continue
                    cells.append(f"{label} {value:.4f}")
                if cells:
                    parts.append(f"- {group_name}：" + "；".join(cells))
        return "\n".join(parts)

    @staticmethod
    def _section_beneish(report: FinancialReport) -> str:
        beneish = report.forensic.beneish
        if not beneish.applicable:
            return (
                "## 6. Beneish M-Score\n"
                f"- 不适用：{beneish.reason}\n"
                f"- 结论：{beneish.risk_text}"
            )
        header = "| 变量 | 中文名 | 取值 | 系数 | 贡献 | 公式 |\n|---|---|---|---|---|---|"
        body = "\n".join(
            "| {code} | {cn} | {v} | {w:.3f} | {c} | {f} |".format(
                code=var.code,
                cn=var.name_cn,
                v=fmt_ratio(var.value),
                w=var.weight,
                c=fmt_ratio(var.contribution),
                f=var.formula.replace("|", "/"),
            )
            for var in beneish.variables
        )
        return f"""## 6. Beneish M-Score（t={beneish.latest_period} vs t-1={beneish.prior_period}）

- 模型：{beneish.model_formula}
- **M-Score = {fmt_score(beneish.m_score)}（阈值 {beneish.threshold}）→ {beneish.risk_text}**

{header}
{body}

- 模型局限：{" ".join(beneish.cautions)}"""

    @staticmethod
    def _section_rules(report: FinancialReport) -> str:
        lines = ["## 7. 本土化排雷规则（6 条硬检验）"]
        for flag in report.forensic.rule_flags:
            lines.append(_render_rule(flag))
        return "\n".join(lines)

    @staticmethod
    def _section_quality(report: FinancialReport) -> str:
        warnings = aggregate_warnings(report.warnings)
        summary = report.summary
        return f"""## 8. 数据质量与口径（已聚合去重）

- 数据完整度：{summary.data_quality}
- 综合风险等级（仅由规则触发数量推导，不构成结论）：{summary.risk_level}
- 缺失/口径提示（去重聚合）：
{chr(10).join(warnings) if warnings else "- 无"}"""

    # -- 锚点 ---------------------------------------------------------------
    @staticmethod
    def _build_anchors(report: FinancialReport) -> list[Anchor]:
        """构造"必须正确"的核心锚点（仅最新年度，避免锚点过多导致校验噪音）。"""
        period = report.meta.latest_report_period
        anchors: list[Anchor] = []

        def add(key: str, label: str, value: Optional[float], kind: AnchorKind) -> None:
            if value is None:
                return
            anchors.append(Anchor(key=key, label=label, raw=float(value), kind=kind, period=period))

        growth = next(
            (row for row in report.growth_profitability.years if row.report_period == period), None
        )
        if growth is not None:
            add("total_operating_revenue", "营业总收入", growth.total_operating_revenue, "amount")
            add("parent_net_profit", "归母净利润", growth.parent_net_profit, "amount")
            add(
                "deducted_parent_net_profit",
                "扣非归母净利润",
                growth.deducted_parent_net_profit,
                "amount",
            )
            add("gross_margin", "毛利率", growth.gross_margin, "percent")
            add("net_margin", "净利率", growth.net_margin, "percent")

        dupont = next((row for row in report.dupont.years if row.report_period == period), None)
        if dupont is not None:
            add("roe_dupont", "ROE(杜邦)", dupont.roe_dupont, "percent")

        cash = next(
            (row for row in report.cashflow_quality.years if row.report_period == period), None
        )
        if cash is not None:
            add("net_cash_ratio", "净现比", cash.net_cash_ratio, "ratio")

        m_score = report.forensic.beneish.m_score
        if m_score is not None:
            add("m_score", "M-Score", m_score, "score")

        return anchors


def _render_evidence(key: str, value: object) -> str:
    """规则证据值 → 带单位的可读字符串。"""
    if value is None:
        return "数据缺失"
    unit = _EVIDENCE_UNITS.get(key)
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return "、".join(f"{k}={_render_evidence(k, v)}" for k, v in value.items() if v is not None) or "无"
    if isinstance(value, (int, float)):
        if unit == "亿元":
            return f"{value:.4f} 亿元"
        if unit == "%":
            return f"{value * 100:.2f}%"
        if unit == "pct":
            return f"{value * 100:+.2f}pct"
        return f"{value:g}"
    return str(value)


def _render_rule(flag: RuleFlag) -> str:
    if not flag.evaluated:
        status = "不适用"
    elif flag.triggered:
        status = {"high": "触发-高", "medium": "触发-中", "info": "触发-提示"}[flag.severity]
    else:
        status = "未触发"

    lines = [f"### [{status}] {flag.name}（{flag.code}）", f"- 结论：{flag.summary}"]
    for item in flag.detail:
        lines.append(f"- 证据：{item}")
    key_evidence = {
        key: value
        for key, value in flag.evidence.items()
        if key in _EVIDENCE_UNITS and value is not None
    }
    if key_evidence:
        lines.append(
            "- 关键数值：" + "；".join(f"{k}={_render_evidence(k, v)}" for k, v in key_evidence.items())
        )
    if flag.triggered and flag.interpretation_hint:
        lines.append(f"- 追查方向：{flag.interpretation_hint}")
    if not flag.evaluated and flag.skip_reason:
        lines.append(f"- 跳过原因：{flag.skip_reason}")
    return "\n".join(lines)


def build_fact_pack(report: FinancialReport, peer_text: str = "") -> FactPack:
    """便捷函数：一步生成事实包。"""
    return FactPackBuilder().build(report, peer_text=peer_text)
