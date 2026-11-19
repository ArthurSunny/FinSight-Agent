"""FinSight-Agent Phase 1 · 财务引擎实盘闭环验证脚本。

运行方式（无需 pytest，直接跑）：:

    python tests/test_financial_engine.py
    python tests/test_financial_engine.py --code 300750
    python tests/test_financial_engine.py --no-json      # 不打印完整 JSON

验证内容（对应 AGENTS.md 铁律 4 —— "跑通 + 数值合理"才算完成）
-------------------------------------------------------------
1. 主流程：贵州茅台 600519 全链路计算，无异常；
2. 数值合理性：毛利率区间、ROE 区间、杜邦恒等式误差 < 1e-6；
3. 回归基准：2023/2024 年营业总收入与实测值一致（防口径漂移）；
4. 内部一致性：净现比、毛利率可由 raw_metrics 复算得到相同结果；
5. JSON 严格合法：``json.dumps(..., allow_nan=False)`` 不报错，且不含 NaN/Inf；
6. 溯源完整：每个报告期都有 raw_metrics，每条警告都有说明；
7. 交叉校验：与新浪财务指标逐项比对，偏差在容差内；
8. 降级路径：金融类（招商银行 600036）与制造业（宁德时代 300750）不崩溃且提示"不适用"；
9. Phase 1.1 会计口径精修：受限货币资金勾稽、财务公司列报差异识别、
   应收账款重要性门槛、非经常性损益正负方向与保壳扭亏识别（含脱机合成样本 6 类情形）。

任何一项失败，脚本以非 0 退出码结束。
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import sys
import time
import traceback
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

# ---- 让脚本可以 `python tests/test_financial_engine.py` 直接运行 ----
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# Windows 控制台默认 GBK，强制切 UTF-8 以免中文输出报 UnicodeEncodeError
for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (ValueError, OSError):
            pass

from src.config import REPORT_OUTPUT_DIR, ensure_directories  # noqa: E402
from src.tools.diagnostics import WarningCollector  # noqa: E402
from src.tools.financial_data import DataUnavailableError, StatementBundle  # noqa: E402
from src.tools.financial_engine import (  # noqa: E402
    FinancialEngine,
    _StatementView,
    report_to_json,
    save_report,
)
from src.tools.schemas import FinancialReport  # noqa: E402

import pandas as pd  # noqa: E402

TRANSCRIPT: list[str] = []


# ---------------------------------------------------------------------------
# 输出与断言工具
# ---------------------------------------------------------------------------
def emit(text: str = "") -> None:
    """同时打印到终端并记录到文本副本。"""
    print(text)
    TRANSCRIPT.append(text)


def banner(title: str, char: str = "=") -> None:
    emit()
    emit(char * 96)
    emit(title)
    emit(char * 96)


def _amt(value: Optional[float]) -> str:
    return f"{'-':>12}" if value is None else f"{value:>12.4f}"


def _pct(value: Optional[float]) -> str:
    return f"{'-':>9}" if value is None else f"{value * 100:>8.2f}%"


def _num(value: Optional[float], digits: int = 4) -> str:
    return f"{'-':>9}" if value is None else f"{value:>9.{digits}f}"


class Checker:
    """极简断言收集器：记录每条断言的通过/失败，最后统一汇报。"""

    def __init__(self) -> None:
        self.passed = 0
        self.failed: list[str] = []
        self.skipped: list[str] = []

    def check(self, condition: bool, label: str, detail: str = "") -> bool:
        if condition:
            self.passed += 1
            emit(f"  [OK]   {label}" + (f"  ({detail})" if detail else ""))
            return True
        self.failed.append(label + (f"  ({detail})" if detail else ""))
        emit(f"  [FAIL] {label}" + (f"  ({detail})" if detail else ""))
        return False

    def skip(self, label: str, reason: str) -> None:
        self.skipped.append(f"{label}: {reason}")
        emit(f"  [SKIP] {label}  ({reason})")

    def summary(self) -> int:
        emit()
        emit(f"断言结果：通过 {self.passed} 项，失败 {len(self.failed)} 项，跳过 {len(self.skipped)} 项。")
        for item in self.failed:
            emit(f"  [FAIL] {item}")
        for item in self.skipped:
            emit(f"  [SKIP] {item}")
        return 0 if not self.failed else 1


def iter_floats(obj: Any, path: str = "$") -> Iterable[tuple[str, float]]:
    """递归遍历 JSON 结构里的所有浮点数（用于检查 NaN/Inf 污染）。"""
    if isinstance(obj, dict):
        for key, value in obj.items():
            yield from iter_floats(value, f"{path}.{key}")
    elif isinstance(obj, list):
        for index, value in enumerate(obj):
            yield from iter_floats(value, f"{path}[{index}]")
    elif isinstance(obj, float):
        yield path, obj


# ---------------------------------------------------------------------------
# 报告渲染
# ---------------------------------------------------------------------------
def render_meta(report: FinancialReport) -> None:
    meta = report.meta
    banner("[0] 报告元信息（可追溯性）")
    emit(f"  标的            : {meta.stock_code} {meta.stock_name}（市场前缀 {meta.market_prefix}）")
    emit(f"  企业类型        : {meta.org_type}  金融类={meta.is_financial}")
    emit(f"  金融类判定依据  : {meta.financial_reason}")
    emit(f"  最新完整年度    : {meta.latest_report_period}")
    emit(f"  覆盖报告期      : {', '.join(meta.report_periods)}（{meta.lookback_years} 个完整年度）")
    emit(f"  审计意见        : {meta.audit_opinions}")
    emit(f"  金额单位        : {meta.amount_unit} / 比率单位: {meta.ratio_unit}")
    emit(f"  引擎版本        : {meta.engine_version}   生成时间: {meta.generated_at}")
    emit(f"  数据截止日      : {meta.data_source.as_of_date}")
    for key, interface in meta.data_source.interfaces.items():
        hit = meta.data_source.cache_hit.get(key)
        fetched = meta.data_source.fetched_at.get(key, "")
        emit(f"    - {key:<16} {interface:<38} 缓存命中={hit} 抓取于 {fetched}")


def render_growth(report: FinancialReport) -> None:
    block = report.growth_profitability
    banner("[1] 核心成长与盈利趋势（金额：亿元；比率：小数）")
    header = (
        f"  {'报告期':<10}{'营业总收入':>14}{'YoY':>10}{'归母净利润':>14}{'YoY':>10}"
        f"{'扣非归母':>14}{'YoY':>10}{'毛利率':>10}{'净利率':>10}"
    )
    emit(header)
    emit("  " + "-" * (len(header) - 2))
    for row in block.years:
        emit(
            f"  {row.report_period:<10}{_amt(row.total_operating_revenue)}"
            f"{_pct(row.total_operating_revenue_yoy)}"
            f"{_amt(row.parent_net_profit)}{_pct(row.parent_net_profit_yoy)}"
            f"{_amt(row.deducted_parent_net_profit)}{_pct(row.deducted_parent_net_profit_yoy)}"
            f"{_pct(row.gross_margin)}{_pct(row.net_margin)}"
        )
    cagr = block.cagr
    emit(
        f"  区间 CAGR（{cagr.window_start} → {cagr.window_end}，{cagr.periods} 年）："
        f"营收 {_pct(cagr.revenue_cagr).strip()} / 归母 {_pct(cagr.parent_net_profit_cagr).strip()} "
        f"/ 扣非 {_pct(cagr.deducted_net_profit_cagr).strip()}"
    )
    emit("  自动趋势观察：")
    for item in block.trend_observations:
        emit(f"    · {item}")


def render_dupont(report: FinancialReport) -> None:
    block = report.dupont
    banner("[2] 杜邦分析体系（ROE = 净利率 × 总资产周转率 × 权益乘数）")
    emit(f"  公式：{block.formula}")
    emit()
    header = (
        f"  {'报告期':<10}{'销售净利率':>12}{'总资产周转率':>14}{'权益乘数':>10}"
        f"{'ROE(杜邦)':>12}{'ROE(直接)':>12}{'恒等式误差':>13}{'摊薄ROE':>10}"
    )
    emit(header)
    emit("  " + "-" * (len(header) - 2))
    for row in block.years:
        emit(
            f"  {row.report_period:<10}{_pct(row.net_margin)}"
            f"{_num(row.total_asset_turnover, 4):>14}{_num(row.equity_multiplier, 4):>10}"
            f"{_pct(row.roe_dupont)}{_pct(row.roe_direct)}"
            f"{_num(row.identity_check_error, 10):>13}{_pct(row.roe_parent_diluted)}"
        )
    emit()
    emit("  同比变动（连环替代法归因）：")
    for row in block.years:
        if row.roe_delta is None:
            continue
        emit(
            f"    {row.report_period}：ROE {row.roe_delta * 100:+.2f}pct"
            f"（净利率 {row.net_margin_delta * 100:+.2f}pct / "
            f"周转率 {row.asset_turnover_delta:+.4f} 次 / "
            f"权益乘数 {row.equity_multiplier_delta:+.4f}）"
        )
    if block.driver_note:
        emit()
        emit(f"  主导驱动：{block.dominant_driver}")
        emit(f"  {block.driver_note}")


def render_cashflow(report: FinancialReport) -> None:
    block = report.cashflow_quality
    banner("[3] 盈余质量与现金流检验（金额：亿元）")
    header = (
        f"  {'报告期':<10}{'经营现金流':>13}{'净利润':>12}{'净现比':>10}"
        f"{'收现比':>10}{'资本开支':>12}{'FCF':>12}{'FCF/营收':>11}{'应计比率':>11}"
    )
    emit(header)
    emit("  " + "-" * (len(header) - 2))
    for row in block.years:
        emit(
            f"  {row.report_period:<10}{_amt(row.net_cash_from_operating)}"
            f"{_amt(row.net_profit)}{_num(row.net_cash_ratio, 4):>10}"
            f"{_num(row.cash_collection_ratio, 4):>10}{_amt(row.capex)}"
            f"{_amt(row.free_cash_flow)}{_pct(row.fcf_margin)}{_num(row.accrual_ratio, 4):>11}"
        )
    emit()
    emit("  口径提示：")
    for note in block.notes:
        emit(f"    · {note}")


def render_efficiency(report: FinancialReport) -> None:
    block = report.efficiency
    if not block.years:
        banner("[3b] 营运效率（不适用）")
        emit("  金融类企业无存货/经营性应收应付，本维度为空。")
        return
    banner("[3b] 辅助：营运效率")
    header = (
        f"  {'报告期':<10}{'存货周转率':>12}{'存货天数':>10}{'应收天数':>10}"
        f"{'应付天数':>10}{'现金循环周期':>14}{'存货/流动资产':>16}"
    )
    emit(header)
    emit("  " + "-" * (len(header) - 2))
    for row in block.years:
        emit(
            f"  {row.report_period:<10}{_num(row.inventory_turnover, 4):>12}"
            f"{_num(row.inventory_days, 0):>10}{_num(row.receivables_days, 1):>10}"
            f"{_num(row.payables_days, 1):>10}{_num(row.cash_conversion_cycle, 0):>14}"
            f"{_pct(row.inventory_to_current_assets):>16}"
        )


def render_forensic(report: FinancialReport) -> None:
    forensic = report.forensic
    beneish = forensic.beneish
    banner("[4] 会计排雷 · Beneish M-Score（8 变量）")
    emit(f"  模型：{beneish.model_formula}")
    emit(f"  对比期间：{beneish.prior_period} → {beneish.latest_period}   适用：{beneish.applicable}")
    if not beneish.applicable:
        emit(f"  不适用原因：{beneish.reason}")
    else:
        emit()
        header = f"  {'变量':<7}{'中文名':<16}{'取值':>10}{'系数':>8}{'贡献':>10}  说明"
        emit(header)
        emit("  " + "-" * (len(header) - 2))
        for var in beneish.variables:
            emit(
                f"  {var.code:<7}{var.name_cn:<16}{_num(var.value, 4):>10}"
                f"{var.weight:>8.3f}{_num(var.contribution, 4):>10}  {var.formula}"
            )
        emit()
        emit(f"  M-Score = {beneish.m_score}（阈值 {beneish.threshold}）→ {beneish.risk_text}")
    emit()
    emit("  模型局限性：")
    for caution in beneish.cautions:
        emit(f"    · {caution}")

    banner("[5] 会计排雷 · 本土化规则硬检验")
    for flag in forensic.rule_flags:
        if not flag.evaluated:
            marker = "[SKIP]"
        elif flag.triggered:
            marker = "[!!高]" if flag.severity == "high" else "[! 中]"
        else:
            marker = "[OK]  "
        emit(f"  {marker} {flag.name}（{flag.code}）")
        emit(f"         {flag.summary}")
        for line in flag.detail:
            emit(f"         - {line}")
        if flag.triggered and flag.interpretation_hint:
            emit(f"         → 追查方向：{flag.interpretation_hint}")
        if not flag.evaluated and flag.skip_reason:
            emit(f"         → 跳过原因：{flag.skip_reason}")
        emit()
    emit(f"  触发 {forensic.triggered_count} 条（高严重度 {forensic.triggered_high_count} 条），"
         f"综合风险等级 = {forensic.risk_level}")


def render_cross_validation(report: FinancialReport, checker: Checker) -> None:
    banner("[6] 交叉校验（引擎 vs 外部财务指标源）")
    block = report.cross_validation
    if block is None:
        emit("  未启用（include_cross_validation=False）。")
        checker.skip("交叉校验", "本次运行未启用")
        return
    if not block.available:
        emit(f"  未能执行：{block.reason}")
        checker.skip("交叉校验", "外部接口不可用")
        return
    emit(f"  数据源：{block.source}")
    header = (
        f"  {'指标':<24}{'报告期':<10}{'引擎':>12}{'外部源':>12}{'偏差':>12}{'容差':>8}  结果"
    )
    emit(header)
    emit("  " + "-" * (len(header) - 2))
    for item in block.checks:
        verdict = "通过" if item.passed else ("跳过" if item.passed is None else "不通过")
        emit(
            f"  {item.metric:<24}{item.report_period:<10}"
            f"{_num(item.engine_value, 4):>12}{_num(item.source_value, 4):>12}"
            f"{_num(item.diff, 4):>12}{item.tolerance:>8.2f}  {verdict}"
        )
    emit()
    emit(f"  通过 {block.passed_count} 项 / 不通过 {block.failed_count} 项 / 跳过 {block.skipped_count} 项")


def render_warnings(report: FinancialReport) -> None:
    banner("[7] 数据质量与容错（缺失科目一律 null，绝不填 0）")
    emit(f"  摘要：{report.summary.data_quality}")
    emit(f"  警告总数：{len(report.warnings)}")
    for item in report.warnings[:40]:
        emit(f"    [{item.code}] {item.period or '-'} | {item.message}")
    if len(report.warnings) > 40:
        emit(f"    ...（其余 {len(report.warnings) - 40} 条见 JSON）")


def render_summary(report: FinancialReport) -> None:
    banner("[8] 综合结论（确定性文本，由算法生成，非 LLM 推断）")
    summary = report.summary
    emit(f"  风险等级：{summary.risk_level}   触发预警：{summary.triggered_flags} 条"
         f"（高严重度 {summary.high_severity_flags} 条）")
    emit(f"  数据质量：{summary.data_quality}")
    emit(f"  口径声明：{summary.risk_disclaimer}")
    emit("  关键发现：")
    for finding in summary.key_findings:
        emit(f"    · {finding}")


# ---------------------------------------------------------------------------
# 断言：数值合理性与内部一致性
# ---------------------------------------------------------------------------
def validate_numbers(report: FinancialReport, checker: Checker, code: str) -> None:
    banner("[9] 数值合理性断言")

    periods = report.meta.report_periods
    checker.check(len(periods) >= 3, "覆盖期不少于 3 个完整会计年度", f"实际 {len(periods)}")

    latest = report.growth_profitability.years[0]
    checker.check(
        latest.total_operating_revenue is not None and latest.total_operating_revenue > 0,
        "最新年度营业总收入存在且为正",
        f"{latest.total_operating_revenue} 亿元",
    )

    gm = latest.gross_margin
    if code == "600519":
        checker.check(gm is not None and 0.85 <= gm <= 0.95, "茅台毛利率落在 85%~95% 合理区间",
                      f"{gm:.2%}" if gm is not None else "null")
    elif gm is not None:
        checker.check(-1.0 < gm < 1.0, "毛利率落在 (-100%, 100%) 合理区间", f"{gm:.2%}")
    else:
        checker.skip("毛利率区间", "该标的毛利率不可计算")

    roe = report.dupont.years[0].roe_dupont if report.dupont.years else None
    checker.check(roe is not None and 0.0 < roe < 1.5, "最新年度 ROE 处于 (0,150%) 合理区间",
                  f"{roe:.2%}" if roe is not None else "null")

    errors = [
        abs(row.identity_check_error)
        for row in report.dupont.years
        if row.identity_check_error is not None
    ]
    checker.check(bool(errors) and max(errors) < 1e-6, "杜邦恒等式误差 < 1e-6",
                  f"max={max(errors):.3e}" if errors else "无可用年度")

    # ---- 内部一致性：用 raw_metrics 复算关键比率 ----
    raw = report.raw_metrics[latest.report_period]
    cf = report.cashflow_quality.years[0]
    if raw.net_cash_from_operating and raw.net_profit:
        recomputed = raw.net_cash_from_operating / raw.net_profit
        checker.check(
            cf.net_cash_ratio is not None and abs(cf.net_cash_ratio - recomputed) < 1e-5,
            "净现比可由 raw_metrics 复算",
            f"{cf.net_cash_ratio} vs {recomputed:.6f}",
        )
    if raw.operating_revenue and raw.operating_cost is not None and raw.operating_revenue > 0:
        recomputed_gm = (raw.operating_revenue - raw.operating_cost) / raw.operating_revenue
        checker.check(
            latest.gross_margin is not None and abs(latest.gross_margin - recomputed_gm) < 1e-5,
            "毛利率可由 raw_metrics 复算",
            f"{latest.gross_margin} vs {recomputed_gm:.6f}",
        )
    if raw.cash_from_sales and raw.operating_revenue:
        recomputed_cr = raw.cash_from_sales / raw.operating_revenue
        checker.check(
            cf.cash_collection_ratio is not None
            and abs(cf.cash_collection_ratio - recomputed_cr) < 1e-5,
            "收现比可由 raw_metrics 复算",
            f"{cf.cash_collection_ratio} vs {recomputed_cr:.6f}",
        )

    # ---- 回归基准：与接口实测值对齐，防口径漂移 ----
    if code == "600519":
        expected = {"2024年报": 1741.4407, "2023年报": 1505.6033}
        for period, value in expected.items():
            actual = report.raw_metrics.get(period)
            actual_value = None if actual is None else actual.total_operating_revenue
            checker.check(
                actual_value is not None and abs(actual_value - value) < 0.001,
                f"{period} 营业总收入回归基准 = {value} 亿元",
                f"实际 {actual_value}",
            )

    # ---- 溯源完整性 ----
    checker.check(
        set(report.raw_metrics) == set(periods),
        "raw_metrics 覆盖全部报告期",
        f"{sorted(report.raw_metrics)}",
    )
    checker.check(
        all(item.message.strip() for item in report.warnings),
        "每条警告都有中文说明",
        f"{len(report.warnings)} 条",
    )
    checker.check(
        all(flag.code and flag.name and flag.summary for flag in report.forensic.rule_flags),
        "每条规则结果都有 code/name/summary",
        f"{len(report.forensic.rule_flags)} 条",
    )

    # ---- JSON 严格性 ----
    payload = report.model_dump(mode="json")
    try:
        json.dumps(payload, ensure_ascii=False, allow_nan=False)
        serializable = True
        error_text = ""
    except (TypeError, ValueError) as exc:
        serializable = False
        error_text = f"{type(exc).__name__}: {exc}"
    checker.check(serializable, "报告可通过 json.dumps(allow_nan=False) 序列化", error_text)

    bad = [(path, value) for path, value in iter_floats(payload) if not math.isfinite(value)]
    checker.check(not bad, "报告内不存在 NaN / Infinity", f"异常字段 {bad[:3]}" if bad else "0 处")


# ---------------------------------------------------------------------------
# 场景测试：金融类降级 & 制造业
# ---------------------------------------------------------------------------
def scenario_financial(engine: FinancialEngine, checker: Checker) -> None:
    banner("[10] 降级路径验证：金融类企业（招商银行 600036）")
    try:
        report = engine.analyze("600036", lookback_years=3)
    except DataUnavailableError as exc:
        checker.skip("金融类降级路径", f"数据不可用：{exc}")
        return
    except Exception as exc:  # noqa: BLE001
        checker.check(False, "金融类降级路径执行", f"{type(exc).__name__}: {exc}")
        return

    emit(f"  标的：{report.meta.stock_code} {report.meta.stock_name}，"
         f"ORG_TYPE={report.meta.org_type}，is_financial={report.meta.is_financial}")
    emit(f"  判定依据：{report.meta.financial_reason}")
    checker.check(report.meta.is_financial, "识别为金融类企业")
    checker.check(not report.forensic.beneish.applicable, "Beneish M-Score 标注为不适用")
    checker.check(
        report.forensic.beneish.m_score is None and not report.forensic.beneish.variables,
        "金融类不输出 M-Score 数值（避免误导）",
    )
    inv_flag = next(
        (flag for flag in report.forensic.rule_flags if flag.code == "INVENTORY_PILEUP"), None
    )
    checker.check(
        inv_flag is not None and not inv_flag.evaluated, "存货积压规则标注为不适用而非崩溃"
    )
    latest = report.growth_profitability.years[0]
    checker.check(latest.gross_margin is None, "金融类毛利率返回 null（不适用）")
    checker.check(
        report.efficiency.years == [], "金融类营运效率维度为空"
    )
    checker.check(
        latest.total_operating_revenue is not None,
        "金融类营业总收入缺失时已退回营业收入口径",
        f"{latest.total_operating_revenue} 亿元",
    )
    checker.check(
        report.dupont.years and report.dupont.years[0].roe_dupont is not None,
        "金融类杜邦 ROE 仍可计算（不因缺列而全盘 null）",
        f"{report.dupont.years[0].roe_dupont}" if report.dupont.years else "无年度",
    )
    emit()
    emit(f"  ROE(杜邦) = {report.dupont.years[0].roe_dupont}，"
         f"净利率 = {latest.net_margin}，总资产 {report.raw_metrics[report.meta.latest_report_period].total_assets} 亿元")
    emit(f"  风险等级：{report.summary.risk_level}，警告 {len(report.warnings)} 条")

    try:
        payload = report.model_dump(mode="json")
        json.dumps(payload, ensure_ascii=False, allow_nan=False)
        bad = [p for p, v in iter_floats(payload) if not math.isfinite(v)]
        checker.check(not bad, "金融类报告 JSON 合法且无 NaN")
    except (TypeError, ValueError) as exc:
        checker.check(False, "金融类报告 JSON 合法且无 NaN", f"{type(exc).__name__}: {exc}")


def scenario_manufacturing(engine: FinancialEngine, checker: Checker) -> None:
    banner("[11] 稳健性验证：制造业（宁德时代 300750）")
    try:
        report = engine.analyze("300750", lookback_years=4)
    except DataUnavailableError as exc:
        checker.skip("制造业稳健性", f"数据不可用：{exc}")
        return
    except Exception as exc:  # noqa: BLE001
        checker.check(False, "制造业稳健性执行", f"{type(exc).__name__}: {exc}")
        return

    latest = report.growth_profitability.years[0]
    forensic = report.forensic
    emit(f"  标的：{report.meta.stock_code} {report.meta.stock_name}，is_financial={report.meta.is_financial}")
    emit(f"  最新年度：营收 {latest.total_operating_revenue} 亿元（YoY {_pct(latest.total_operating_revenue_yoy).strip()}），"
         f"归母 {latest.parent_net_profit} 亿元，毛利率 {_pct(latest.gross_margin).strip()}")
    emit(f"  M-Score = {forensic.beneish.m_score}（{forensic.beneish.risk_text}）")
    for flag in forensic.rule_flags:
        state = "触发" if flag.triggered else ("跳过" if not flag.evaluated else "未触发")
        emit(f"    - {flag.name:<14} {state}")
    emit(f"  综合风险等级：{forensic.risk_level}，警告 {len(report.warnings)} 条")

    checker.check(not report.meta.is_financial, "制造业识别为非金融类")
    checker.check(forensic.beneish.applicable, "制造业 Beneish 适用")
    checker.check(
        forensic.beneish.m_score is not None and math.isfinite(forensic.beneish.m_score),
        "制造业 M-Score 为有限数值",
        f"{forensic.beneish.m_score}",
    )
    checker.check(
        len(report.forensic.rule_flags) == 6, "规则数量为 6 条", f"{len(report.forensic.rule_flags)}"
    )
    checker.check(latest.gross_margin is not None, "制造业毛利率可计算",
                  f"{latest.gross_margin:.2%}" if latest.gross_margin is not None else "null")


# ---------------------------------------------------------------------------
# Phase 1.1 会计口径精修：真实报告断言 + 脱机单元断言
# ---------------------------------------------------------------------------
def validate_phase11_real(report: FinancialReport, checker: Checker) -> None:
    """针对 600519 真实报告，逐项校验 Phase 1.1 新增的会计口径。"""
    banner("[12] Phase 1.1 精修 · 真实报告断言（货币资金勾稽 / 重要性 / 非经常性方向）")
    flags = {flag.code: flag for flag in report.forensic.rule_flags}

    # ---- A. 存贷双高：期末现金等价物勾稽 ----
    cash_flag = flags["CASH_LOAN_DOUBLE_HIGH"]
    evidence = cash_flag.evidence
    emit(f"  货币资金 {evidence.get('monetary_funds')} 亿元 / "
         f"期末现金及现金等价物 {evidence.get('ending_cash_and_equivalents')} 亿元 / "
         f"营业总收入-营业收入 {evidence.get('revenue_gap_total_minus_operating')} 亿元")
    checker.check(
        evidence.get("ending_cash_and_equivalents") is not None,
        "存贷双高：已用 END_CCE 勾稽货币资金",
        f"{evidence.get('ending_cash_and_equivalents')} 亿元",
    )
    checker.check(
        evidence.get("cce_exceeds_cash") is True,
        "期末现金等价物显著大于货币资金（>1.05 倍）",
        f"{evidence.get('ending_cash_and_equivalents')} vs {evidence.get('monetary_funds')}",
    )
    checker.check(
        evidence.get("financial_company_hint") is True, "已识别为合并财务公司列报口径差异"
    )
    checker.check(
        evidence.get("implied_restricted_cash_ratio") is None
        and evidence.get("restricted_cash") is None,
        "未误判为受限货币资金（现金等价物 > 货币资金时不计算受限比例）",
    )
    checker.check(
        any("财务公司" in line for line in cash_flag.detail),
        "detail 已标注「合并集团财务公司（存放央行及同业款项未列入货币资金）」",
    )
    gap = evidence.get("revenue_gap_total_minus_operating")
    checker.check(
        gap is not None and abs(gap - 32.1607) < 0.01,
        "营业总收入 - 营业收入 差额 = 32.1607 亿元（利息收入等非主营收入）",
        f"实际 {gap}",
    )
    composition = evidence.get("debt_composition") or {}
    checker.check(
        bool(composition) and all(v is None or round(v, 4) == v for v in composition.values()),
        "有息负债明细 debt_composition 已统一为 4 位小数",
        f"{composition}",
    )

    # ---- B. 纸面富贵：重要性原则 ----
    paper = flags["PAPER_PROFIT"].evidence
    emit(f"  应收账款占营业收入 {paper.get('receivables_to_revenue')} / "
         f"重要性门槛 {paper.get('min_receivables_to_revenue')}")
    checker.check(
        paper.get("receivables_to_revenue") is not None
        and paper.get("min_receivables_to_revenue") == 0.01,
        "纸面富贵：已落地 1% 重要性门槛",
        f"{paper.get('receivables_to_revenue')}",
    )
    checker.check(
        paper.get("material_receivables") is False,
        "茅台应收账款占营收约 0.0015%，未达重要性门槛（不因极小基数波动误报）",
    )

    # ---- C. 非经常性损益：方向判定 ----
    nonrecurring = flags["NONRECURRING_RELIANCE"].evidence
    emit(f"  非经常性损益 {nonrecurring.get('nonrecurring_amount')} 亿元（净收益）"
         f"/ 依赖度 {nonrecurring.get('reliance')}")
    checker.check(
        nonrecurring.get("nonrecurring_direction") == "income",
        "非经常性损益方向判定为「净收益」",
        f"{nonrecurring.get('nonrecurring_direction')}",
    )
    checker.check(
        nonrecurring.get("is_reverse_turnaround") is False,
        "未误判为保壳扭亏（归母与扣非同为正）",
    )
    checker.check(
        flags["NONRECURRING_RELIANCE"].triggered is False,
        "0.03% 的净收益占比不触发依赖预警",
        f"reliance={nonrecurring.get('reliance')}",
    )

    # ---- D. 精度统一 ----
    yoy_values = [
        value
        for row in report.growth_profitability.years
        for value in (
            row.total_operating_revenue_yoy,
            row.operating_revenue_yoy,
            row.parent_net_profit_yoy,
            row.deducted_parent_net_profit_yoy,
            row.net_profit_yoy,
        )
    ]
    checker.check(
        all(value is None or round(value, 6) == value for value in yoy_values),
        "YearGrowth 全部 YoY 字段已按 6 位小数取整",
        f"{len(yoy_values)} 个字段",
    )
    goodwill_detail = flags["GOODWILL_IMPAIRMENT_RISK"].detail
    checker.check(
        bool(goodwill_detail) and "商誉" in goodwill_detail[0] and "归母净资产" in goodwill_detail[0],
        "商誉 detail 已按科目格式化输出",
        goodwill_detail[0] if goodwill_detail else "空",
    )


def _synthetic_bundle(
    *,
    profit_rows: Sequence[dict[str, Any]],
    balance_rows: Sequence[dict[str, Any]] = (),
    cash_rows: Sequence[dict[str, Any]] = (),
    org_type: str = "通用",
) -> StatementBundle:
    """构造最小可用的合成报表，用于脱机验证规则分支（金额单位：元）。"""

    def to_frame(rows: Sequence[dict[str, Any]]) -> pd.DataFrame:
        frame = pd.DataFrame(list(rows))
        if frame.empty:
            return pd.DataFrame(columns=["_report_year"])
        frame["_report_year"] = frame["_report_year"].astype(int)
        return frame

    return StatementBundle(
        code="000000",
        em_symbol="SZ000000",
        market_prefix="SZ",
        stock_name="脱机合成样本",
        org_type=org_type,
        frames={
            "balance_sheet": to_frame(balance_rows),
            "profit_sheet": to_frame(profit_rows),
            "cash_flow_sheet": to_frame(cash_rows),
        },
        provenance={},
        audit_opinions={},
    )


def _yi(value: float) -> float:
    """亿元 → 元（仅用于合成样本）。"""
    return value * 1e8


# 说明：下列三个 _run_* 直接调用引擎的规则方法（私有），目的是在不依赖网络与
# 特定上市公司真实财报的前提下，锁定"分支方向性"这类只能靠构造样本验证的逻辑。
def _run_nonrecurring(engine: FinancialEngine, parent_yi: float, deducted_yi: float) -> Any:
    bundle = _synthetic_bundle(
        profit_rows=[
            {
                "_report_year": 2025,
                "PARENT_NETPROFIT": _yi(parent_yi),
                "DEDUCT_PARENT_NETPROFIT": _yi(deducted_yi),
            }
        ]
    )
    return engine._rule_nonrecurring_reliance(  # noqa: SLF001 - 见上方说明
        _StatementView(bundle, WarningCollector("000000")), [2025, 2024]
    )


def _run_cash_rule(
    engine: FinancialEngine,
    *,
    cash_yi: float,
    cce_yi: float,
    assets_yi: float,
    interest_yi: float,
    cash_prev_yi: float,
) -> Any:
    bundle = _synthetic_bundle(
        balance_rows=[
            {
                "_report_year": 2025,
                "MONETARYFUNDS": _yi(cash_yi),
                "TOTAL_ASSETS": _yi(assets_yi),
            },
            {
                "_report_year": 2024,
                "MONETARYFUNDS": _yi(cash_prev_yi),
                "TOTAL_ASSETS": _yi(assets_yi * 0.96),
            },
        ],
        profit_rows=[
            {
                "_report_year": 2025,
                "FE_INTEREST_INCOME": _yi(interest_yi),
                "TOTAL_OPERATE_INCOME": _yi(100.0),
                "OPERATE_INCOME": _yi(100.0),
            },
            {
                "_report_year": 2024,
                "TOTAL_OPERATE_INCOME": _yi(90.0),
                "OPERATE_INCOME": _yi(90.0),
            },
        ],
        cash_rows=[{"_report_year": 2025, "END_CCE": _yi(cce_yi)}],
    )
    return engine._rule_cash_and_debt_double_high(  # noqa: SLF001
        _StatementView(bundle, WarningCollector("000000")), [2025, 2024], False
    )


def _run_paper_rule(
    engine: FinancialEngine,
    *,
    revenue_yi: float,
    receivables_yi: float,
    revenue_prev_yi: float,
    receivables_prev_yi: float,
    net_profit_yi: float,
    ocf_yi: float,
) -> Any:
    bundle = _synthetic_bundle(
        profit_rows=[
            {"_report_year": 2025, "OPERATE_INCOME": _yi(revenue_yi), "NETPROFIT": _yi(net_profit_yi)},
            {
                "_report_year": 2024,
                "OPERATE_INCOME": _yi(revenue_prev_yi),
                "NETPROFIT": _yi(net_profit_yi),
            },
        ],
        balance_rows=[
            {"_report_year": 2025, "ACCOUNTS_RECE": _yi(receivables_yi)},
            {"_report_year": 2024, "ACCOUNTS_RECE": _yi(receivables_prev_yi)},
        ],
        cash_rows=[
            {"_report_year": 2025, "NETCASH_OPERATE": _yi(ocf_yi)},
            {"_report_year": 2024, "NETCASH_OPERATE": _yi(ocf_yi)},
        ],
    )
    return engine._rule_paper_profit(  # noqa: SLF001
        _StatementView(bundle, WarningCollector("000000")), [2025, 2024], False
    )


def validate_phase11_offline(checker: Checker) -> None:
    """用合成样本锁定 Phase 1.1 新增分支的方向性（不联网、可重复）。"""
    banner("[13] Phase 1.1 精修 · 脱机单元断言（合成样本，不依赖网络）")
    engine = FinancialEngine()

    # ---- A. 非经常性损益：六类情形 ----
    emit("  A. 非经常性损益：正负方向 / 保壳扭亏 / 亏损掩盖")
    scenarios = (
        ("保壳扭亏（归母 +0.5 亿、扣非 -1 亿）", 0.5, -1.0, True, "high", "income", True),
        ("非经常性净支出（归母 10、扣非 11）", 10.0, 11.0, False, "info", "expense", False),
        ("常规依赖 40%（归母 10、扣非 6）", 10.0, 6.0, True, "medium", "income", False),
        ("小额净收益 10%（归母 10、扣非 9）", 10.0, 9.0, False, "info", "income", False),
        ("亏损被补助掩盖（归母 -10、扣非 -14）", -10.0, -14.0, True, "medium", "income", False),
        ("双亏且净支出（归母 -10、扣非 -8）", -10.0, -8.0, False, "info", "expense", False),
    )
    for label, parent, deducted, expect_triggered, expect_severity, expect_dir, expect_reverse in scenarios:
        flag = _run_nonrecurring(engine, parent, deducted)
        checker.check(
            flag.triggered is expect_triggered
            and flag.severity == expect_severity
            and flag.evidence.get("nonrecurring_direction") == expect_dir
            and flag.evidence.get("is_reverse_turnaround") is expect_reverse,
            f"非经常性损益 · {label}",
            f"triggered={flag.triggered} severity={flag.severity} "
            f"direction={flag.evidence.get('nonrecurring_direction')}",
        )

    # ---- B. 存贷双高：受限货币资金 ----
    emit("  B. 存贷双高：受限货币资金测算")
    high = _run_cash_rule(
        engine, cash_yi=100.0, cce_yi=40.0, assets_yi=500.0, interest_yi=10.0, cash_prev_yi=90.0
    )
    checker.check(
        high.triggered is True
        and high.severity == "high"
        and high.evidence.get("restricted_cash_high") is True
        and abs(float(high.evidence.get("implied_restricted_cash_ratio") or 0) - 0.6) < 1e-6,
        "受限比例 60%（货币资金 100 亿 / 现金等价物 40 亿）→ 高受限风险",
        f"triggered={high.triggered} severity={high.severity} "
        f"ratio={high.evidence.get('implied_restricted_cash_ratio')}",
    )
    checker.check(
        any("受限货币资金" in line for line in high.detail), "受限资金结论已写入 detail"
    )

    low = _run_cash_rule(
        engine, cash_yi=100.0, cce_yi=85.0, assets_yi=500.0, interest_yi=10.0, cash_prev_yi=90.0
    )
    checker.check(
        low.triggered is False and low.evidence.get("restricted_cash_high") is False,
        "受限比例 15%（<30% 警戒线）→ 不触发，避免过度预警",
        f"ratio={low.evidence.get('implied_restricted_cash_ratio')}",
    )

    # ---- C. 纸面富贵：重要性原则 ----
    emit("  C. 纸面富贵：重要性原则（应收账款 / 营业收入 ≥ 1%）")
    material = _run_paper_rule(
        engine,
        revenue_yi=10.0,
        receivables_yi=0.5,
        revenue_prev_yi=5.0,
        receivables_prev_yi=0.1,
        net_profit_yi=1.0,
        ocf_yi=2.0,
    )
    checker.check(
        material.triggered is True and material.evidence.get("material_receivables") is True,
        "应收账款占营收 5%、增速差 300pct → 触发（重要性充足）",
        f"ratio={material.evidence.get('receivables_to_revenue')}",
    )

    immaterial = _run_paper_rule(
        engine,
        revenue_yi=10.0,
        receivables_yi=0.05,
        revenue_prev_yi=5.0,
        receivables_prev_yi=0.01,
        net_profit_yi=1.0,
        ocf_yi=2.0,
    )
    checker.check(
        immaterial.triggered is False
        and immaterial.evidence.get("growth_gap_exceeded") is True
        and immaterial.evidence.get("material_receivables") is False
        and any("重要性" in line for line in immaterial.detail),
        "应收账款占营收 0.5%（<1%）→ 增速差超阈值但按重要性原则不触发",
        f"ratio={immaterial.evidence.get('receivables_to_revenue')}",
    )


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="FinSight-Agent 财务引擎实盘验证")
    parser.add_argument("--code", default="600519", help="主验证标的（默认 600519 贵州茅台）")
    parser.add_argument("--lookback", type=int, default=4, help="回溯年度数（默认 4）")
    parser.add_argument("--refresh", action="store_true", help="忽略缓存强制拉取")
    parser.add_argument("--no-json", action="store_true", help="不打印完整 JSON")
    parser.add_argument("--no-cache", action="store_true", help="使用临时缓存目录（验证冷启动）")
    parser.add_argument("--no-scenarios", action="store_true", help="跳过金融类/制造业场景测试")
    parser.add_argument("--cross-validation", dest="cross_validation", action="store_true",
                        default=True, help="启用交叉校验（默认启用）")
    parser.add_argument("--no-cross-validation", dest="cross_validation", action="store_false",
                        help="关闭交叉校验")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    ensure_directories()
    checker = Checker()
    started = time.perf_counter()

    cache_dir = None
    if args.no_cache:
        cache_dir = PROJECT_ROOT / "data" / "cache" / "_tmp_validation"
        emit(f"[info] 使用临时缓存目录验证冷启动：{cache_dir}")

    engine = FinancialEngine(cache_dir=cache_dir)

    banner("FinSight-Agent · Phase 1 财务引擎实盘验证")
    emit(f"  运行时间：{dt.datetime.now().isoformat(timespec='seconds')}")
    emit(f"  主标的  ：{args.code}（回溯 {args.lookback} 年）")
    emit(f"  交叉校验：{args.cross_validation}")

    try:
        report = engine.analyze(
            args.code,
            lookback_years=args.lookback,
            force_refresh=args.refresh,
            include_cross_validation=args.cross_validation,
        )
    except (DataUnavailableError, ValueError) as exc:
        emit(f"[FATAL] 主流程失败：{type(exc).__name__}: {exc}")
        traceback.print_exc()
        _write_transcript()
        return 1

    elapsed = time.perf_counter() - started

    # ---- 渲染 ----
    render_meta(report)
    render_growth(report)
    render_dupont(report)
    render_cashflow(report)
    render_efficiency(report)
    render_forensic(report)
    render_cross_validation(report, checker)
    render_warnings(report)
    render_summary(report)

    # ---- 断言 ----
    validate_numbers(report, checker, args.code)
    cross = report.cross_validation
    if cross is not None and cross.available:
        checker.check(
            cross.failed_count == 0,
            "交叉校验全部通过（偏差在容差内）",
            f"不通过 {cross.failed_count} 项",
        )

    if not args.no_scenarios:
        scenario_financial(engine, checker)
        scenario_manufacturing(engine, checker)

    # ---- Phase 1.1 会计口径精修断言 ----
    validate_phase11_real(report, checker)
    validate_phase11_offline(checker)

    # ---- 完整 JSON ----
    json_text = report_to_json(report, indent=2)
    banner("[14] 标准 JSON 输出（可直接作为下游 Agent 的唯一事实来源）")
    if args.no_json:
        emit(f"  已跳过打印（--no-json）。JSON 长度 {len(json_text)} 字符。")
    else:
        emit(json_text)

    output_path = save_report(report)
    checker.check(output_path.exists() and output_path.stat().st_size > 1000,
                  "报告已落盘为 JSON 文件", str(output_path))

    total_elapsed = time.perf_counter() - started
    banner("验证完成")
    emit(f"  主流程耗时：{elapsed:.2f}s   总耗时：{total_elapsed:.2f}s")
    emit(f"  JSON 报告：{output_path}")
    emit(f"  文本副本：{_write_transcript()}")
    exit_code = checker.summary()

    emit()
    if exit_code == 0:
        emit("  >>> 全部断言通过：Phase 1 财务计算与会计排雷引擎验证成功。")
    else:
        emit("  >>> 存在失败断言，请修复后重跑。")
    _write_transcript()
    return exit_code


def _write_transcript() -> Path:
    """把终端输出同步保存一份 UTF-8 文本，便于在控制台编码异常时复核。"""
    ensure_directories()
    target = REPORT_OUTPUT_DIR / "phase1_validation_transcript.txt"
    try:
        target.write_text("\n".join(TRANSCRIPT), encoding="utf-8")
    except OSError:
        pass
    return target


if __name__ == "__main__":
    raise SystemExit(main())
