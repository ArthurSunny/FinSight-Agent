"""与外部财务指标源的独立交叉校验（自证引擎计算正确性）。

为什么需要它
------------
AGENTS.md 铁律 2 要求"数据可追溯、防幻觉"。光有口径说明不够 —— 本模块用**完全独立的
第二个数据源**（新浪财经财务指标，经 akshare 封装）复算同一批指标，逐项比对偏差：

* 通过 → 说明引擎口径与该数据源一致，数值可信；
* 不通过 → 立即暴露口径错误（例如把"归母净利润"当成"净利润"、把百分数当小数）。

实测口径坑（务必注意）
----------------------
`stock_financial_analysis_indicator` 返回的 ``经营现金净流量与净利润的比率(%)`` 列名标注了
``(%)``，但实测数值是**倍数**而非百分数（例如 0.7212 对应净现比 72.12%）。
因此本模块对该列使用 ``scale=1.0`` 而不是 100，并在此显式记录，避免后来者误改。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Optional, Sequence

from src.tools.diagnostics import WarningCollector, finite_or_none, round_ratio
from src.tools.schemas import CrossValidation, ValidationCheck

#: 交叉校验所用的外部接口
INTERFACE: str = "stock_financial_analysis_indicator"
SOURCE_LABEL: str = (
    "akshare.stock_financial_analysis_indicator（新浪财经财务指标，季度口径，此处取 12-31 年度行）"
)


# ---------------------------------------------------------------------------
# 取值器：从引擎已算好的结构化结果里取对应指标
# ---------------------------------------------------------------------------
def _row_of(rows: Sequence[Any], year: int) -> Optional[Any]:
    for row in rows:
        if getattr(row, "report_year", None) == year:
            return row
    return None


def _roe_parent(engine: dict[str, Any], year: int) -> Optional[float]:
    row = _row_of(engine["dupont"].years, year)
    return None if row is None else row.roe_parent_diluted


def _net_margin(engine: dict[str, Any], year: int) -> Optional[float]:
    row = _row_of(engine["growth"].years, year)
    return None if row is None else row.net_margin


def _asset_turnover(engine: dict[str, Any], year: int) -> Optional[float]:
    row = _row_of(engine["dupont"].years, year)
    return None if row is None else row.total_asset_turnover


def _inventory_turnover(engine: dict[str, Any], year: int) -> Optional[float]:
    row = _row_of(engine["efficiency"].years, year)
    return None if row is None else row.inventory_turnover


def _net_cash_ratio(engine: dict[str, Any], year: int) -> Optional[float]:
    row = _row_of(engine["cashflow"].years, year)
    return None if row is None else row.net_cash_ratio


def _debt_ratio(engine: dict[str, Any], year: int) -> Optional[float]:
    period = f"{year}年报"
    metrics = engine["raw_metrics"].get(period)
    if metrics is None or not metrics.total_assets or metrics.total_liabilities is None:
        return None
    return metrics.total_liabilities / metrics.total_assets


@dataclass(frozen=True)
class _CheckSpec:
    """单项校验定义。

    Attributes
    ----------
    metric:
        展示用指标名。
    source_column:
        外部数据源列名。
    scale:
        引擎值 × scale 后与外部值比较（100 表示外部为百分数）。
    tolerance:
        允许的绝对偏差。
    extractor:
        从引擎结果取值的函数。
    note:
        口径说明。
    """

    metric: str
    source_column: str
    scale: float
    tolerance: float
    extractor: Callable[[dict[str, Any], int], Optional[float]]
    note: str = ""


CHECK_SPECS: tuple[_CheckSpec, ...] = (
    _CheckSpec(
        metric="归母摊薄ROE",
        source_column="净资产收益率(%)",
        scale=100.0,
        tolerance=0.5,
        extractor=_roe_parent,
        note="外部源口径 = 归母净利润 / 期末归母所有者权益（摊薄）",
    ),
    _CheckSpec(
        metric="销售净利率",
        source_column="销售净利率(%)",
        scale=100.0,
        tolerance=0.2,
        extractor=_net_margin,
        note="分母为营业收入（非营业总收入）",
    ),
    _CheckSpec(
        metric="总资产周转率",
        source_column="总资产周转率(次)",
        scale=1.0,
        tolerance=0.02,
        extractor=_asset_turnover,
        note="营业总收入 / 平均总资产",
    ),
    _CheckSpec(
        metric="存货周转率",
        source_column="存货周转率(次)",
        scale=1.0,
        tolerance=0.02,
        extractor=_inventory_turnover,
        note="营业成本 / 平均存货",
    ),
    _CheckSpec(
        metric="资产负债率",
        source_column="资产负债率(%)",
        scale=100.0,
        tolerance=0.3,
        extractor=_debt_ratio,
        note="负债合计 / 资产总计（期末）",
    ),
    _CheckSpec(
        metric="净现比",
        source_column="经营现金净流量与净利润的比率(%)",
        scale=1.0,
        tolerance=0.01,
        extractor=_net_cash_ratio,
        note="该列名标注(%)，实测为倍数口径，故 scale=1.0",
    ),
)


def run_cross_validation(
    *,
    stock_code: str,
    display_years: Sequence[int],
    report: dict[str, Any],
    collector: WarningCollector,
) -> CrossValidation:
    """执行交叉校验；任何异常都降级为 ``available=False``，绝不影响主流程。"""
    try:
        import akshare as ak

        frame = ak.stock_financial_analysis_indicator(
            symbol=stock_code, start_year=str(min(display_years))
        )
    except Exception as exc:  # noqa: BLE001 - 外部接口失败不应阻断报告生成
        collector.add(
            "cross_validation_failed",
            f"交叉校验接口 {INTERFACE} 调用失败（{type(exc).__name__}: {exc}），"
            f"本次报告未包含交叉校验结果。",
            level="info",
        )
        return CrossValidation(
            available=False,
            source=f"{INTERFACE} — {SOURCE_LABEL}",
            reason=f"{type(exc).__name__}: {exc}",
            note="交叉校验为可选项，失败不影响主报告；建议在网络稳定时以 include_cross_validation=True 重跑。",
        )

    if frame is None or frame.empty or "日期" not in frame.columns:
        collector.add(
            "cross_validation_empty",
            f"交叉校验接口 {INTERFACE} 返回空数据，本次报告未包含交叉校验结果。",
            level="info",
        )
        return CrossValidation(
            available=False,
            source=f"{INTERFACE} — {SOURCE_LABEL}",
            reason="接口返回空数据或无'日期'列",
            note="",
        )

    annual_only = frame[frame["日期"].astype(str).str.endswith("12-31")]
    by_year: dict[int, Any] = {}
    for _, row in annual_only.iterrows():
        try:
            by_year[int(str(row["日期"])[:4])] = row
        except (TypeError, ValueError):
            continue

    checks: list[ValidationCheck] = []
    for year in display_years:
        source_row = by_year.get(year)
        period = f"{year}年报"
        for spec in CHECK_SPECS:
            engine_value = finite_or_none(spec.extractor(report, year))
            source_value = (
                None
                if source_row is None
                else finite_or_none(source_row.get(spec.source_column))
            )
            if engine_value is None or source_value is None:
                checks.append(
                    ValidationCheck(
                        metric=spec.metric,
                        report_period=period,
                        engine_value=(
                            None if engine_value is None else round_ratio(engine_value * spec.scale)
                        ),
                        source_value=round_ratio(source_value),
                        diff=None,
                        tolerance=spec.tolerance,
                        passed=None,
                    )
                )
                continue
            engine_scaled = engine_value * spec.scale
            diff = engine_scaled - source_value
            checks.append(
                ValidationCheck(
                    metric=spec.metric,
                    report_period=period,
                    engine_value=round_ratio(engine_scaled),
                    source_value=round_ratio(source_value),
                    diff=round_ratio(diff),
                    tolerance=spec.tolerance,
                    passed=bool(abs(diff) <= spec.tolerance),
                )
            )

    passed = sum(1 for item in checks if item.passed is True)
    failed = sum(1 for item in checks if item.passed is False)
    skipped = sum(1 for item in checks if item.passed is None)

    if failed:
        collector.add(
            "cross_validation_mismatch",
            f"交叉校验有 {failed} 项未通过（共 {len(checks)} 项），请复核引擎口径或数据源变更。",
            level="warning",
            field="cross_validation",
        )

    return CrossValidation(
        available=True,
        source=f"{INTERFACE} — {SOURCE_LABEL}",
        reason=None,
        checks=checks,
        passed_count=passed,
        failed_count=failed,
        skipped_count=skipped,
        note=(
            "交叉校验用于自证引擎口径：偏差在容差内说明两套独立实现一致；"
            "跳过项表示任一侧缺失（如金融类无存货周转率）。"
        ),
    )
