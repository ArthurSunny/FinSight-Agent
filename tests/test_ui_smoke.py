"""Phase 4 · Streamlit 投研工作台冒烟测试（无头）。

运行：:

    python tests/test_ui_smoke.py
    python tests/test_ui_smoke.py --keep-html      # 保留生成的 HTML 供人工查看

覆盖范围
--------
1. **模块可导入**：``src.ui.theme/charts/exporter/state/components`` 与 ``app`` 无语法/依赖问题；
2. **图表构建**：600519 实盘数据下 7 类 Plotly Figure 全部返回非空 trace；
   600036（金融股）走降级路径不抛错；
3. **HTML 导出**：自包含（含 ``@page`` / 内联 CSS / 六章节 / 表格）、体积合理、
   **不含任何 API Key**，且能落盘；
4. **AppTest 无头渲染**：首屏空状态 → 点击「载入该报告」→ 渲染 5 个标签页与 KPI；
   切换「实时深度解构」→ 表单控件齐备；
5. **附注检索器**：从缓存秒级加载 600519 PDF，检索「研发费用 / 存货跌价准备」返回命中；
6. **铁律 1 静态检查**：``src/ui/*.py`` 中不得出现财务运算（``/ 1e8``、``* 1e8``）。
"""

from __future__ import annotations

import argparse
import re
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

from src.config import REPORT_OUTPUT_DIR, ensure_directories  # noqa: E402
from src.tools.financial_engine import analyze_stock  # noqa: E402
from src.tools.peer_comparison import compare_peers  # noqa: E402
from src.ui import charts, exporter, state as ui_state, theme  # noqa: E402

TRANSCRIPT: list[str] = []
SECRET_PATTERN = re.compile(r"sk-[A-Za-z0-9]{16,}")


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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Phase 4 UI 冒烟测试")
    parser.add_argument("--keep-html", action="store_true", help="保留生成的 HTML 文件")
    parser.add_argument("--skip-app", action="store_true", help="跳过 AppTest 渲染（更快的纯函数测试）")
    return parser


# ---------------------------------------------------------------------------
# [2] 图表
# ---------------------------------------------------------------------------
def check_charts(checker: Checker, report, peer) -> None:
    banner("[2] Plotly 图表构建（只读 FinancialReport）")
    builders = (
        ("成长与盈利趋势", lambda: charts.growth_trend_figure(report)),
        ("杜邦 ROE 瀑布", lambda: charts.dupont_waterfall_figure(report)),
        ("现金流含金量", lambda: charts.cashflow_figure(report)),
        ("M-Score 八变量", lambda: charts.beneish_figure(report)),
        ("M-Score 雷达", lambda: charts.beneish_radar_figure(report)),
        ("M-Score 仪表", lambda: charts.mscore_gauge_figure(report)),
        ("同业对标", lambda: charts.peer_comparison_figure(peer)),
    )
    for name, builder in builders:
        try:
            figure = builder()
        except Exception as exc:  # noqa: BLE001
            checker.check(False, f"{name} 构建", f"{type(exc).__name__}: {exc}")
            continue
        traces = len(figure.data)
        checker.check(traces > 0, f"{name} 图 trace 非空", f"{traces} 条 trace")
        checker.check(
            figure.layout.height is not None, f"{name} 图高度已设置", f"{figure.layout.height}"
        )

    waterfall = charts.dupont_waterfall_figure(report)
    measures = list(waterfall.data[0].measure)
    checker.check(
        measures == ["absolute", "relative", "relative", "relative", "total"],
        "瀑布图 measure 结构正确（相对项可累加、末项为合计）",
        str(measures),
    )
    values = list(waterfall.data[0].y)
    effect_sum = sum(value for value, measure in zip(values, measures) if measure == "relative")
    latest_dupont = report.dupont.years[0]
    expected = (latest_dupont.roe_dupont - report.dupont.years[1].roe_dupont) * 100
    checker.check(
        abs(effect_sum - expected) < 0.01,
        "三因子贡献之和 = ROE 实际变动（容差 0.01pct）",
        f"{effect_sum:+.4f} vs {expected:+.4f}",
    )
    checker.check(
        len(charts.peer_table_rows(peer)) == len(peer.rows), "同业对标表行数与 PeerComparison 一致"
    )
    checker.check(
        len(charts.empty_figure("无数据").data) == 0, "空状态图不抛错（优雅降级）"
    )

    banner("[2b] 金融股降级路径（600036 招商银行）")
    try:
        bank = analyze_stock("600036", lookback_years=3)
    except Exception as exc:  # noqa: BLE001
        checker.skip("金融股降级", f"取数失败：{type(exc).__name__}")
        return
    checker.check(bank.meta.is_financial, "600036 被判定为金融企业", bank.meta.org_type)
    try:
        bank_beneish = charts.beneish_figure(bank)
        bank_growth = charts.growth_trend_figure(bank)
        bank_waterfall = charts.dupont_waterfall_figure(bank)
    except Exception as exc:  # noqa: BLE001
        checker.check(False, "金融股图表构建不抛错", f"{type(exc).__name__}: {exc}")
        return
    checker.check(len(bank_beneish.data) == 0, "金融股 M-Score 图返回空状态（不适用）")
    checker.check(len(bank_growth.data) > 0, "金融股成长图仍可渲染", f"{len(bank_growth.data)} 条")
    checker.check(
        len(bank_waterfall.data) > 0, "金融股杜邦瀑布仍可渲染", f"{len(bank_waterfall.data)} 条"
    )


# ---------------------------------------------------------------------------
# [3] HTML 导出
# ---------------------------------------------------------------------------
def check_exporter(checker: Checker, report, markdown: str, trace: Optional[dict], keep: bool) -> None:
    banner("[3] 自包含打印级 HTML 导出")
    t0 = time.perf_counter()
    bundle = exporter.build_export_bundle(report, markdown, trace)
    elapsed = time.perf_counter() - t0
    emit(f"  渲染耗时 {elapsed * 1000:.0f} ms｜HTML {len(bundle.html):,} 字符")

    html = bundle.html
    checker.check(html.startswith("<!DOCTYPE html>"), "输出为完整 HTML 文档")
    checker.check("@page" in html and "size: A4" in html, "包含 @media print A4 分页样式")
    checker.check("@media print" in html, "包含打印专用 CSS")
    checker.check("<style>" in html and "stylesheet" not in html, "CSS 完全内联（无外部样式表）")
    checker.check("http://" not in html and "https://" not in html, "无任何外部网络资源引用")
    checker.check(html.count("<table") >= 3, "报告表格已渲染", f"{html.count('<table')} 个表格")
    for ordinal in ("一、", "二、", "三、", "四、", "五、", "六、"):
        checker.check(f"{ordinal}" in html, f"第六章节点已渲染：{ordinal}")
    checker.check(
        report.meta.stock_name in html and report.meta.stock_code in html, "封面含标的名与代码"
    )
    checker.check("免责声明" in html, "页脚含免责声明")
    checker.check(
        not SECRET_PATTERN.search(html), "HTML 中不含任何 API Key（sk-**** 扫描）"
    )
    checker.check(len(html) > 12_000, "HTML 体积合理（>12KB）", f"{len(html) / 1024:.1f} KB")
    checker.check(
        len(bundle.markdown) > 1000 and json_loadable(bundle.trace_json),
        "Markdown 与 trace JSON 载荷均可下载",
    )

    target = REPORT_OUTPUT_DIR / bundle.html_filename
    exporter.save_html(html, target)
    checker.check(target.exists() and target.stat().st_size > 10_000, "HTML 落盘成功",
                  f"{target.name}（{target.stat().st_size / 1024:.1f} KB）")
    emit(f"  导出文件：{target}")
    if not keep:
        transcript = REPORT_OUTPUT_DIR / "ui_smoke_transcript.txt"
        emit(f"  提示：HTML 已保留在 {target.name}，可直接浏览器打开打印。{transcript.name if transcript else ''}")


def json_loadable(text: str) -> bool:
    import json

    try:
        json.loads(text)
        return True
    except ValueError:
        return False


# ---------------------------------------------------------------------------
# [4] AppTest 无头渲染
# ---------------------------------------------------------------------------
def check_apptest(checker: Checker) -> None:
    banner("[4] Streamlit AppTest 无头渲染")
    from streamlit.testing.v1 import AppTest

    t0 = time.perf_counter()
    at = AppTest.from_file(str(PROJECT_ROOT / "app.py"), default_timeout=300)
    at.run()
    checker.check(not list(at.exception), "首屏渲染无异常", f"{(time.perf_counter() - t0) * 1000:.0f} ms")
    checker.check(len(at.radio) == 1, "侧边栏模式切换存在")
    checker.check(
        at.radio[0].value == "历史实盘回放", "默认为历史回放模式（演示友好）", at.radio[0].value
    )

    runs = ui_state.scan_archived_runs()
    if not runs:
        checker.skip("历史回放渲染", "data/reports 下无历史存档")
        return

    load_buttons = [button for button in at.button if "载入" in button.label]
    checker.check(bool(load_buttons), "存在「载入该报告」按钮")
    if not load_buttons:
        return

    t0 = time.perf_counter()
    load_buttons[0].click().run()
    render_s = time.perf_counter() - t0
    checker.check(not list(at.exception), "点击载入后渲染无异常", f"{render_s:.1f}s")
    checker.check(len(at.tabs) == 5, "渲染 5 个专业标签页", f"{len(at.tabs)} 个")
    checker.check(render_s < 60, "历史回放耗时 < 60s（含报告重建）", f"{render_s:.1f}s")

    all_text = " ".join(str(item.value) for item in at.markdown)
    expected_metrics = (
        "营业总收入",
        "归母净利润",
        "杜邦 ROE",
        "净现比",
        "Beneish M-Score",
        "综合风险等级",
    )
    missing = [label for label in expected_metrics if label not in all_text]
    checker.check(not missing, "高管级 KPI 条指标齐备（HTML 渲染）", f"缺失 {missing}" if missing else "6 项")
    checker.check(
        any("下载打印级 HTML" == button.label for button in at.download_button),
        "报告下载按钮齐备（HTML/MD/JSON）",
        f"{len(at.download_button)} 个下载按钮",
    )
    checker.check(
        len(at.get("plotly_chart")) >= 5, "图表区块已渲染", f"{len(at.get('plotly_chart'))} 个"
    )
    checker.check("FinSight-Agent" in all_text, "品牌标题已渲染")
    checker.check("防幻觉质检" in all_text, "防幻觉质检徽章已渲染")
    checker.check(
        not SECRET_PATTERN.search(all_text), "页面文本不含 API Key"
    )
    errors = [str(item.value) for item in at.error]
    checker.check(not errors, "页面无错误提示（st.error 为空）", "；".join(errors)[:120])

    at.radio[0].set_value("实时深度解构").run()
    checker.check(not list(at.exception), "切换实时模式无异常")
    checker.check(any("股票代码" == item.label for item in at.text_input), "实时模式含代码输入框")
    checker.check(
        any("开始实时解构" in button.label for button in at.button), "实时模式含运行按钮"
    )


# ---------------------------------------------------------------------------
# [5] 附注检索器
# ---------------------------------------------------------------------------
def check_retriever(checker: Checker) -> None:
    banner("[5] 附注即时检索（Tab 4 的检索引擎）")
    pdfs = ui_state.list_local_pdfs("600519")
    if not pdfs:
        checker.skip("附注检索器", "data/pdfs 下无 600519 年报")
        return
    path = pdfs[0]
    period = ui_state.infer_pdf_period(path)
    t0 = time.perf_counter()
    retriever, document = ui_state.load_retriever(path, period)
    load_s = time.perf_counter() - t0
    checker.check(document.page_count > 50, "PDF 解析成功", f"{document.page_count} 页")
    checker.check(load_s < 90, "检索器加载 < 90s（冷解析上限）", f"{load_s:.1f}s")

    cases = (
        ("研发费用", "研发费用", ("研发",)),
        ("存货跌价准备", "存货", ("存货",)),
        ("前五名客户", "前五名客户", ("客户", "前五名")),
    )
    for query, account, expected in cases:
        outcome = retriever.search(query, target_account=account, top_k=5)
        hits = outcome.hits
        text = " ".join(f"{hit.section_title} {hit.section_path}" for hit in hits)
        checker.check(bool(hits), f"检索「{query}」命中", f"{len(hits)} 条 / {outcome.stats.elapsed_s * 1000:.0f} ms")
        checker.check(
            any(token in text for token in expected),
            f"检索「{query}」命中相关章节",
            hits[0].page_label if hits else "—",
        )
        checker.check(
            all(re.fullmatch(r"P\.\d+(-\d+)?", hit.page_label) for hit in hits),
            f"检索「{query}」页码格式合法",
            "、".join(hit.page_label for hit in hits[:3]),
        )


# ---------------------------------------------------------------------------
# [6] 铁律 1 静态检查
# ---------------------------------------------------------------------------
def check_no_financial_math(checker: Checker) -> None:
    banner("[6] 铁律 1 静态检查（UI 层不得重算财务指标）")
    forbidden = re.compile(r"(/\s*1e8\*?\*?|\*\s*1e8)")
    ui_dir = PROJECT_ROOT / "src" / "ui"
    offenders: list[str] = []
    for source in sorted(ui_dir.glob("*.py")):
        text = source.read_text(encoding="utf-8")
        for match in forbidden.finditer(text):
            line_no = text[: match.start()].count("\n") + 1
            offenders.append(f"{source.name}:{line_no}")
    checker.check(not offenders, "src/ui/*.py 无 1e8 单位换算（金额换算只允许在 tools 层）",
                  "、".join(offenders))

    app_text = (PROJECT_ROOT / "app.py").read_text(encoding="utf-8")
    checker.check(
        not forbidden.search(app_text), "app.py 无 1e8 单位换算"
    )
    checker.check(
        "analyze_stock" not in app_text,
        "app.py 不直接调用 analyze_stock（统一走 src.ui.state）",
    )


def _write_transcript() -> Path:
    target = REPORT_OUTPUT_DIR / "ui_smoke_transcript.txt"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("\n".join(TRANSCRIPT), encoding="utf-8")
    return target


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    ensure_directories()
    checker = Checker()
    started = time.perf_counter()

    banner("FinSight-Agent Phase 4 · Streamlit 投研工作台冒烟测试")
    emit(f"  运行时间：{time.strftime('%Y-%m-%d %H:%M:%S')}")

    banner("[1] 模块导入")
    try:
        import src.ui.components  # noqa: F401
        import src.ui.theme  # noqa: F401

        checker.check(True, "src.ui 全套模块可导入")
    except Exception as exc:  # noqa: BLE001
        checker.check(False, "src.ui 模块导入", f"{type(exc).__name__}: {exc}")
        return checker.summary()

    checker.check(len(theme.GLOBAL_CSS) > 2000, "全局 CSS 已定义", f"{len(theme.GLOBAL_CSS)} 字符")
    checker.check(
        theme.risk_badge("high")[1] == theme.RED, "风险配色映射正确", str(theme.risk_badge("high"))
    )

    banner("[1b] 载入实盘数据")
    try:
        report = analyze_stock("600519", lookback_years=4)
        peer = compare_peers("600519", ["000858", "000568"], lookback_years=3)
    except Exception as exc:  # noqa: BLE001
        checker.check(False, "600519 实盘数据载入", f"{type(exc).__name__}: {exc}")
        return checker.summary()
    checker.check(report.meta.stock_code == "600519", "报告标的正确")
    checker.check(peer is not None and peer.available, "同业对标可用", f"{len(peer.rows)} 家公司")

    markdown_path = REPORT_OUTPUT_DIR / "600519_2025年报_tearsheet.md"
    markdown = ui_state.load_markdown_text(markdown_path) if markdown_path.exists() else ""
    trace_path = REPORT_OUTPUT_DIR / "600519_2025年报_agent_trace.json"
    trace = ui_state.load_trace(trace_path) if trace_path.exists() else None
    checker.check(len(markdown) > 2000, "Markdown 纪要已加载", f"{len(markdown)} 字符")
    checker.check(trace is not None and "usage" in (trace or {}), "运行轨迹已加载")

    check_charts(checker, report, peer)
    check_exporter(checker, report, markdown, trace, keep=args.keep_html)
    if args.skip_app:
        checker.skip("AppTest 无头渲染", "--skip-app")
    else:
        try:
            check_apptest(checker)
        except Exception:  # noqa: BLE001
            checker.check(False, "AppTest 渲染", traceback.format_exc(limit=3))
    check_retriever(checker)
    check_no_financial_math(checker)

    banner("[7] 汇总")
    total = time.perf_counter() - started
    emit(f"  总耗时：{total:.1f}s")
    exit_code = checker.summary()
    _write_transcript()
    emit()
    emit("  >>> 全部断言通过：Phase 4 前端口径验证成功。" if exit_code == 0
         else "  >>> 存在失败断言，请修复后重跑。")
    _write_transcript()
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
