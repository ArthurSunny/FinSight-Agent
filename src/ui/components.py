"""Phase 4：Streamlit 展示组件（KPI 条 / 规则卡片 / 附注证据 / 可观测性）。

本模块只把**已算好的数据**渲染成界面，不做任何财务计算（铁律 1）。
所有数值格式化统一走 :mod:`src.agents.context_formatter` 的 ``fmt_*``，
保证页面、Markdown 纪要、HTML 报告三处显示完全一致。
"""

from __future__ import annotations

import html as html_lib
from typing import Any, Optional, Sequence

import streamlit as st

from src.tools.schemas import FinancialReport
from src.ui.exporter import build_kpi_cells, markdown_to_html
from src.ui.state import fact_check_rows, token_breakdown
from src.ui.theme import GOLD, GREEN, MUTED, NAVY, RED, risk_badge, verdict_badge

_RULE_STATUS = {
    ("high", True): ("已触发·高", RED),
    ("medium", True): ("已触发·中", GOLD),
    ("info", True): ("已触发·提示", MUTED),
    ("info", False): ("未触发", GREEN),
}


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------
def _badge(text: str, color: str, *, solid: bool = True) -> str:
    cls = "fs-badge fs-badge-solid" if solid else "fs-badge fs-badge-outline"
    style = f"background:{color};" if solid else f"color:{color};"
    return f'<span class="{cls}" style="{style}">{html_lib.escape(text)}</span>'


def _dataframe(data: Any, *, height: Optional[int] = None) -> None:
    """兼容不同 Streamlit 版本的宽表格渲染。"""
    kwargs: dict[str, Any] = {"hide_index": True}
    if height is not None:
        kwargs["height"] = height
    try:
        st.dataframe(data, width="stretch", **kwargs)
    except TypeError:  # 旧版本回退
        st.dataframe(data, use_container_width=True, **kwargs)


def _fmt_evidence(key: str, value: Any) -> str:
    """把 ``RuleFlag.evidence`` 的值渲染成可读文本（含单位猜测）。"""
    if value is None:
        return "缺失"
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, dict):
        inner = "；".join(f"{k}={_fmt_evidence(k, v)}" for k, v in value.items())
        return inner or "无"
    if isinstance(value, (int, float)):
        percent_keys = ("ratio", "rate", "to_assets", "growth", "reliance", "receivables_to_revenue")
        lowered = key.lower()
        if any(token in lowered for token in percent_keys) and abs(value) <= 5:
            return f"{value * 100:.2f}%"
        return f"{value:,.4f}"
    return str(value)


# ---------------------------------------------------------------------------
# 品牌与 KPI
# ---------------------------------------------------------------------------
def render_brand(subtitle: str) -> None:
    st.markdown(
        f"""<div class="fs-brand">
          <span class="fs-brand-title">FinSight-Agent</span>
          <span class="fs-brand-sub">{html_lib.escape(subtitle)}</span>
        </div>""",
        unsafe_allow_html=True,
    )


def render_kpi_strip(report: FinancialReport, trace: Optional[dict[str, Any]] = None) -> None:
    """高管级核心指标条 + 防幻觉质检徽章。"""
    cells = build_kpi_cells(report)
    cards: list[str] = []
    for label, value, sub, card_class, sub_class in cells:
        cards.append(
            f"""<div class="fs-kpi {card_class}">
                  <div class="fs-kpi-label">{html_lib.escape(label)}</div>
                  <div class="fs-kpi-value">{html_lib.escape(value)}</div>
                  <div class="fs-kpi-sub {sub_class}">{html_lib.escape(sub)}</div>
                </div>"""
        )
    st.markdown(f'<div class="fs-kpi-grid">{"".join(cards)}</div>', unsafe_allow_html=True)
    render_quality_badge(report, trace)


def render_quality_badge(report: FinancialReport, trace: Optional[dict[str, Any]]) -> None:
    """防幻觉质检徽章：锚点 / 章节 / PDF 实证页码 / 重写次数。"""
    fact = (trace or {}).get("fact_check") or {}
    stats = fact.get("stats") or {}
    evidences = ((trace or {}).get("rag_evidences") or {}).get("evidences") or []
    pages = {
        citation.get("page")
        for item in evidences
        for citation in item.get("citations") or []
        if citation.get("verified")
    }
    retries = int((trace or {}).get("retry_count", 0) or 0)

    anchors_ok = stats.get("anchors_ok")
    anchors = stats.get("anchors")
    sections_ok = stats.get("sections_ok")
    passed = fact.get("passed")

    parts = [
        f"锚点 {anchors_ok}/{anchors} 通过" if anchors else "锚点校验未记录",
        f"章节 {sections_ok}/6",
        f"PDF 实证页码 {len(pages)} 处",
        f"重写 {retries} 次",
    ]
    color = GREEN if passed else GOLD
    label = "防幻觉质检通过" if passed else "质检已放行（需人工复核）"
    chips = "".join(
        f'<span class="fs-badge fs-badge-outline" style="color:{MUTED};margin-left:.35rem">'
        f"{html_lib.escape(item)}</span>"
        for item in parts
    )
    st.markdown(
        f'<div style="margin:-.55rem 0 1rem 0;font-size:.8rem;color:{MUTED}">'
        f"{_badge(label, color)}"
        f'<span style="margin-left:.4rem;color:{MUTED}">核心数值与附注页码均经确定性校验</span>'
        f"{chips}</div>",
        unsafe_allow_html=True,
    )


def render_financial_notice(report: FinancialReport) -> None:
    """金融类企业的会计准则差异说明（优雅降级，不报错）。"""
    if not report.meta.is_financial:
        return
    reason = report.meta.financial_reason or "数据源判定为非工商企业口径"
    st.markdown(
        f"""<div class="fs-panel warn">
          <div class="fs-panel-title">金融企业会计准则差异说明</div>
          本标的被判定为<b>金融类企业</b>（{html_lib.escape(report.meta.org_type)}）：{html_lib.escape(reason)}<br>
          因此以下指标<b>不适用</b>并返回 null，属预期行为而非数据缺失：
          毛利率、存货/应收/应付周转率、Beneish M-Score（其输入依赖存货与营业成本）。
          排雷改用监管口径（不良率、拨备覆盖率、流动性覆盖率）更合适，本项目仅作结构展示。
        </div>""",
        unsafe_allow_html=True,
    )


def render_report_markdown(markdown_text: str) -> None:
    """把六章节 Markdown 渲染为带专业样式的 HTML 块。"""
    if not markdown_text.strip():
        st.info("尚无报告正文。")
        return
    lines = markdown_text.lstrip().splitlines()
    if lines and lines[0].startswith("# "):
        lines = lines[1:]
    body = markdown_to_html("\n".join(lines))
    st.markdown(f'<div class="fs-report">{body}</div>', unsafe_allow_html=True)


# ---------------------------------------------------------------------------
# Tab 3：排雷规则卡片
# ---------------------------------------------------------------------------
def render_rule_cards(report: FinancialReport) -> None:
    flags = report.forensic.rule_flags
    if not flags:
        st.info("无规则检验结果。")
        return

    for flag in flags:
        label, color = _RULE_STATUS.get((flag.severity, flag.triggered), ("未触发", GREEN))
        if not flag.evaluated:
            label, color = "不适用", MUTED
        header = (
            f"{_badge(label, color)}&nbsp;&nbsp;"
            f"<span style='font-weight:700;color:{NAVY};font-size:.95rem'>{html_lib.escape(flag.name)}</span>"
            f"<span style='color:{MUTED};font-size:.75rem;margin-left:.4rem'>{flag.code}</span>"
        )
        st.markdown(f"<div style='margin:.35rem 0 .1rem 0'>{header}</div>", unsafe_allow_html=True)
        st.markdown(
            f"<div style='font-size:.86rem;color:#33414F;margin-bottom:.3rem'>"
            f"{html_lib.escape(flag.summary)}</div>",
            unsafe_allow_html=True,
        )
        with st.expander("展开证据明细", expanded=flag.triggered and flag.severity == "high"):
            if flag.detail:
                for line in flag.detail:
                    st.markdown(f"- {line}")
            if flag.evidence:
                rows = [
                    {"项目": key, "数值": _fmt_evidence(key, value)}
                    for key, value in flag.evidence.items()
                    if not isinstance(value, dict)
                ]
                if rows:
                    _dataframe(rows)
                for key, value in flag.evidence.items():
                    if isinstance(value, dict):
                        st.caption(f"{key} 明细")
                        _dataframe(
                            [{"项目": k, "数值": _fmt_evidence(k, v)} for k, v in value.items()]
                        )
            if flag.interpretation_hint:
                st.markdown(f"**追查方向**：{flag.interpretation_hint}")
            if flag.skip_reason:
                st.markdown(f"**跳过原因**：{flag.skip_reason}")
            if flag.thresholds:
                st.caption(
                    "适用阈值："
                    + "；".join(f"{key}={value:g}" for key, value in flag.thresholds.items())
                )


# ---------------------------------------------------------------------------
# Tab 4：附注证据
# ---------------------------------------------------------------------------
def render_pdf_meta(rag_meta: Optional[dict[str, Any]]) -> None:
    meta = rag_meta or {}
    if not meta.get("available"):
        st.markdown(
            f"""<div class="fs-panel warn">
              <div class="fs-panel-title">本次未执行附注穿透</div>
              {html_lib.escape(str(meta.get("reason") or "未启用 RAG 或未获取到年报 PDF"))}
            </div>""",
            unsafe_allow_html=True,
        )
        return

    aligned = not meta.get("period_mismatch_note")
    cols = st.columns(6)
    items = (
        ("PDF 报告期", str(meta.get("pdf_period", "—"))),
        ("总页数", f"{meta.get('page_count', 0)} 页"),
        ("切片总数", f"{meta.get('chunks', 0)}"),
        ("核心附注切片", f"{meta.get('note_chunks', 0)}"),
        ("含表格切片", f"{meta.get('chunks_with_table', 0)}"),
        ("期次对齐", "已对齐" if aligned else "存在差异"),
    )
    for column, (label, value) in zip(cols, items):
        column.metric(label, value)

    source_note = (
        f"来源：{meta.get('pdf_source', '—')}｜解析耗时 {meta.get('parse_elapsed_s', '—')}s"
        f"{'（命中缓存）' if meta.get('parse_cache_hit') else '（冷解析）'}"
        f"｜检索 {meta.get('retrieval_ms', 0)} ms"
    )
    st.caption(source_note)
    if meta.get("period_mismatch_note"):
        st.warning(f"期次差异提示：{meta['period_mismatch_note']}")
    if meta.get("pdf_path"):
        st.caption(f"文件：{meta['pdf_path']}")


def render_evidence_cards(evidences: Sequence[dict[str, Any]]) -> None:
    if not evidences:
        st.info("本次没有附注核查结论。")
        return

    for item in evidences:
        label, color = verdict_badge(item.get("verdict"))
        account = html_lib.escape(str(item.get("target_account", "—")))
        summary = html_lib.escape(str(item.get("finding_summary", "")))
        citations = item.get("citations") or []
        pages = [
            citation.get("page", "") for citation in citations if citation.get("verified")
        ]
        page_text = "、".join(dict.fromkeys(pages)) or "无（未通过页码校验）"
        st.markdown(
            f"""<div class="fs-evidence">
                  <div class="fs-evidence-head">
                    <span class="fs-evidence-account">{account}</span>
                    {_badge(label, color)}
                    <span style="color:{MUTED};font-size:.76rem">页码：{html_lib.escape(page_text)}</span>
                  </div>
                  <div class="fs-evidence-body">{summary}</div>
                </div>""",
            unsafe_allow_html=True,
        )
        if citations:
            with st.expander(f"查看 {account} 的原文摘录（{len(citations)} 条）"):
                for citation in citations:
                    verified = citation.get("verified")
                    flag = "已核实" if verified else "未通过页码校验"
                    st.markdown(
                        f"**{citation.get('page', '—')}**｜{citation.get('pdf_period', '')}"
                        f"｜<span style='color:{GREEN if verified else RED}'>{flag}</span>",
                        unsafe_allow_html=True,
                    )
                    st.code(str(citation.get("quote", "")) or "（无摘录）", language="text")


def render_retriever_hits(hits: Sequence[Any], query: str) -> None:
    """即时检索结果（页码 + 章节路径 + 相关度 + 原文/Markdown 表格）。"""
    if not hits:
        st.warning(f"未命中任何切片：「{query}」。可尝试更具体的科目名（如「存货跌价准备」）。")
        return
    for rank, hit in enumerate(hits, start=1):
        badges = (
            f"{_badge(hit.page_label, NAVY, solid=False)}"
            f"<span style='color:{MUTED};font-size:.75rem;margin-left:.4rem'>"
            f"相关度 {hit.score:.2f}｜route {hit.route_score:.2f}｜bm25 {hit.bm25_score:.2f}"
            f"{'｜含表格' if hit.has_table else ''}</span>"
        )
        st.markdown(
            f"<div style='margin:.6rem 0 .2rem 0;font-size:.86rem;color:{NAVY};"
            f"font-weight:600'>#{rank} {html_lib.escape(hit.section_path)}</div>"
            f"<div>{badges}</div>",
            unsafe_allow_html=True,
        )
        with st.expander("展开原文", expanded=rank == 1):
            st.code(hit.snippet(1600), language="text")


# ---------------------------------------------------------------------------
# Tab 5：可观测性
# ---------------------------------------------------------------------------
def render_observability(trace: Optional[dict[str, Any]]) -> None:
    if not trace:
        st.info("暂无运行轨迹（trace）数据。")
        return

    stats = token_breakdown(trace)
    columns = st.columns(6)
    columns[0].metric("LLM 调用次数", stats["calls"])
    columns[1].metric("Prompt Tokens", f"{stats['prompt']:,}")
    columns[2].metric("Completion Tokens", f"{stats['completion']:,}")
    columns[3].metric("总 Tokens", f"{stats['total']:,}")
    columns[4].metric(
        "KV-Cache 命中率",
        f"{stats['cache_hit_rate'] * 100:.1f}%",
        help="DeepSeek 前缀缓存：命中部分按更低价计费，命中率越高越省钱、首字越快",
    )
    columns[5].metric("LLM 累计延迟", f"{stats['llm_latency_s']:.1f}s")

    if stats["cached"]:
        st.markdown(
            f"""<div class="fs-panel ok">
              <div class="fs-panel-title">KV-Cache 前缀缓存（省钱提速）</div>
              本次共 <b>{stats['cached']:,}</b> / {stats['prompt']:,} 个 prompt token 命中前缀缓存，
              命中率 <b>{stats['cache_hit_rate'] * 100:.1f}%</b>。
              三个并行专家节点共享同一份事实包前缀，因此各自命中率通常最高——
              这正是 LangGraph 并行扇出（fan-out）复用同一上下文带来的直接收益。
            </div>""",
            unsafe_allow_html=True,
        )

    rows = stats["rows"]
    if rows:
        st.markdown("##### 分节点 Token 与延迟")
        _dataframe(
            [
                {
                    "节点": item.get("tag", ""),
                    "Prompt": item.get("prompt_tokens", 0),
                    "Cached": item.get("cached_tokens", 0),
                    "Completion": item.get("completion_tokens", 0),
                    "Total": item.get("total_tokens", 0),
                    "延迟(s)": item.get("latency_s", 0),
                    "结束原因": item.get("finish_reason", ""),
                }
                for item in rows
            ]
        )

    st.markdown("##### 节点执行轨迹")
    trace_rows = []
    for item in trace.get("trace") or []:
        elapsed = item.get("elapsed_s", item.get("latency_s"))
        tokens = item.get("total_tokens")
        note = ""
        if item.get("attempt"):
            note = f"第 {item.get('attempt')} 次撰写"
        elif item.get("errors"):
            note = f"{len(item.get('errors') or [])} 项未通过"
        elif item.get("reason"):
            note = str(item["reason"])
        elif "passed" in item:
            note = "通过" if item["passed"] else "未通过"
        trace_rows.append(
            {
                "节点": str(item.get("node", "")),
                "事件": str(item.get("event", "")),
                # 数值列统一用 None 占位，避免 arrow 因 "" 与 float 混排而报错
                "耗时(s)": float(elapsed) if isinstance(elapsed, (int, float)) else None,
                "Tokens": int(tokens) if isinstance(tokens, (int, float)) else None,
                "备注": note,
            }
        )
    if trace_rows:
        _dataframe(trace_rows)

    st.markdown("##### 防幻觉质检明细（fact_checker）")
    checks = fact_check_rows(trace)
    if checks:
        _dataframe(checks, height=min(520, 44 + 34 * len(checks)))
    passed = (trace.get("fact_check") or {}).get("passed")
    st.caption(
        "质检结论：" + ("全部通过" if passed else "存在未通过项（已按最大重试次数放行，需人工复核）")
    )


def render_provenance(report: FinancialReport, trace: Optional[dict[str, Any]] = None) -> None:
    """数据源溯源 + 聚合后的数据质量警告。"""
    meta = report.meta
    source = meta.data_source
    st.markdown("##### 数据源溯源")
    rows = [
        {
            "报表": name,
            "接口": source.interfaces.get(name, ""),
            "抓取时间": source.fetched_at.get(name, ""),
            "缓存命中": "是" if source.cache_hit.get(name) else "否",
        }
        for name in source.interfaces
    ]
    _dataframe(rows)
    st.caption(
        f"上游：{source.upstream}｜数据可见性截止日 {source.as_of_date}｜"
        f"引擎版本 v{meta.engine_version}｜报告生成时间 {meta.generated_at}"
    )

    st.markdown("##### 数据质量警告（聚合）")
    warnings = report.warnings
    if not warnings:
        st.success("无数据质量警告。")
        return
    counts: dict[str, int] = {}
    for item in warnings:
        counts[item.code] = counts.get(item.code, 0) + 1
    st.markdown(
        "、".join(f"`{code}`×{count}" for code, count in sorted(counts.items(), key=lambda kv: -kv[1]))
    )
    with st.expander(f"查看全部 {len(warnings)} 条警告"):
        _dataframe(
            [
                {"类型": item.code, "报告期": item.period or "", "说明": item.message}
                for item in warnings
            ]
        )

    summary = report.summary
    st.markdown("##### 报告摘要")
    st.markdown(
        f"""<div class="fs-panel">
          <div class="fs-panel-title">确定性结论（由算法生成，非 LLM 推断）</div>
          {html_lib.escape(summary.data_quality)}<br>
          {html_lib.escape(summary.risk_disclaimer)}
        </div>""",
        unsafe_allow_html=True,
    )
    for finding in summary.key_findings:
        st.markdown(f"- {finding}")
