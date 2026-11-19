"""Patch 4.1 · 北交所 920 新号段 / 新三板兼容与友好降级验证。

运行：:

    python tests/test_market_compat.py            # 离线断言（打桩，不发请求，秒级）
    python tests/test_market_compat.py --online   # 追加实盘断言（联网）

覆盖范围
--------
1. **市场前缀映射**：``920xxx`` → ``BJ``（**不是** SH）、``900xxx`` → SH、
   ``43/83/87/88`` → BJ 旧号段（标记上游三表不可用）；
2. **巨潮 column 映射**：``920`` → ``bj``、``900`` → ``sse``；
3. **异常翻译**：上游 ``TypeError: 'NoneType' object is not subscriptable`` 必须
   被翻译成中文业务提示，**提示文本中不得出现 NoneType / subscriptable**；
4. **同业降级**：无法取数的新三板代码自动跳过并记录原因，绝不阻塞主标的；
5. **实盘（--online）**：``920012`` 完整出报表；``874886`` 干净报错；
   混入同业时主标的照常完成。
"""

from __future__ import annotations

import argparse
import sys
import time
import traceback
from pathlib import Path
from typing import Optional, Sequence

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (ValueError, OSError):
            pass

from src.rag.pdf_fetcher import _market_column  # noqa: E402
from src.tools.financial_data import (  # noqa: E402
    REPORT_TABLES,
    DataUnavailableError,
    FinancialDataProvider,
    classify_market,
    describe_unavailable,
    split_symbol,
)

TRANSCRIPT: list[str] = []


def emit(text: str = "") -> None:
    print(text)
    TRANSCRIPT.append(text)


def banner(title: str) -> None:
    emit()
    emit("=" * 92)
    emit(title)
    emit("=" * 92)


class Checker:
    def __init__(self) -> None:
        self.passed = 0
        self.failed: list[str] = []
        self.skipped: list[str] = []

    def check(self, condition: bool, label: str, detail: str = "") -> bool:
        if condition:
            self.passed += 1
            emit(f"  [OK]   {label}" + (f"  ({detail})" if detail else ""))
            return True
        self.failed.append(f"{label}" + (f"  ({detail})" if detail else ""))
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


#: (代码, 期望前缀, 期望板块关键词, 上游三表是否可用)
MAPPING_CASES: tuple[tuple[str, str, str, bool], ...] = (
    ("600519", "SH", "沪市", True),
    ("688981", "SH", "沪市", True),
    ("900901", "SH", "沪市 B 股", True),   # 900 必须是 SH，不能落到 BJ
    ("300750", "SZ", "创业板", True),
    ("002594", "SZ", "深市", True),
    ("000858", "SZ", "深市", True),
    ("200596", "SZ", "深市 B 股", True),
    ("920012", "BJ", "北交所", True),      # 新号段：必须 BJ 且上游可用
    ("920000", "BJ", "北交所", True),
    ("430047", "BJ", "旧号段", False),
    ("830799", "BJ", "旧号段", False),
    ("870436", "BJ", "旧号段", False),
    ("874886", "BJ", "旧号段", False),     # 新三板挂牌
)

COLUMN_CASES: tuple[tuple[str, str], ...] = (
    ("600519", "sse"),
    ("900901", "sse"),
    ("300750", "szse"),
    ("920012", "bj"),
    ("920000", "bj"),
    ("874886", "bj"),
    ("430047", "bj"),
)


# ---------------------------------------------------------------------------
# [1][2] 映射表
# ---------------------------------------------------------------------------
def check_mapping(checker: Checker) -> None:
    banner("[1] 市场前缀映射（financial_data.split_symbol / classify_market）")
    for code, expected_prefix, board_keyword, em_ok in MAPPING_CASES:
        market = classify_market(code)
        prefix, em_symbol = split_symbol(code)
        checker.check(
            prefix == expected_prefix and em_symbol == f"{expected_prefix}{code}",
            f"{code} → {expected_prefix}",
            f"{em_symbol}｜{market.board}",
        )
        checker.check(
            board_keyword in market.board,
            f"{code} 板块名称含「{board_keyword}」",
            market.board,
        )
        checker.check(
            market.em_yearly_supported is em_ok,
            f"{code} 上游三表可用性标记 = {em_ok}",
            str(market.em_yearly_supported),
        )

    # 回归：920 绝不能被判成沪市（旧实现的 bug）
    checker.check(split_symbol("920012")[0] != "SH", "920012 不再被误判为沪市")
    checker.check(split_symbol("900901")[0] == "SH", "900901 仍为沪市 B 股")
    for bad in ("60051", "6005199", "abcdef", ""):
        try:
            split_symbol(bad)
            checker.check(False, f"非法代码 {bad!r} 应抛 ValueError")
        except ValueError:
            checker.check(True, f"非法代码 {bad!r} 抛 ValueError")

    banner("[2] 巨潮 column 映射（pdf_fetcher._market_column）")
    for code, expected in COLUMN_CASES:
        actual = _market_column(code)
        checker.check(actual == expected, f"{code} → column={expected}", actual)


# ---------------------------------------------------------------------------
# [3] 异常翻译
# ---------------------------------------------------------------------------
def check_error_translation(checker: Checker) -> None:
    banner("[3] 上游异常 → 中文业务提示（不得泄露 NoneType）")
    spec = REPORT_TABLES[0]

    old_market = classify_market("874886")
    err = describe_unavailable(
        "874886", spec, old_market, exc=TypeError("'NoneType' object is not subscriptable")
    )
    text = str(err)
    checker.check(isinstance(err, DataUnavailableError), "旧号段失败抛 DataUnavailableError")
    checker.check("NoneType" not in text, "提示中无 NoneType")
    checker.check("subscriptable" not in text, "提示中无 subscriptable")
    checker.check("874886" in text and "新三板" in text, "提示点名代码与板块归属")
    checker.check("PDF" in text or "年报" in text, "提示给出「上传 PDF 查附注」的可行出路")
    checker.check("Comparable" not in text and "可比公司" in text, "提示给出「改用可比公司」的出路")
    emit(f"  示例文案：{text}")

    net_err = describe_unavailable(
        "600519", spec, classify_market("600519"), exc=ConnectionResetError("boom")
    )
    checker.check("网络" in str(net_err), "网络异常走「网络/代理」措辞", str(net_err)[:60])
    checker.check("NoneType" not in str(net_err), "网络异常提示同样不含 NoneType")

    generic = describe_unavailable("920012", spec, classify_market("920012"), empty=True)
    checker.check("暂不可用" in str(generic), "新号段空数据走通用措辞")
    checker.check("920" in str(generic), "通用措辞提示北交所 920 号段规则")

    # 真实调用链：打桩 akshare 让接口抛 NoneType，验证 load() 的翻译
    import akshare as ak

    original = getattr(ak, spec.interface)

    def _boom(symbol: str):  # noqa: ANN202
        raise TypeError("'NoneType' object is not subscriptable")

    setattr(ak, spec.interface, _boom)
    try:
        provider = FinancialDataProvider(cache_dir=PROJECT_ROOT / "data" / "cache" / "_probe_compat")
        try:
            provider.load("430047", force_refresh=True)  # 无本地 PDF 的代码
            checker.check(False, "打桩后 load() 应抛错")
        except DataUnavailableError as exc:
            message = str(exc)
            checker.check(True, "打桩后 load() 抛 DataUnavailableError")
            checker.check(
                "NoneType" not in message and "subscriptable" not in message,
                "load() 透出的消息不含底层类名",
                message[:60] + "…",
            )
            checker.check("430047" in message, "消息包含代码")
        except Exception as exc:  # noqa: BLE001
            checker.check(False, "打桩后抛出了非业务异常", f"{type(exc).__name__}: {exc}")
    finally:
        setattr(ak, spec.interface, original)


# ---------------------------------------------------------------------------
# [4] 同业降级
# ---------------------------------------------------------------------------
def check_peer_skip(checker: Checker) -> None:
    banner("[4] 同业混入新三板代码（打桩，不联网）")
    import src.tools.peer_comparison as pc_module

    original = pc_module.analyze_stock

    def _fake_analyze(code: str, lookback_years: int = 4):  # noqa: ANN202
        digits = "".join(ch for ch in str(code) if ch.isdigit())
        if digits in {"874886", "430047"}:
            raise describe_unavailable(digits, REPORT_TABLES[0], classify_market(digits))
        return original(code, lookback_years=lookback_years)

    pc_module.analyze_stock = _fake_analyze
    try:
        result = pc_module.compare_peers("600519", ["000858", "874886", "430047"], lookback_years=3)
    finally:
        pc_module.analyze_stock = original

    checker.check(len(result.rows) >= 2, "主标的与可用同业正常入表", f"{len(result.rows)} 行")
    checker.check(len(result.skipped) == 2, "两个不可用代码被跳过", f"{len(result.skipped)} 条")
    joined = " ".join(result.skipped)
    checker.check("NoneType" not in joined, "跳过原因不含 NoneType")
    checker.check("874886" in joined and "430047" in joined, "跳过原因点名代码")
    checker.check(result.rows[0].is_target, "首行为目标公司")
    for item in result.skipped:
        emit(f"  跳过：{item[:100]}…")


# ---------------------------------------------------------------------------
# [5] 实盘
# ---------------------------------------------------------------------------
def check_online(checker: Checker) -> None:
    banner("[5] 实盘断言（联网）")
    from src.tools.financial_data import DataUnavailableError as BusErr
    from src.tools.financial_engine import analyze_stock

    t0 = time.perf_counter()
    try:
        report = analyze_stock("920012", lookback_years=3, force_refresh=True)
    except Exception as exc:  # noqa: BLE001
        checker.check(False, "920012 实盘出报表", f"{type(exc).__name__}: {exc}")
        return
    growth = report.growth_profitability.years[0]
    checker.check(report.meta.stock_code == "920012", "920012 取数成功", report.meta.stock_name)
    checker.check(report.meta.market_prefix == "BJ", "市场前缀为 BJ", report.meta.market_prefix)
    checker.check(
        growth.total_operating_revenue is not None and growth.total_operating_revenue > 0,
        "营业总收入为正数",
        f"{growth.total_operating_revenue} 亿元",
    )
    checker.check(
        report.dupont.years and report.dupont.years[0].roe_dupont is not None,
        "杜邦 ROE 可计算",
        f"{report.dupont.years[0].roe_dupont if report.dupont.years else None}",
    )
    checker.check(report.forensic.risk_level in {"high", "medium", "low"}, "风险等级已判定",
                  report.forensic.risk_level)
    emit(
        f"  耗时 {time.perf_counter() - t0:.1f}s｜营收 {growth.total_operating_revenue} 亿｜"
        f"M-Score {report.forensic.beneish.m_score}"
    )

    from src.tools.pdf_financial_extractor import find_local_pdf

    if find_local_pdf("874886") is not None:
        # Patch 4.2：本地有年报 PDF 时，874886 会自动回退并成功出报表
        try:
            report = analyze_stock("874886", lookback_years=3, force_refresh=True)
            checker.check(
                report.meta.data_source.provider == "local_pdf_tables",
                "874886 通过本地 PDF 兜底成功",
                report.meta.data_source.upstream[:46],
            )
        except Exception as exc:  # noqa: BLE001
            checker.check(False, "874886 PDF 兜底失败", f"{type(exc).__name__}: {exc}")
    else:
        try:
            analyze_stock("874886", lookback_years=3, force_refresh=True)
            checker.check(False, "874886 应抛业务异常")
        except BusErr as exc:
            message = str(exc)
            checker.check("NoneType" not in message, "874886 报错干净（无 NoneType）")
            checker.check("新三板" in message, "874886 报错说明新三板归属")
            emit(f"  874886 提示：{message[:110]}…")
        except Exception as exc:  # noqa: BLE001
            checker.check(False, "874886 抛出了非业务异常", f"{type(exc).__name__}: {exc}")

    from src.rag.pdf_fetcher import AnnualReportFetcher

    try:
        reports = AnnualReportFetcher(verbose=False).list_annual_reports("920000")
        checker.check(len(reports) > 0, "巨潮对北交所 920 号段可列年报", f"{len(reports)} 份")
    except Exception as exc:  # noqa: BLE001
        checker.skip("巨潮 920 号段", f"{type(exc).__name__}: {exc}")


def check_pdf_tables(checker: Checker) -> None:
    """Patch 4.2：本地年报 PDF → 标准化三大表（874886 实盘样本）。"""
    banner("[5] 本地 PDF 三大表还原（874886 映日科技）")
    from src.tools.pdf_financial_extractor import extract_statements, find_local_pdf

    pdf = find_local_pdf("874886")
    if pdf is None:
        checker.skip("PDF 三大表还原", "data/pdfs 下无 874886 年报")
        return
    emit(f"  样本：{pdf.name}")
    extraction = extract_statements(pdf, code="874886")
    checker.check(extraction.code == "874886", "识别证券代码", extraction.code)
    checker.check(extraction.stock_name == "映日科技", "识别公司简称", extraction.stock_name)
    checker.check(extraction.report_year == 2025, "识别会计年度", str(extraction.report_year))
    checker.check(len(extraction.periods) >= 2, "至少还原 2 个完整年度", str(extraction.years))
    checker.check(extraction.field_count >= 80, "字段数充足", str(extraction.field_count))

    latest = extraction.periods[0]
    # 期望值直接取自 PDF 原文（P.8 摘要表 / P.45-55 三大表），误差要求 < 0.01 元
    expectations = {
        ("profit", "OPERATE_INCOME"): 773_174_647.88,
        ("profit", "PARENT_NETPROFIT"): 131_641_160.14,
        ("profit", "DEDUCT_PARENT_NETPROFIT"): 122_462_821.00,
        ("profit", "OPERATE_COST"): 566_794_294.77,
        ("balance", "TOTAL_ASSETS"): 1_284_715_891.60,
        ("balance", "TOTAL_LIABILITIES"): 489_309_549.38,
        ("balance", "TOTAL_PARENT_EQUITY"): 795_406_342.22,
        ("balance", "INVENTORY"): 570_688_124.54,
        ("cash", "SALES_SERVICES"): 913_497_770.04,
        ("cash", "NETCASH_OPERATE"): 147_953_951.88,
        ("cash", "FA_IR_DEPR"): 18_943_272.53,
    }
    for (block, field), expected in expectations.items():
        actual = getattr(latest, block).get(field)
        delta = abs((actual or 0) - expected)
        checker.check(
            actual is not None and delta < 0.01,
            f"{block}.{field} 与 PDF 原文一致",
            f"{actual:,.2f}（差 {delta:.4f}）" if actual is not None else "缺失",
        )

    previous = extraction.periods[1]
    checker.check(
        abs((previous.profit.get("OPERATE_INCOME") or 0) - 646_312_740.03) < 0.01,
        "上期营业收入与 PDF 一致",
        f"{previous.profit.get('OPERATE_INCOME'):,.2f}",
    )
    checker.check(
        not any("未完全平衡" in item for item in extraction.warnings),
        "资产负债表勾稽通过（无未平衡警告）",
        "；".join(extraction.warnings) or "无警告",
    )


def check_pdf_pipeline(checker: Checker) -> None:
    """Patch 4.2：在线无数据 → 自动回退本地 PDF → 完整 FinancialReport。"""
    banner("[6] 本地 PDF 兜底跑通 analyze_stock")
    from src.tools.financial_engine import analyze_stock

    try:
        report = analyze_stock("874886", lookback_years=4, force_refresh=True)
    except Exception as exc:  # noqa: BLE001
        checker.check(False, "874886 analyze_stock", f"{type(exc).__name__}: {exc}")
        return
    checker.check(report.meta.data_source.provider == "local_pdf_tables",
                  "数据来源标记为 local_pdf_tables", report.meta.data_source.provider)
    checker.check("PDF" in report.meta.data_source.upstream,
                  "溯源含 PDF 文件名", report.meta.data_source.upstream[:46])
    checker.check(
        all(str(v).startswith("local_pdf:") for v in report.meta.data_source.interfaces.values()),
        "三表均标注 local_pdf 接口",
        str(list(report.meta.data_source.interfaces.values())[:1]),
    )
    growth = report.growth_profitability.years[0]
    checker.check(
        abs((growth.total_operating_revenue or 0) - 7.7317) < 0.001,
        "营业总收入 7.7317 亿元（元→亿元换算正确）",
        f"{growth.total_operating_revenue}",
    )
    checker.check(
        abs((growth.deducted_parent_net_profit or 0) - 1.2246) < 0.001,
        "扣非归母 1.2246 亿元（摘要表补齐生效）",
        f"{growth.deducted_parent_net_profit}",
    )
    dupont = report.dupont.years[0]
    checker.check(
        dupont.identity_check_error is not None and dupont.identity_check_error < 1e-6,
        "杜邦恒等式误差 < 1e-6（PDF 数据可用）",
        str(dupont.identity_check_error),
    )
    checker.check(
        report.forensic.beneish.applicable and report.forensic.beneish.m_score is not None,
        "M-Score 可计算",
        str(report.forensic.beneish.m_score),
    )


def _write_transcript() -> Path:
    from src.config import REPORT_OUTPUT_DIR

    target = REPORT_OUTPUT_DIR / "market_compat_transcript.txt"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("\n".join(TRANSCRIPT), encoding="utf-8")
    return target


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Patch 4.1 市场兼容性验证")
    parser.add_argument("--online", action="store_true", help="追加联网实盘断言")
    args = parser.parse_args(argv)

    checker = Checker()
    banner("FinSight-Agent Patch 4.1 · 北交所 920 / 新三板兼容与友好降级")
    emit(f"  运行时间：{time.strftime('%Y-%m-%d %H:%M:%S')}")
    try:
        check_mapping(checker)
        check_error_translation(checker)
        check_peer_skip(checker)
        if args.online:
            check_online(checker)
            check_pdf_tables(checker)
            check_pdf_pipeline(checker)
        else:
            checker.skip("实盘断言（含 PDF 三大表）", "未传 --online")
    except Exception:  # noqa: BLE001
        checker.check(False, "测试脚本异常", traceback.format_exc(limit=3))

    banner("[6] 汇总")
    exit_code = checker.summary()
    _write_transcript()
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
