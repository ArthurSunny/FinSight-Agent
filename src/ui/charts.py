"""Phase 4：机构级 Plotly 图表（只读 FinancialReport，绝不重算任何财务指标）。

铁律 1 在 UI 层的落地
--------------------
本模块**只做两件事**：读取 Pydantic 模型里已算好的字段 → 渲染成图。
唯一允许的数值变换是"小数 → 百分数"的**显示换算**（:func:`_pct`，即 ×100 加 %），
与 ``context_formatter.fmt_pct`` 完全同构，不属于财务计算。
任何缺失值（``None``）都跳过该数据点，不补 0（铁律 3）。
"""

from __future__ import annotations

from typing import Any, Optional, Sequence

import plotly.graph_objects as go
from plotly.subplots import make_subplots

from src.tools.peer_comparison import PeerComparison
from src.tools.schemas import FinancialReport
from src.ui.theme import (
    FONT_FAMILY,
    GOLD,
    GRID,
    GREEN,
    MUTED,
    NAVY,
    NAVY_LIGHT,
    PANEL,
    RED,
    SERIES_COLORS,
    SLATE,
)

#: 净现比警戒线（与 Phase 1「纸面富贵」规则阈值一致，仅用于画参考线）
NET_CASH_RATIO_ALERT = 0.7


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------
def _pct(value: Optional[float]) -> Optional[float]:
    """小数比率 → 百分数**显示值**（0.3440 → 34.40）。纯展示换算，非财务计算。"""
    return None if value is None else round(value * 100, 4)


def _layout(fig: go.Figure, title: str, height: int, *, legend: bool = True) -> go.Figure:
    """统一图表外观（字体、留白、网格）。"""
    fig.update_layout(
        title={"text": title, "x": 0.01, "xanchor": "left", "font": {"size": 14, "color": NAVY}},
        height=height,
        margin={"l": 58, "r": 58, "t": 54, "b": 44},
        font={"family": FONT_FAMILY, "size": 11.5, "color": "#33414F"},
        paper_bgcolor="#FFFFFF",
        plot_bgcolor="#FFFFFF",
        hoverlabel={"font": {"family": FONT_FAMILY, "size": 11.5}, "bgcolor": "#FFFFFF"},
        showlegend=legend,
        legend={"orientation": "h", "yanchor": "bottom", "y": 1.02, "x": 0, "font": {"size": 11}},
        bargap=0.28,
        bargroupgap=0.08,
    )
    fig.update_xaxes(showgrid=False, linecolor=GRID, zeroline=False, tickfont={"size": 11})
    fig.update_yaxes(gridcolor=GRID, gridwidth=1, zeroline=False, linecolor=GRID)
    return fig


def empty_figure(
    message: str,
    *,
    height: int = 240,
    hint: str = "",
) -> go.Figure:
    """无数据时的占位图（优雅降级，不抛错）。"""
    fig = go.Figure()
    fig.add_annotation(
        text=f"<b>{message}</b>" + (f"<br><span style='font-size:11px'>{hint}</span>" if hint else ""),
        x=0.5,
        y=0.5,
        xref="paper",
        yref="paper",
        showarrow=False,
        font={"family": FONT_FAMILY, "size": 13, "color": MUTED},
        align="center",
    )
    fig.update_layout(
        height=height,
        paper_bgcolor=PANEL,
        plot_bgcolor=PANEL,
        margin={"l": 10, "r": 10, "t": 10, "b": 10},
        xaxis={"visible": False},
        yaxis={"visible": False},
    )
    return fig


def _ascending(rows: Sequence[Any]) -> list[Any]:
    """引擎按时间倒序输出，图表需要正序。"""
    return list(reversed(rows))


# ---------------------------------------------------------------------------
# 1. 成长与盈利趋势（柱 + 双轴折线）
# ---------------------------------------------------------------------------
def growth_trend_figure(report: FinancialReport, *, height: int = 400) -> go.Figure:
    rows = _ascending(report.growth_profitability.years)
    if not rows:
        return empty_figure("暂无成长性数据")

    periods = [row.report_period for row in rows]
    fig = make_subplots(specs=[[{"secondary_y": True}]])

    bars = (
        ("营业总收入", [row.total_operating_revenue for row in rows], NAVY),
        ("归母净利润", [row.parent_net_profit for row in rows], NAVY_LIGHT),
        ("扣非归母净利润", [row.deducted_parent_net_profit for row in rows], SLATE),
    )
    for name, values, color in bars:
        fig.add_trace(
            go.Bar(
                x=periods,
                y=values,
                name=name,
                marker_color=color,
                marker_line={"width": 0},
                hovertemplate=f"{name}<br>%{{x}}：%{{y:,.2f}} 亿元<extra></extra>",
            ),
            secondary_y=False,
        )

    lines = (
        ("毛利率", [row.gross_margin for row in rows], GOLD, "circle"),
        ("销售净利率", [row.net_margin for row in rows], RED, "diamond"),
    )
    for name, values, color, symbol in lines:
        y = [_pct(value) for value in values]
        if all(value is None for value in y):
            continue
        fig.add_trace(
            go.Scatter(
                x=periods,
                y=y,
                name=name,
                mode="lines+markers",
                line={"color": color, "width": 2.2},
                marker={"size": 8, "symbol": symbol, "color": color},
                connectgaps=False,
                hovertemplate=f"{name}<br>%{{x}}：%{{y:.2f}}%<extra></extra>",
            ),
            secondary_y=True,
        )

    _layout(fig, "近四年度成长与盈利趋势（金额：亿元｜比率：%）", height)
    fig.update_yaxes(title_text="金额（亿元）", secondary_y=False, title_font={"size": 11})
    fig.update_yaxes(
        title_text="比率（%）", secondary_y=True, showgrid=False, title_font={"size": 11}
    )
    return fig


# ---------------------------------------------------------------------------
# 2. 杜邦 ROE 连环替代瀑布图（核心亮点）
# ---------------------------------------------------------------------------
def dupont_waterfall_figure(report: FinancialReport, *, height: int = 380) -> go.Figure:
    years = report.dupont.years
    if len(years) < 2:
        return empty_figure("杜邦瀑布图需要至少两个年度", hint="当前报告仅有一个完整年度")

    current, previous = years[0], years[1]
    roe_from = _pct(previous.roe_dupont)
    roe_to = _pct(current.roe_dupont)
    effects = (
        ("销售净利率", _pct(current.net_margin_effect)),
        ("总资产周转率", _pct(current.asset_turnover_effect)),
        ("权益乘数", _pct(current.equity_multiplier_effect)),
    )
    if roe_from is None or roe_to is None or any(value is None for _, value in effects):
        return empty_figure(
            "三因子贡献数据缺失，无法绘制瀑布图",
            hint="请确认报告中该年度存在可比的上一年度数据",
        )

    labels = [f"{previous.report_period}\nROE"] + [name for name, _ in effects] + [
        f"{current.report_period}\nROE"
    ]
    values = [roe_from] + [value for _, value in effects] + [roe_to]
    measures = ["absolute", "relative", "relative", "relative", "total"]

    fig = go.Figure(
        go.Waterfall(
            name="ROE 归因",
            orientation="v",
            measure=measures,
            x=labels,
            y=values,
            text=[f"{value:+.2f}pct" if measure == "relative" else f"{value:.2f}%"
                  for value, measure in zip(values, measures)],
            textposition="outside",
            textfont={"size": 11.5},
            connector={"line": {"color": GRID, "width": 1}},
            increasing={"marker": {"color": GREEN}},
            decreasing={"marker": {"color": RED}},
            totals={"marker": {"color": NAVY}},
            hovertemplate="%{x}<br>影响：%{y:+.2f}pct<extra></extra>",
        )
    )
    driver = report.dupont.dominant_driver or "—"
    _layout(
        fig,
        f"杜邦 ROE 连环替代归因（{current.report_period} vs {previous.report_period}）"
        f"　主导因子：{driver}",
        height,
        legend=False,
    )
    fig.update_yaxes(title_text="ROE（%）", title_font={"size": 11})
    fig.add_hline(y=roe_from, line={"color": GRID, "dash": "dot", "width": 1})
    return fig


# ---------------------------------------------------------------------------
# 3. 现金流含金量
# ---------------------------------------------------------------------------
def cashflow_figure(report: FinancialReport, *, height: int = 400) -> go.Figure:
    rows = _ascending(report.cashflow_quality.years)
    if not rows:
        return empty_figure("暂无现金流数据")

    periods = [row.report_period for row in rows]
    fig = make_subplots(specs=[[{"secondary_y": True}]])

    bars = (
        ("经营活动现金流净额", [row.net_cash_from_operating for row in rows], NAVY),
        ("净利润", [row.net_profit for row in rows], SLATE),
        ("自由现金流 FCF", [row.free_cash_flow for row in rows], GOLD),
    )
    for name, values, color in bars:
        fig.add_trace(
            go.Bar(
                x=periods,
                y=values,
                name=name,
                marker_color=color,
                hovertemplate=f"{name}<br>%{{x}}：%{{y:,.2f}} 亿元<extra></extra>",
            ),
            secondary_y=False,
        )

    ratios = [row.net_cash_ratio for row in rows]
    if any(value is not None for value in ratios):
        fig.add_trace(
            go.Scatter(
                x=periods,
                y=ratios,
                name="净现比",
                mode="lines+markers",
                line={"color": RED, "width": 2.2},
                marker={"size": 9, "symbol": "diamond"},
                connectgaps=False,
                hovertemplate="净现比<br>%{x}：%{y:.4f}<extra></extra>",
            ),
            secondary_y=True,
        )
        fig.add_shape(
            type="line",
            xref="paper",
            x0=0,
            x1=1,
            yref="y2",
            y0=NET_CASH_RATIO_ALERT,
            y1=NET_CASH_RATIO_ALERT,
            line={"color": RED, "dash": "dash", "width": 1.2},
        )
        fig.add_annotation(
            xref="paper",
            x=1,
            yref="y2",
            y=NET_CASH_RATIO_ALERT,
            text=f"净现比警戒线 {NET_CASH_RATIO_ALERT}",
            showarrow=False,
            xanchor="right",
            yanchor="bottom",
            font={"size": 10, "color": RED},
        )

    _layout(fig, "现金流含金量透视（金额：亿元｜净现比：倍）", height)
    fig.update_yaxes(title_text="金额（亿元）", secondary_y=False, title_font={"size": 11})
    fig.update_yaxes(title_text="净现比（倍）", secondary_y=True, showgrid=False,
                     title_font={"size": 11})
    return fig


# ---------------------------------------------------------------------------
# 4. Beneish M-Score 八变量
# ---------------------------------------------------------------------------
def _beneish_hover(item: Any) -> str:
    """单个 Beneish 变量的悬浮说明（贡献缺失时退化为基础信息）。"""
    head = f"{item.name_cn}（{item.code}）"
    if item.contribution is None or item.value is None:
        return f"{head}<br>数据缺失"
    return (
        f"{head}<br>"
        f"取值：{item.value:.4f}<br>"
        f"系数：{item.weight:+.3f}<br>"
        f"贡献：{item.contribution:+.4f}"
    )


def beneish_figure(report: FinancialReport, *, height: int = 400) -> go.Figure:
    analysis = report.forensic.beneish
    if not analysis.applicable or not analysis.variables:
        return empty_figure(
            "Beneish M-Score 不适用",
            hint=analysis.reason or "金融类企业不适用该模型",
        )

    ordered = sorted(
        analysis.variables,
        key=lambda item: (item.contribution if item.contribution is not None else 0.0),
    )
    codes = [f"{item.code}" for item in ordered]
    values = [item.value for item in ordered]
    colors = [RED if (item.contribution or 0) > 0 else GREEN for item in ordered]
    hover = [_beneish_hover(item) for item in ordered]

    fig = go.Figure(
        go.Bar(
            x=values,
            y=codes,
            orientation="h",
            marker={"color": colors, "line": {"width": 0}},
            text=[
                f"{value:.3f}" if value is not None else "缺失" for value in values
            ],
            textposition="outside",
            textfont={"size": 10.5},
            hovertext=hover,
            hoverinfo="text",
            name="变量取值",
        )
    )
    fig.add_shape(
        type="line",
        xref="x",
        x0=1.0,
        x1=1.0,
        yref="paper",
        y0=0,
        y1=1,
        line={"color": GOLD, "dash": "dash", "width": 1.4},
    )
    fig.add_annotation(
        x=1.0,
        y=1.01,
        yref="paper",
        text="基准线 1.0",
        showarrow=False,
        font={"size": 10, "color": GOLD},
        xanchor="left",
    )
    m_score = analysis.m_score
    verdict = "触发嫌疑区间" if analysis.is_high_risk else "未触发嫌疑区间"
    _layout(
        fig,
        f"Beneish M-Score 八变量拆解　M = {m_score if m_score is not None else '缺失'}"
        f"（阈值 {analysis.threshold}）→ {verdict}",
        height,
        legend=False,
    )
    fig.update_xaxes(title_text="指标取值（红色=推高风险，绿色=降低风险；TATA 基准为 0）",
                     title_font={"size": 10.5})
    fig.update_yaxes(showgrid=False)
    return fig


def beneish_radar_figure(report: FinancialReport, *, height: int = 400) -> go.Figure:
    """以"对 M-Score 的贡献"为半径的雷达图（量纲统一，正值为推高风险）。"""
    analysis = report.forensic.beneish
    if not analysis.applicable or not analysis.variables:
        return empty_figure("Beneish M-Score 不适用", hint=analysis.reason or "")

    labels = [item.code for item in analysis.variables]
    contributions = [item.contribution if item.contribution is not None else 0.0
                     for item in analysis.variables]
    labels_closed = labels + labels[:1]
    values_closed = contributions + contributions[:1]

    fig = go.Figure(
        go.Scatterpolar(
            r=values_closed,
            theta=labels_closed,
            fill="toself",
            line={"color": NAVY, "width": 2},
            fillcolor="rgba(31,58,95,0.16)",
            hovertemplate="%{theta}：贡献 %{r:+.4f}<extra></extra>",
            name="对 M-Score 的贡献",
        )
    )
    fig.update_layout(
        title={
            "text": "M-Score 八变量贡献雷达（外圈=推高风险，内圈=降低风险）",
            "x": 0.01,
            "xanchor": "left",
            "font": {"size": 14, "color": NAVY},
        },
        height=height,
        margin={"l": 60, "r": 60, "t": 70, "b": 40},
        font={"family": FONT_FAMILY, "size": 11.5},
        polar={
            "bgcolor": "#FCFDFE",
            "radialaxis": {
                "gridcolor": GRID,
                "linecolor": SLATE,
                "showline": True,
                "tickfont": {"size": 10},
            },
            "angularaxis": {"gridcolor": GRID, "tickfont": {"size": 11}},
        },
        showlegend=False,
        paper_bgcolor="#FFFFFF",
    )
    return fig


def mscore_gauge_figure(report: FinancialReport, *, height: int = 240) -> go.Figure:
    """M-Score 仪表盘（-5 ~ 0，阈值 -1.78）。"""
    analysis = report.forensic.beneish
    if not analysis.applicable or analysis.m_score is None:
        return empty_figure("M-Score 不适用或数据缺失", hint=analysis.reason or "", height=height)

    score = analysis.m_score
    color = RED if analysis.is_high_risk else GREEN
    fig = go.Figure(
        go.Indicator(
            mode="gauge+number",
            value=score,
            number={"font": {"size": 26, "color": color}, "valueformat": ".4f"},
            title={"text": f"Beneish M-Score（阈值 {analysis.threshold}）",
                   "font": {"size": 12, "color": SLATE}},
            gauge={
                "axis": {"range": [-5, 0], "tickwidth": 1, "tickcolor": MUTED,
                         "tickfont": {"size": 10}},
                "bar": {"color": color, "thickness": 0.32},
                "bgcolor": "#F2F5F8",
                "borderwidth": 0,
                "steps": [
                    {"range": [-5, analysis.threshold], "color": "rgba(30,111,92,0.16)"},
                    {"range": [analysis.threshold, 0], "color": "rgba(169,50,38,0.16)"},
                ],
                "threshold": {
                    "line": {"color": GOLD, "width": 3},
                    "thickness": 0.8,
                    "value": analysis.threshold,
                },
            },
        )
    )
    fig.update_layout(
        height=height,
        margin={"l": 30, "r": 30, "t": 46, "b": 16},
        font={"family": FONT_FAMILY},
        paper_bgcolor="#FFFFFF",
    )
    return fig


# ---------------------------------------------------------------------------
# 5. 同业横向对标
# ---------------------------------------------------------------------------
_PEER_METRICS: tuple[tuple[str, str, bool], ...] = (
    # (字段名, 展示名, 是否百分比)
    ("roe_dupont", "ROE（杜邦）", True),
    ("gross_margin", "毛利率", True),
    ("net_margin", "净利率", True),
    ("interest_debt_ratio", "有息负债率", True),
)


def peer_comparison_figure(peer: Optional[PeerComparison], *, height: int = 420) -> go.Figure:
    if peer is None or not peer.available:
        return empty_figure(
            "本次未提供可比同业数据",
            hint="可在侧边栏填写同业代码后重新运行（如 000858,000568）",
        )

    rows = peer.rows
    fig = go.Figure()
    for index, row in enumerate(rows):
        label = f"{row.stock_code} {row.stock_name}"
        if row.data_caution:
            label += "（待核实）"
        values = [
            _pct(getattr(row, field)) if is_pct else getattr(row, field)
            for field, _, is_pct in _PEER_METRICS
        ]
        color = SERIES_COLORS[index % len(SERIES_COLORS)]
        fig.add_trace(
            go.Bar(
                name=("★ " if row.is_target else "") + label,
                x=[display for _, display, _ in _PEER_METRICS],
                y=values,
                marker={
                    "color": color,
                    "line": {"width": 2 if row.is_target else 0, "color": NAVY},
                },
                hovertemplate=f"{label}<br>%{{x}}：%{{y:.2f}}%<extra></extra>",
            )
        )

    cautions = [row for row in rows if row.data_caution]
    title = "同业横向财务对标（%）"
    if cautions:
        title += "　⚠ " + "、".join(f"{row.stock_code}" for row in cautions) + " 数据待核实"
    _layout(fig, title, height)
    fig.update_layout(barmode="group")
    fig.update_yaxes(title_text="百分比（%）", title_font={"size": 11})

    if cautions:
        note = "；".join(f"{row.stock_code} {row.stock_name}：{row.data_caution}" for row in cautions)
        fig.add_annotation(
            text=f"数据可信度提示：{note}",
            xref="paper",
            yref="paper",
            x=0,
            y=-0.24,
            showarrow=False,
            align="left",
            font={"size": 10, "color": RED},
        )
        fig.update_layout(margin={"b": 96})
    return fig


def peer_table_rows(peer: Optional[PeerComparison]) -> list[dict[str, Any]]:
    """同业对标的结构化行（供 st.dataframe 渲染，UI 不做任何计算）。"""
    if peer is None or not peer.rows:
        return []
    rows: list[dict[str, Any]] = []
    for row in peer.rows:
        rows.append(
            {
                "公司": f"{row.stock_code} {row.stock_name}",
                "报告期": row.latest_report_period,
                "营业总收入(亿元)": row.revenue,
                "营收同比(%)": _pct(row.revenue_yoy),
                "毛利率(%)": _pct(row.gross_margin),
                "净利率(%)": _pct(row.net_margin),
                "ROE(%)": _pct(row.roe_dupont),
                "净现比": row.net_cash_ratio,
                "有息负债率(%)": _pct(row.interest_debt_ratio),
                "M-Score": row.m_score,
                "风险等级": row.risk_level,
                "数据提示": row.data_caution or "",
            }
        )
    return rows
