"""Phase 4：自包含打印级 HTML 投研报告导出器。

为什么不用 weasyprint / wkhtmltopdf
----------------------------------
这两者都依赖外部二进制（GTK / Qt DLL），在 Windows 上极易出现"DLL load failed"，
对开源项目的"开箱即用"是灾难。本模块改用**纯 Python**：

    Markdown ──(markdown 库)──> HTML 片段 ──(内联 CSS)──> 单个 .html 文件

用户在浏览器里直接「Ctrl+P → 另存为 PDF」即可得到排版良好的 A4 报告，
零外部依赖、零网络请求（内联 CSS 与内联 SVG/表格，无 CDN）。

格式一致性
----------
所有百分比/金额字符串复用 :mod:`src.agents.context_formatter` 的 ``fmt_*`` 函数，
保证「事实包 / Markdown 纪要 / HTML 报告 / Streamlit 页面」四处的显示完全一致，
不会出现"报告里 91.18%，页面上 91.2%"这类不一致。
"""

from __future__ import annotations

import datetime as dt
import html as html_lib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from src.agents.context_formatter import (
    fmt_amount,
    fmt_pct,
    fmt_pct_point,
    fmt_ratio,
    fmt_score,
)
from src.tools.schemas import FinancialReport

#: 导出文件名后缀
HTML_SUFFIX = ".html"
MARKDOWN_SUFFIX = ".md"
TRACE_SUFFIX = "_agent_trace.json"


# ---------------------------------------------------------------------------
# 导出包
# ---------------------------------------------------------------------------
@dataclass
class ExportBundle:
    """一次导出的全部载荷（供 Streamlit 的下载按钮直接使用）。"""

    filename_stem: str
    html: str
    markdown: str
    trace_json: str

    @property
    def html_filename(self) -> str:
        return f"{self.filename_stem}{HTML_SUFFIX}"

    @property
    def markdown_filename(self) -> str:
        return f"{self.filename_stem}{MARKDOWN_SUFFIX}"

    @property
    def trace_filename(self) -> str:
        return f"{self.filename_stem}{TRACE_SUFFIX}"

    def to_bytes(self, kind: str) -> bytes:
        payload = {"html": self.html, "markdown": self.markdown, "trace": self.trace_json}[kind]
        return payload.encode("utf-8")


# ---------------------------------------------------------------------------
# Markdown → HTML
# ---------------------------------------------------------------------------
def markdown_to_html(markdown_text: str) -> str:
    """Markdown → HTML 片段（启用表格 / 围栏代码 / 有序列表扩展）。"""
    import markdown as md_lib

    return md_lib.markdown(
        markdown_text or "",
        extensions=["tables", "fenced_code", "sane_lists", "nl2br"],
        output_format="html5",
    )


def _strip_report_title(markdown_text: str) -> str:
    """去掉报告首行的 H1（HTML 模板已有封面标题，避免重复）。"""
    lines = (markdown_text or "").lstrip().splitlines()
    if lines and lines[0].startswith("# "):
        lines = lines[1:]
    return "\n".join(lines).lstrip()


# ---------------------------------------------------------------------------
# 样式（内联，打印优先）
# ---------------------------------------------------------------------------
_PRINT_CSS = """
@page { size: A4; margin: 15mm 13mm 16mm 13mm; }

:root {
  --navy: #1F3A5F; --slate: #5B6B7B; --gold: #C9A227; --red: #A93226;
  --green: #1E6F5C; --ink: #1B2430; --muted: #7A8794; --border: #D8E0E8;
}

* { box-sizing: border-box; }

body {
  margin: 0; padding: 0 0 40px 0; background: #E9EDF1;
  font-family: "Microsoft YaHei", "PingFang SC", "Hiragino Sans GB", "Source Han Sans SC", sans-serif;
  color: var(--ink); font-size: 10.5pt; line-height: 1.75;
  -webkit-font-smoothing: antialiased;
}

.page {
  width: 210mm; min-height: 297mm; margin: 18px auto; background: #fff;
  box-shadow: 0 2px 14px rgba(27, 36, 48, .16); padding: 15mm 13mm 18mm 13mm;
}

/* ---------- 封面头 ---------- */
.cover { border-bottom: 3px solid var(--navy); padding-bottom: 10px; margin-bottom: 16px; }
.brand {
  font-size: 8.6pt; letter-spacing: .16em; color: var(--muted);
  text-transform: uppercase; margin-bottom: 6px;
}
.cover h1 { font-size: 19pt; color: var(--navy); margin: 0 0 6px 0; letter-spacing: .01em; }
.cover .subtitle { font-size: 10.5pt; color: var(--slate); margin-bottom: 8px; }
.cover .meta { font-size: 8.4pt; color: var(--muted); line-height: 1.7; }

/* ---------- KPI 条 ---------- */
.kpis { display: flex; flex-wrap: wrap; gap: 6px; margin: 12px 0 4px 0; }
.kpi {
  flex: 1 1 108px; border: 1px solid var(--border); border-top: 3px solid var(--navy);
  padding: 6px 8px; background: #FCFDFE;
}
.kpi.risk-high { border-top-color: var(--red); }
.kpi.risk-medium { border-top-color: var(--gold); }
.kpi.risk-low { border-top-color: var(--green); }
.kpi .k-label { font-size: 7.8pt; color: var(--muted); letter-spacing: .04em; }
.kpi .k-value { font-size: 12.5pt; font-weight: 700; color: var(--ink); }
.kpi .k-sub { font-size: 7.8pt; color: var(--slate); }
.up { color: var(--green); } .down { color: var(--red); } .flat { color: var(--muted); }

/* ---------- 正文 ---------- */
.report h2 {
  font-size: 13pt; color: var(--navy); margin: 20px 0 8px 0; padding-left: 8px;
  border-left: 4px solid var(--gold); break-after: avoid;
}
.report h3 { font-size: 11pt; color: var(--slate); margin: 14px 0 5px 0; break-after: avoid; }
.report h4 { font-size: 10.5pt; color: var(--slate); margin: 10px 0 4px 0; }
.report p { margin: 6px 0; text-align: justify; }
.report ul, .report ol { margin: 6px 0 6px 0; padding-left: 20px; }
.report li { margin: 2px 0; }
.report strong { color: #12202F; }
.report blockquote {
  margin: 8px 0; padding: 7px 12px; background: #FDF8EC;
  border-left: 3px solid var(--gold); color: #4A5765; font-size: 9.6pt;
}
.report code {
  background: #EFF3F7; padding: 1px 4px; border-radius: 3px;
  font-family: Consolas, "Courier New", monospace; font-size: 9pt;
}
.report pre { background: #F4F7FA; padding: 8px 10px; overflow-x: auto; border-radius: 3px; }

/* ---------- 表格 ---------- */
.report table, table.data {
  border-collapse: collapse; width: 100%; margin: 10px 0 14px 0; font-size: 9.1pt;
  break-inside: auto;
}
.report th, .report td, table.data th, table.data td {
  border: 1px solid var(--border); padding: 4px 7px; vertical-align: top;
}
.report th, table.data th { background: #EDF1F5; color: var(--navy); font-weight: 600; text-align: left; }
.report tr:nth-child(even) td { background: #FAFBFC; }
.report tr { break-inside: avoid; }

/* ---------- 页脚 ---------- */
.footer {
  margin-top: 22px; padding-top: 8px; border-top: 1px solid var(--border);
  font-size: 7.9pt; color: var(--muted); line-height: 1.65;
}

/* ---------- 打印 ---------- */
@media print {
  body { background: #fff; font-size: 10pt; }
  .page { width: auto; min-height: 0; margin: 0; padding: 0; box-shadow: none; }
  .no-print { display: none !important; }
  .report h2 { break-after: avoid-page; }
  .report table { page-break-inside: auto; }
  .report tr, .kpi { page-break-inside: avoid; }
  a { color: inherit; text-decoration: none; }
}

/* ---------- 屏幕上的打印提示 ---------- */
.print-hint {
  max-width: 210mm; margin: 14px auto -6px auto; padding: 8px 12px;
  background: #fff; border: 1px solid var(--border); border-left: 3px solid var(--navy);
  font-size: 9pt; color: var(--slate);
}
"""


# ---------------------------------------------------------------------------
# KPI 构建
# ---------------------------------------------------------------------------
def _kpi(label: str, value: str, sub: str = "", css_class: str = "", sub_class: str = "flat") -> str:
    cls = f"kpi {css_class}".strip()
    sub_html = f'<div class="k-sub {sub_class}">{html_lib.escape(sub)}</div>' if sub else ""
    return (
        f'<div class="{cls}"><div class="k-label">{html_lib.escape(label)}</div>'
        f'<div class="k-value">{html_lib.escape(value)}</div>{sub_html}</div>'
    )


def _trend_class(value: Optional[float]) -> str:
    if value is None:
        return "flat"
    return "up" if value > 0 else ("down" if value < 0 else "flat")


def build_kpi_cells(report: FinancialReport) -> list[tuple[str, str, str, str, str]]:
    """返回 KPI 元组列表：(label, value, sub, card_class, sub_class)。"""
    meta = report.meta
    latest_period = meta.latest_report_period
    growth = next(
        (row for row in report.growth_profitability.years if row.report_period == latest_period),
        None,
    )
    dupont = next(
        (row for row in report.dupont.years if row.report_period == latest_period), None
    )
    cash = next(
        (row for row in report.cashflow_quality.years if row.report_period == latest_period), None
    )
    risk_level = report.forensic.risk_level
    risk_label = {"high": "高风险", "medium": "中风险", "low": "低风险"}.get(risk_level, "待判定")

    cells: list[tuple[str, str, str, str, str]] = [
        ("营业总收入", fmt_amount(growth.total_operating_revenue if growth else None),
         f"YoY {fmt_pct(growth.total_operating_revenue_yoy if growth else None, signed=True)}",
         "", _trend_class(growth.total_operating_revenue_yoy if growth else None)),
        ("归母净利润", fmt_amount(growth.parent_net_profit if growth else None),
         f"YoY {fmt_pct(growth.parent_net_profit_yoy if growth else None, signed=True)}",
         "", _trend_class(growth.parent_net_profit_yoy if growth else None)),
        ("杜邦 ROE", fmt_pct(dupont.roe_dupont if dupont else None),
         f"Δ {fmt_pct_point(dupont.roe_delta if dupont else None)}",
         "", "flat"),
        ("净现比", fmt_ratio(cash.net_cash_ratio if cash else None),
         f"FCF {fmt_amount(cash.free_cash_flow if cash else None)}", "", "flat"),
        ("Beneish M-Score",
         fmt_score(report.forensic.beneish.m_score),
         "触发嫌疑" if report.forensic.beneish.is_high_risk else "未触发嫌疑",
         "", "down" if report.forensic.beneish.is_high_risk else "up"),
        ("综合风险等级", risk_label,
         f"触发 {report.forensic.triggered_count} 条规则", f"risk-{risk_level}", "flat"),
    ]
    return cells


# ---------------------------------------------------------------------------
# 主渲染
# ---------------------------------------------------------------------------
def render_report_html(
    report: FinancialReport,
    markdown_text: str,
    *,
    trace: Optional[dict[str, Any]] = None,
    generated_at: Optional[str] = None,
    include_print_hint: bool = True,
) -> str:
    """渲染自包含 HTML（内联 CSS + 封面 + KPI + 正文 + 页脚）。"""
    meta = report.meta
    period = meta.latest_report_period
    generated_at = generated_at or dt.datetime.now().strftime("%Y-%m-%d %H:%M")

    rag_meta = (trace or {}).get("rag_meta") or {}
    evidences = ((trace or {}).get("rag_evidences") or {}).get("evidences") or []
    rag_verified = sum(1 for item in evidences if item.get("verdict") != "inconclusive")
    fact_stats = ((trace or {}).get("fact_check") or {}).get("stats") or {}

    # ---- 溯源行 ----
    datasource = meta.data_source
    interfaces = "、".join(
        f"{name}={iface}" for name, iface in list(datasource.interfaces.items())[:3]
    )
    provenance_lines = [
        f"标的：{meta.stock_code} {meta.stock_name}（{meta.market_prefix}）｜企业类型：{meta.org_type}"
        f"｜最新报告期：{period}",
        f"数据源：{datasource.provider} / {datasource.upstream}｜截止日 {datasource.as_of_date}",
        f"接口：{interfaces or '—'}",
        f"质检：锚点 {fact_stats.get('anchors_ok', '—')}/{fact_stats.get('anchors', '—')} 通过"
        f"｜章节 {fact_stats.get('sections_ok', '—')}/6｜附注实证 {len(evidences)} 条（已证实 {rag_verified}）"
        + (f"｜PDF {rag_meta.get('pdf_period')}（{rag_meta.get('page_count')} 页）"
           if rag_meta.get("available") else "｜未启用附注穿透"),
        f"引擎：v{meta.engine_version}｜生成时间：{generated_at}",
    ]

    kpi_html = "".join(
        _kpi(label, value, sub, card_class, sub_class)
        for label, value, sub, card_class, sub_class in build_kpi_cells(report)
    )

    body_html = markdown_to_html(_strip_report_title(markdown_text))
    hint_html = (
        '<div class="print-hint no-print">本文件为自包含 HTML（无外部依赖）。'
        "在浏览器中按 <b>Ctrl+P</b>（macOS: <b>Cmd+P</b>）即可打印或另存为 A4 PDF；"
        "页边距与分页已针对打印优化。</div>"
        if include_print_hint
        else ""
    )

    disclaimer = (
        "免责声明：本报告由 FinSight-Agent 自动生成，所有财务数值均由 Python 引擎确定性计算，"
        "并由确定性规则完成防幻觉质检；大模型仅负责解读与组织语言。"
        "报告内容基于公开定期报告，不构成任何投资建议、评级或收益预测。"
        "数据准确性以交易所披露的定期报告为准。"
    )

    title = f"{meta.stock_name}（{meta.stock_code}）{period} 财务体检与排雷纪要"
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html_lib.escape(title)}</title>
<style>{_PRINT_CSS}</style>
</head>
<body>
{hint_html}
<div class="page">
  <header class="cover">
    <div class="brand">FinSight-Agent · 深度财务体检与排雷纪要 · Tear Sheet</div>
    <h1>{html_lib.escape(meta.stock_name)}（{html_lib.escape(meta.stock_code)}）</h1>
    <div class="subtitle">{html_lib.escape(period)} · 多智能体协同投研 · 年报附注穿透取证</div>
    <div class="meta">{"<br>".join(html_lib.escape(line) for line in provenance_lines)}</div>
    <div class="kpis">{kpi_html}</div>
  </header>
  <main class="report">
{body_html}
  </main>
  <footer class="footer">{html_lib.escape(disclaimer)}</footer>
</div>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# 导出入口
# ---------------------------------------------------------------------------
def build_export_bundle(
    report: FinancialReport,
    markdown_text: str,
    trace: Optional[dict[str, Any]] = None,
) -> ExportBundle:
    """组装 HTML / Markdown / Trace 三份下载载荷。"""
    stem = f"{report.meta.stock_code}_{report.meta.latest_report_period}_report"
    trace_json = json.dumps(trace or {}, ensure_ascii=False, indent=2, allow_nan=False)
    return ExportBundle(
        filename_stem=stem,
        html=render_report_html(report, markdown_text, trace=trace),
        markdown=markdown_text,
        trace_json=trace_json,
    )


def save_html(html_text: str, path: Path | str) -> Path:
    """把 HTML 写入磁盘（UTF-8）。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(html_text, encoding="utf-8")
    return target


def export_report(
    report: FinancialReport,
    markdown_text: str,
    *,
    trace: Optional[dict[str, Any]] = None,
    output_dir: Optional[Path | str] = None,
) -> Path:
    """一步导出 HTML 到 ``data/reports/`` 并返回路径。"""
    from src.config import REPORT_OUTPUT_DIR

    directory = Path(output_dir) if output_dir is not None else REPORT_OUTPUT_DIR
    bundle = build_export_bundle(report, markdown_text, trace)
    return save_html(bundle.html, directory / bundle.html_filename)
