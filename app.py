"""FinSight-Agent · 投研工作台（Streamlit 入口）。

启动方式::

    streamlit run app.py

设计要点
--------
1. **双入口**：
   ① 历史实盘报告秒开回放（扫描 ``data/reports/*_agent_trace.json``，0.5s 内渲染全套图表）；
   ② 实时深度解构运行（LangGraph 全流程，``st.status`` 逐节点展示进度）。
2. **UI 层零财务计算**（铁律 1）：所有数字来自 ``FinancialReport`` /
   ``PeerComparison`` / ``rag_evidences``，页面只做读取与格式化。
3. **优雅降级**：金融股（600036）走"准则差异说明"卡片；无 PDF 时 Tab 4 给出明确提示，
   而不是报错。
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any, Optional

import streamlit as st

# set_page_config 必须是第一个 Streamlit 调用
st.set_page_config(
    page_title="FinSight-Agent · 投研工作台",
    page_icon="◧",
    layout="wide",
    initial_sidebar_state="expanded",
)

from src.config import PDF_DIR, REPORT_OUTPUT_DIR, ensure_directories  # noqa: E402
from src.tools.financial_engine import DataUnavailableError  # noqa: E402
from src.ui import charts, components, state as ui_state  # noqa: E402
from src.ui.exporter import build_export_bundle  # noqa: E402
from src.ui.theme import inject_css  # noqa: E402

#: 快捷预设（演示友好）
PRESETS: tuple[tuple[str, str, str], ...] = (
    ("600519", "贵州茅台", "消费 · 白酒"),
    ("300750", "宁德时代", "制造 · 动力电池"),
    ("600036", "招商银行", "金融 · 降级演示"),
)

#: 节点中文名（用于 st.status 进度）
NODE_LABELS: dict[str, str] = {
    "data_loader": "① 装载财务事实与零歧义事实包",
    "fundamental_analyst": "② 基本面与杜邦归因分析",
    "forensic_auditor": "② CPA 舞弊排雷审计",
    "peer_valuation_analyst": "② 同业横向对标",
    "footnote_rag_verifier": "③ 年报附注穿透取证",
    "chief_editor": "④ 首席撰稿：六章节纪要",
    "fact_checker": "⑤ 防幻觉质检",
}

PAYLOAD_KEY = "fs_payload"


# ---------------------------------------------------------------------------
# 会话状态
# ---------------------------------------------------------------------------
def _set_payload(
    *,
    report: Any,
    markdown: str,
    trace: Optional[dict[str, Any]],
    peer: Any,
    source_label: str,
    archived: bool,
    generated_at: str,
) -> None:
    st.session_state[PAYLOAD_KEY] = {
        "report": report,
        "markdown": markdown,
        "trace": trace,
        "peer": peer,
        "source_label": source_label,
        "archived": archived,
        "generated_at": generated_at,
    }


def _get_payload() -> Optional[dict[str, Any]]:
    return st.session_state.get(PAYLOAD_KEY)


# ---------------------------------------------------------------------------
# 侧边栏
# ---------------------------------------------------------------------------
def render_sidebar() -> Optional[str]:
    """渲染控制台，返回需要执行的动作（'replay' / 'run' / None）。"""
    with st.sidebar:
        st.markdown(
            "<div style='font-size:1.02rem;font-weight:700;color:#1F3A5F'>FinSight-Agent</div>"
            "<div style='font-size:.74rem;color:#7A8794;margin-bottom:.7rem'>"
            "多智能体财报解构 · 会计排雷 · 附注穿透</div>",
            unsafe_allow_html=True,
        )
        mode = st.radio(
            "工作模式",
            options=["历史实盘回放", "实时深度解构"],
            captions=["零 Token 秒开，适合演示与复现", "跑完整 LangGraph 工作流"],
            label_visibility="collapsed",
        )
        st.divider()

        action: Optional[str] = None
        if mode == "历史实盘回放":
            action = _sidebar_replay()
        else:
            action = _sidebar_live()

        st.divider()
        _sidebar_status()
    return action


def _sidebar_replay() -> Optional[str]:
    runs = ui_state.scan_archived_runs()
    if not runs:
        st.warning("data/reports/ 下暂无历史存档。请先运行一次实时解构。")
        return None

    labels = [run.label for run in runs]
    index = st.selectbox(
        "选择历史报告",
        options=range(len(runs)),
        format_func=lambda i: labels[i],
        key="replay_index",
    )
    run = runs[index]
    st.caption(run.caption)
    st.caption(f"存档：{run.trace_path.name}")

    if st.button("载入该报告", type="primary", width="stretch"):
        return "replay"
    return None


def _sidebar_live() -> Optional[str]:
    preset_cols = st.columns(3)
    for column, (code, name, tag) in zip(preset_cols, PRESETS):
        if column.button(name, key=f"preset_{code}", width="stretch", help=tag):
            st.session_state["input_code"] = code
            st.session_state["input_peers"] = {
                "600519": "000858,000568",
                "300750": "002594,300014",
                "600036": "601601,601628",
            }.get(code, "")

    st.caption(
        "支持范围：沪深主板/创业板/科创板、北交所 920 号段、B 股（900/200）。"
        "43/83/87 旧号段与新三板暂无免费标准化三表（Patch 4.1）。"
    )

    with st.form("live_form", border=False):
        code = st.text_input(
            "股票代码", value=st.session_state.get("input_code", "600519"), max_chars=6
        )
        peers = st.text_input(
            "同业代码（逗号分隔，留空用内置默认池）",
            value=st.session_state.get("input_peers", "000858,000568"),
            help="传 `-` 表示跳过同业对比；无法取数的同业会自动跳过，不阻塞主标的",
        )
        col1, col2 = st.columns(2)
        lookback = col1.slider("回溯年度", 2, 6, 4)
        top_k = col2.slider("附注证据数/疑点", 2, 6, 4)
        enable_rag = st.checkbox("启用年报 PDF 附注穿透", value=True)
        offline = st.checkbox("离线模式（不调用 LLM，仅验证流程）", value=False)

        uploaded = st.file_uploader("上传年报 PDF（自动识别代码与年度并归档）", type=["pdf"])
        submitted = st.form_submit_button("开始实时解构", type="primary", width="stretch")

    if uploaded is not None:
        saved, identity = ui_state.save_uploaded_pdf(uploaded.name, uploaded.getvalue())
        code_hint = identity.get("code")
        year_hint = identity.get("report_year")
        if code_hint and year_hint:
            st.success(
                f"已归档为《{saved.name}》：识别到 {identity.get('stock_name', '')}"
                f"（{code_hint}）{year_hint}年报，可直接点击「开始实时解构」"
                f"—— 系统会从 PDF 还原三大表并跑通全链路。"
            )
        else:
            st.warning(
                f"已保存《{saved.name}》，但未能识别出证券代码/年度。"
                "请在代码框填写 6 位代码后运行，系统会按代码匹配该 PDF。"
            )

    if not submitted:
        return None

    st.session_state["pending_run"] = {
        "code": code.strip(),
        "peers": peers.strip(),
        "lookback": int(lookback),
        "top_k": int(top_k),
        "rag": bool(enable_rag),
        "offline": bool(offline),
    }
    return "run"


def _sidebar_status() -> None:
    """密钥状态（脱敏）+ 本地资产概览。"""
    try:
        from src.agents.llm_client import LLMConfig

        config = LLMConfig.from_env()
        status = config.key_status
        model = config.model
    except Exception:  # noqa: BLE001
        status, model = "未知", "—"

    runs = ui_state.scan_archived_runs()
    pdfs = sorted(PDF_DIR.glob("*.pdf")) if PDF_DIR.exists() else []
    st.markdown(
        f"""<div class="fs-panel" style="font-size:.78rem">
          <div class="fs-panel-title">运行环境</div>
          密钥：<b>{status}</b>（仅显示长度，绝不回显原文）<br>
          模型：<b>{model}</b><br>
          历史存档：<b>{len(runs)}</b> 份｜本地 PDF：<b>{len(pdfs)}</b> 份
        </div>""",
        unsafe_allow_html=True,
    )

    # ---- Patch 4.2：独立 PDF 检索台（零 Token，不依赖是否需要三大表）----
    with st.expander("本地 PDF 检索台（零 Token）", expanded=False):
        st.caption(
            "不跑分析也能用：任意年报/公告 PDF 都能做附注级检索，"
            "返回精确页码与原文表格。"
        )
        if not pdfs:
            st.caption("data/pdfs/ 下暂无 PDF，请先上传。")
        else:
            choice = st.selectbox(
                "选择 PDF",
                options=[str(path) for path in pdfs],
                format_func=lambda value: Path(value).name,
                key="pdf_review_choice",
            )
            if st.button("打开检索台", width="stretch"):
                st.session_state["pdf_review"] = choice
                st.session_state.pop(PAYLOAD_KEY, None)

    if pdfs:
        with st.expander("本地年报 PDF"):
            for path in pdfs[:8]:
                st.caption(f"{path.name}（{path.stat().st_size / 1024 / 1024:.1f} MB）")


# ---------------------------------------------------------------------------
# 动作处理
# ---------------------------------------------------------------------------
def do_replay() -> None:
    runs = ui_state.scan_archived_runs()
    index = int(st.session_state.get("replay_index", 0))
    if not runs or index >= len(runs):
        st.error("未找到可回放的历史存档。")
        return
    run = runs[index]
    with st.spinner("载入历史存档并重建财务报告…"):
        trace = ui_state.load_trace(run.trace_path)
        markdown = ui_state.load_markdown_text(run.markdown_path)
        try:
            report = ui_state.load_report(run.stock_code, lookback_years=4)
        except (DataUnavailableError, ValueError) as exc:
            st.error(f"无法重建 {run.stock_code} 的财务报告：{exc}")
            return
    _set_payload(
        report=report,
        markdown=markdown,
        trace=trace,
        peer=ui_state.peer_from_trace(trace),
        source_label=f"历史存档 · {run.trace_path.name}",
        archived=True,
        generated_at=dt.datetime.fromtimestamp(run.modified_at).strftime("%Y-%m-%d %H:%M"),
    )


def do_live_run() -> None:
    from src.agents.graph import AgentWorkflow

    options = st.session_state.get("pending_run") or {}
    code = str(options.get("code", "")).strip()
    if not code.isdigit() or len(code) != 6:
        st.error("请输入 6 位 A 股代码。")
        return

    peers_raw = str(options.get("peers", "")).strip()
    if peers_raw in {"", "-"}:
        peer_codes: list[str] = []
    else:
        peer_codes = [
            item.strip() for item in peers_raw.replace("，", ",").split(",") if item.strip()
        ]

    workflow = AgentWorkflow(offline=bool(options.get("offline")), verbose=False)
    progress_box = st.container()

    with progress_box:
        with st.status("正在运行多智能体工作流…", expanded=True) as status:
            def on_event(node: str, update: dict[str, Any], merged: dict[str, Any]) -> None:
                label = NODE_LABELS.get(node, node)
                detail = ""
                if node == "data_loader":
                    detail = f"事实包 {len(merged.get('fact_pack') or '')} 字符"
                elif node == "footnote_rag_verifier":
                    meta = merged.get("rag_meta") or {}
                    detail = (
                        f"{meta.get('pdf_period')} · {meta.get('note_chunks')} 个附注切片"
                        if meta.get("available")
                        else str(meta.get("reason", "已跳过"))
                    )
                elif node == "fact_checker":
                    fact = merged.get("fact_check") or {}
                    detail = "通过" if fact.get("passed") else f"{len(fact.get('errors') or [])} 项未通过"
                status.write(f"**{label}**　{detail}")

            try:
                result = workflow.run(
                    code,
                    peer_codes=peer_codes,
                    lookback_years=int(options.get("lookback", 4)),
                    rag_enabled=bool(options.get("rag", True)),
                    rag_top_k=int(options.get("top_k", 4)),
                    save=True,
                    on_event=on_event,
                )
            except DataUnavailableError as exc:
                # Patch 4.1/4.2：数据层已把上游异常翻译成中文业务提示，直接展示
                status.update(label="该标的无可用数据", state="error")
                st.error(str(exc))
                st.info(
                    "**还在报错？** 说明在线接口与本地 PDF 都没拿到数据。两条出路：\n"
                    "1. 在本页左侧上传该公司年报 PDF —— 系统会自动识别代码/年度并归档，"
                    "然后用 PDF 原文里的三大表跑通全链路；\n"
                    "2. 换用同行业可比公司（如北交所 920 号段、沪深主板）做对标分析。"
                )
                return
            except Exception as exc:  # noqa: BLE001 - UI 兜底
                status.update(label="运行失败", state="error")
                st.error(f"运行失败：{type(exc).__name__}: {exc}")
                return

            status.update(
                label=f"完成：{result.elapsed_s:.1f}s｜{result.total_tokens:,} tokens"
                f"｜重写 {result.retry_count} 次",
                state="complete" if result.passed else "complete",
            )

    if result.report is None:
        st.error("工作流未返回财务报告。")
        return
    _set_payload(
        report=result.report,
        markdown=result.markdown,
        trace=result.to_dict(),
        peer=result.peer,
        source_label=f"实时运行 · {result.mode.upper()}",
        archived=False,
        generated_at=dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
    )


# ---------------------------------------------------------------------------
# 主工作区
# ---------------------------------------------------------------------------
def render_header(payload: dict[str, Any]) -> None:
    report = payload["report"]
    meta = report.meta
    components.render_brand(
        f"{meta.stock_code} {meta.stock_name} ｜ {meta.latest_report_period} ｜ "
        f"{payload['source_label']} ｜ 生成于 {payload['generated_at']}"
    )
    components.render_kpi_strip(report, payload["trace"])
    components.render_financial_notice(report)


def render_tab_report(payload: dict[str, Any]) -> None:
    report = payload["report"]
    markdown = payload["markdown"] or ""
    bundle = build_export_bundle(report, markdown, payload["trace"])

    columns = st.columns([1.1, 1, 1, 3])
    columns[0].download_button(
        "下载打印级 HTML",
        data=bundle.to_bytes("html"),
        file_name=bundle.html_filename,
        mime="text/html",
        type="primary",
        width="stretch",
        help="自包含 HTML（内联 CSS，无外部依赖，浏览器 Ctrl+P 另存为 A4 PDF）",
    )
    columns[1].download_button(
        "下载 Markdown",
        data=bundle.to_bytes("markdown"),
        file_name=bundle.markdown_filename,
        mime="text/markdown",
        width="stretch",
    )
    columns[2].download_button(
        "下载运行轨迹",
        data=bundle.to_bytes("trace"),
        file_name=bundle.trace_filename,
        mime="application/json",
        width="stretch",
    )
    with columns[3]:
        st.caption(
            "HTML 报告含封面 KPI、六章节正文、附注页码引用与免责声明；"
            "打印时自动应用 A4 分页与表格防截断样式。"
        )

    components.render_report_markdown(markdown)

    if payload.get("archived"):
        st.caption(
            "提示：历史存档仅保存纪要文本与轨迹；图表与指标由本地缓存重建，"
            "如数据源在此后更新，可能与存档时点存在细微差异。"
        )


def render_tab_financials(payload: dict[str, Any]) -> None:
    report = payload["report"]
    peer = payload["peer"]

    st.markdown("##### 成长与盈利趋势")
    st.plotly_chart(
        charts.growth_trend_figure(report), width="stretch", config={"displayModeBar": False}
    )

    left, right = st.columns([1, 1])
    with left:
        st.markdown("##### 杜邦 ROE 连环替代归因")
        st.plotly_chart(
            charts.dupont_waterfall_figure(report),
            width="stretch",
            config={"displayModeBar": False},
        )
        if report.dupont.driver_note:
            st.caption(report.dupont.driver_note)
    with right:
        st.markdown("##### 现金流含金量")
        st.plotly_chart(
            charts.cashflow_figure(report), width="stretch", config={"displayModeBar": False}
        )

    st.markdown("##### 同业横向对标")
    st.plotly_chart(
        charts.peer_comparison_figure(peer), width="stretch", config={"displayModeBar": False}
    )
    rows = charts.peer_table_rows(peer)
    if rows:
        components._dataframe(rows)
    if peer is not None and peer.skipped:
        st.caption("取数失败被跳过：" + "；".join(peer.skipped))


def render_tab_forensic(payload: dict[str, Any]) -> None:
    report = payload["report"]
    analysis = report.forensic.beneish

    left, right = st.columns([1, 1.6])
    with left:
        st.markdown("##### Beneish M-Score 仪表")
        st.plotly_chart(
            charts.mscore_gauge_figure(report), width="stretch", config={"displayModeBar": False}
        )
        st.caption(analysis.risk_text or analysis.reason or "")
        if analysis.cautions:
            with st.expander("模型局限与适用性提醒"):
                for item in analysis.cautions:
                    st.markdown(f"- {item}")
    with right:
        st.markdown("##### 八变量拆解（相对 1.0 基准线）")
        st.plotly_chart(
            charts.beneish_figure(report), width="stretch", config={"displayModeBar": False}
        )

    if analysis.applicable and analysis.variables:
        with st.expander("M-Score 贡献雷达图"):
            st.plotly_chart(
                charts.beneish_radar_figure(report),
                width="stretch",
                config={"displayModeBar": False},
            )
        components._dataframe(
            [
                {
                    "变量": f"{item.code} {item.name_cn}",
                    "取值": item.value,
                    "系数": item.weight,
                    "贡献": item.contribution,
                    "公式": item.formula,
                }
                for item in analysis.variables
            ]
        )

    st.markdown("##### 六大本土化排雷规则")
    components.render_rule_cards(report)


def render_tab_rag(payload: dict[str, Any]) -> None:
    trace = payload["trace"] or {}
    rag_meta = trace.get("rag_meta") or {}
    evidences = (trace.get("rag_evidences") or {}).get("evidences") or []

    st.markdown("##### PDF 元信息")
    components.render_pdf_meta(rag_meta)

    st.markdown("##### 附注核查结论")
    components.render_evidence_cards(evidences)

    st.divider()
    pdf = ui_state.resolve_trace_pdf(trace)
    if pdf is None:
        candidates = ui_state.list_local_pdfs(payload["report"].meta.stock_code)
        if candidates:
            pdf = (candidates[0], ui_state.infer_pdf_period(candidates[0]))
    render_pdf_search(pdf)


def render_pdf_search(pdf: Optional[tuple[Path, str]]) -> None:
    """附注即时检索（Tab 4 与独立检索台共用）。"""
    st.markdown("##### 附注即时检索（零 Token 消耗）")
    if pdf is None:
        st.info(
            "当前没有可用的年报 PDF。可在左侧「实时深度解构」中勾选附注穿透自动下载，"
            "或上传 PDF / 手动放入 `data/pdfs/`。"
        )
        return

    path, period = pdf
    st.caption(f"检索范围：{path.name}（{period}）")
    try:
        with st.spinner("加载附注切片与索引…"):
            retriever, document = ui_state.load_retriever(path, period)
    except Exception as exc:  # noqa: BLE001
        st.error(f"加载 PDF 失败：{type(exc).__name__}: {exc}")
        return

    st.caption(
        f"切片 {len(document.chunks)} 个（核心 {len(document.core_chunks)}），"
        f"覆盖 {document.page_count} 页；索引常驻内存，单次检索 2~5 ms。"
    )

    col1, col2, col3 = st.columns([2.2, 1, 1])
    query = col1.text_input(
        "输入会计科目或问题",
        value="",
        placeholder="例如：存货跌价准备 / 研发费用资本化 / 前五名客户 / 存放同业",
        key=f"q_{path.name}",
    )
    account = col2.text_input("科目（可选，用于精确路由）", value="", key=f"a_{path.name}")
    top_k = col3.slider("返回条数", 3, 8, 5, key=f"k_{path.name}")

    if query or account:
        outcome = retriever.search(query or account, target_account=account, top_k=top_k)
        st.caption(
            f"检索「{query or account}」：命中 {len(outcome.hits)} 条，"
            f"耗时 {outcome.stats.elapsed_s * 1000:.0f} ms，"
            f"科目路由命中 {outcome.stats.route_hits} 条、含表格 {outcome.stats.table_hits} 条"
        )
        components.render_retriever_hits(outcome.hits, query or account)
    else:
        st.caption("输入关键词即可在当前 PDF 中即时检索原文（不调用任何大模型，零 Token）。")


def render_pdf_review() -> None:
    """独立 PDF 审阅台：没有财务报告也能用（纯公告 PDF 亦可）。"""
    raw = st.session_state.get("pdf_review")
    if not raw:
        return
    path = Path(raw)
    components.render_brand(f"本地 PDF 检索台 ｜ {path.name}")
    st.caption(
        "该页面完全本地运行：把 PDF 切成带页码的附注切片并做混合检索，"
        "不调用任何大模型、不消耗 Token。"
    )
    if not path.exists():
        st.error(f"文件不存在：{path}")
        return
    render_pdf_search((path, ui_state.infer_pdf_period(path)))
    if st.button("返回工作台"):
        st.session_state.pop("pdf_review", None)
        st.rerun()


def render_tab_observability(payload: dict[str, Any]) -> None:
    components.render_observability(payload["trace"])
    st.divider()
    components.render_provenance(payload["report"], payload["trace"])


# ---------------------------------------------------------------------------
# 空状态
# ---------------------------------------------------------------------------
def render_empty_state() -> None:
    components.render_brand("多智能体财报解构 · 会计排雷 · 年报附注穿透")
    st.markdown(
        """<div class="fs-panel">
          <div class="fs-panel-title">开始使用</div>
          <b>历史实盘回放</b>：在左侧选择一份已落盘的报告，0.5 秒内即可查看完整图表、
          六章节纪要、附注证据与质检轨迹（零 Token）。<br>
          <b>实时深度解构</b>：输入 6 位代码运行完整流程；可开启年报 PDF 附注穿透，
          让审计疑点在年报原文中取证。<br>
          <b>没有在线数据？</b>新三板 / 北交所旧号段等标的，只要上传其年报 PDF，
          系统会自动从 PDF 还原三大表并跑通全链路。
        </div>""",
        unsafe_allow_html=True,
    )

    runs = ui_state.scan_archived_runs()
    pdfs = sorted(PDF_DIR.glob("*.pdf")) if PDF_DIR.exists() else []
    columns = st.columns(3)
    columns[0].metric("历史存档", f"{len(runs)} 份")
    columns[1].metric("本地年报 PDF", f"{len(pdfs)} 份")
    columns[2].metric("输出目录", "data/reports")

    if pdfs:
        st.markdown("##### 本地 PDF 速览")
        components._dataframe(
            [
                {
                    "文件": path.name,
                    "大小(MB)": round(path.stat().st_size / 1024 / 1024, 2),
                    "推断期次": ui_state.infer_pdf_period(path),
                }
                for path in pdfs
            ]
        )
        if st.button("直接检索最近上传的 PDF（零 Token）"):
            st.session_state["pdf_review"] = str(pdfs[-1])
            st.rerun()

    if runs:
        st.markdown("##### 可回放的报告")
        components._dataframe(
            [
                {
                    "标的": f"{run.stock_code} {run.stock_name}",
                    "报告期": run.latest_period,
                    "模式": run.mode,
                    "耗时(s)": round(run.elapsed_s, 1),
                    "Tokens": run.total_tokens,
                    "附注实证": f"{run.rag_verified}/{run.rag_total}" if run.rag_total else "—",
                    "质检": "通过" if run.fact_check_passed else "放行",
                }
                for run in runs
            ]
        )


def render_footer() -> None:
    st.divider()
    st.caption(
        "FinSight-Agent v1.0 ｜ 计算与推理严格分离：所有财务数值由 Python 引擎确定性计算，"
        "LLM 仅负责解读与撰写；报告核心锚点、单位、报告期与附注页码均经确定性质检。"
        "本工具输出不构成任何投资建议。"
    )


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------
def main() -> None:
    ensure_directories()
    inject_css()

    # Patch 4.2：把 data/pdfs 里未规范命名的 PDF 自动归档为标准名（每个会话一次）
    if not st.session_state.get("_pdf_adopted"):
        # 防御：streamlit 热重载不会重新导入已加载的 src.ui.state，
        # 若进程持有旧模块（无该函数），此处降级为空实现而不是整页 AttributeError
        adopt = getattr(ui_state, "adopt_local_pdfs", None)
        adopted: list[tuple[str, str]] = []
        if callable(adopt):
            try:
                adopted = adopt()
            except Exception as exc:  # noqa: BLE001 - 归档失败不应阻塞工作台
                st.warning(f"本地 PDF 自动归档跳过：{type(exc).__name__}: {exc}")
        else:
            st.warning(
                "当前进程加载的是旧版 `src/ui/state.py`（缺少 PDF 自动归档）。"
                "请重启 Streamlit 服务（Ctrl+C 后重新 `streamlit run app.py`）以启用 Patch 4.2。"
            )
        st.session_state["_pdf_adopted"] = True
        if adopted:
            st.session_state["_pdf_adopted_log"] = adopted

    action = render_sidebar()
    if action == "replay":
        st.session_state.pop("pdf_review", None)
        do_replay()
    elif action == "run":
        st.session_state.pop("pdf_review", None)
        do_live_run()

    adopted_log = st.session_state.get("_pdf_adopted_log") or []
    if adopted_log:
        st.info(
            "已自动归档未规范命名的 PDF："
            + "；".join(f"{old} → {new}" for old, new in adopted_log[:3])
        )

    if st.session_state.get("pdf_review") and _get_payload() is None:
        render_pdf_review()
        render_footer()
        return

    payload = _get_payload()
    if payload is None:
        render_empty_state()
        render_footer()
        return

    render_header(payload)

    tab_report, tab_fin, tab_forensic, tab_rag, tab_obs = st.tabs(
        [
            "深度投研纪要与导出",
            "杜邦归因与财务可视化",
            "CPA 排雷与 M-Score",
            "年报附注穿透取证台",
            "白盒可观测性",
        ]
    )
    with tab_report:
        render_tab_report(payload)
    with tab_fin:
        render_tab_financials(payload)
    with tab_forensic:
        render_tab_forensic(payload)
    with tab_rag:
        render_tab_rag(payload)
    with tab_obs:
        render_tab_observability(payload)

    render_footer()


main()
