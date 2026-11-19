"""LangGraph 节点实现（确定性装载 / 并行分析 / 撰稿 / 质检）。

节点契约
--------
每个节点只返回**局部状态字典**，由 LangGraph 合并；三个并行分析节点各自写自己的键，
``usage`` / ``trace`` / ``warnings`` 三个键使用 ``Annotated[list, operator.add]`` 归并，
因此并行写入不会互相覆盖（这是并行 fan-out 的关键）。
"""

from __future__ import annotations

import datetime as dt
import operator
import re
import time
from dataclasses import dataclass
from typing import Annotated, Any, Optional, TypedDict

from src.agents.context_formatter import FactPackBuilder
from src.agents.fact_check import FactCheckResult, run_fact_check
from src.agents.llm_client import DeepSeekClient, LLMError, mask_secret
from src.agents.prompts import (
    CHIEF_SYSTEM,
    FOOTNOTE_VERIFIER_SYSTEM,
    FORENSIC_SYSTEM,
    FUNDAMENTAL_SYSTEM,
    PEER_SYSTEM,
    REPORT_TITLE,
    build_editor_user_message,
    build_fact_pack_message,
    build_verifier_user_message,
)
from src.tools.financial_engine import analyze_stock
from src.tools.peer_comparison import PeerComparison, compare_peers
from src.tools.schemas import FinancialReport

#: 并行节点输出的消息标记（供 trace 展示）
AGENT_TAGS: dict[str, str] = {
    "fundamental_analyst": "基本面与杜邦归因",
    "forensic_auditor": "CPA 舞弊排雷审计",
    "peer_valuation_analyst": "同业对标研究",
}


class AgentState(TypedDict, total=False):
    """多智能体工作流状态。"""

    # ---- 输入 ----
    stock_code: str
    peer_codes: Optional[list[str]]
    include_peers: bool
    lookback_years: int

    # ---- Node 1 产出 ----
    report: Any  # FinancialReport（对象，便于质检与渲染）
    fact_pack: str
    anchors: list[dict]
    peer_comparison: Any  # PeerComparison | None

    # ---- 并行分析层产出 ----
    fundamental: dict
    forensic: dict
    peer_analysis: dict

    # ---- Phase 3：附注穿透核查 ----
    rag_enabled: bool
    pdf_path: Optional[str]
    rag_top_k: int
    rag_evidences: dict
    rag_meta: dict

    # ---- Node 4 / 5 ----
    report_markdown: str
    audit_feedback: list[str]
    retry_count: int
    fact_check: dict
    next_action: str

    # ---- 归并型通道（并行安全）----
    usage: Annotated[list[dict], operator.add]
    trace: Annotated[list[dict], operator.add]
    warnings: Annotated[list[str], operator.add]


@dataclass
class WorkflowRuntime:
    """节点依赖的运行时对象。"""

    client: DeepSeekClient
    fact_builder: FactPackBuilder
    max_retries: int = 2
    verbose: bool = True

    def log(self, message: str) -> None:
        if self.verbose:
            print(mask_secret(message), flush=True)


def _now() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


def _parse_page_label(raw: str) -> Optional[int]:
    """从 ``P.91`` / ``P.91-92`` / ``91`` / ``第91页`` 中提取起始页码。"""
    match = re.search(r"(\d{1,4})", str(raw or ""))
    return int(match.group(1)) if match else None


class AgentExecutionError(RuntimeError):
    """关键分析节点在重试后仍失败（如 JSON 截断），必须中止而非静默降级。

    设计理由：`footnote_queries` 是 Phase 3 的年报 RAG 接口契约。若审计节点静默降级，
    产出的报告看似完整却丢失了该接口，属于"看起来成功、实际违约"的最坏情况。
    """


#: 失败重试时追加的精简指令（截断/解析失败最常见原因是输出过长）
_COMPACT_HINT = (
    "\n\n【上一次输出被截断或不是合法 JSON】请重新输出，并严格做到：\n"
    "1) 只输出一个完整、可被 json.loads 解析的 JSON 对象，不要任何解释；\n"
    "2) 每个字符串字段控制在 80 字以内，数组元素控制在 3 条以内；\n"
    "3) 优先保证 `footnote_queries` 等结构化字段完整闭合。"
)


def _strip_code_fence(text: str) -> str:
    """去掉模型偶尔加上的 ```markdown 围栏。"""
    stripped = (text or "").strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        stripped = "\n".join(lines).strip()
    return stripped


def _ensure_title(markdown: str) -> str:
    """确保报告以标准一级标题开头（确定性兜底，不依赖模型自觉）。

    模型经常直接从「## 一、」开始写，导致 fact_check 报 ``title_missing`` 警告。
    这里在落盘前统一补齐 `# FinSight 深度财务体检与排雷纪要（Tear Sheet）`。
    """
    text = (markdown or "").lstrip()
    if not text:
        return f"# {REPORT_TITLE}\n"
    first_line = text.splitlines()[0].strip()
    if first_line.startswith("# ") and "Tear Sheet" in first_line:
        return text
    if first_line.startswith("# "):
        # 已有 H1 但不是标准标题（如模型自拟标题）→ 替换为标准标题并保留其余内容
        rest = "\n".join(text.splitlines()[1:]).lstrip()
        return f"# {REPORT_TITLE}\n\n{rest}"
    return f"# {REPORT_TITLE}\n\n{text}"


class WorkflowNodes:
    """把五个节点实现为方法，便于注入运行时依赖（LLM 客户端 / 工具）。"""

    def __init__(self, runtime: WorkflowRuntime) -> None:
        self.rt = runtime

    # ------------------------------------------------------------------
    # Node 1：确定性数据装载
    # ------------------------------------------------------------------
    def data_loader_node(self, state: AgentState) -> dict[str, Any]:
        code = str(state["stock_code"])
        lookback = int(state.get("lookback_years", 4))
        started = time.perf_counter()
        self.rt.log(f"[data_loader] 装载 {code} 财务事实（回溯 {lookback} 年）……")

        report: FinancialReport = analyze_stock(code, lookback_years=lookback)

        peer: Optional[PeerComparison] = None
        peer_text = ""
        warnings: list[str] = []
        if state.get("include_peers", True):
            try:
                peer = compare_peers(code, state.get("peer_codes"), lookback_years=max(3, lookback))
                peer_text = peer.fact_pack_section()
                if peer.available:
                    self.rt.log(
                        f"[data_loader] 同业对标完成：{', '.join(peer.peer_codes)}"
                        + (f"（跳过 {len(peer.skipped)} 家）" if peer.skipped else "")
                    )
                else:
                    self.rt.log("[data_loader] 无可比同业，转为单公司历史分位模式")
                    warnings.extend(peer.skipped)
            except Exception as exc:  # noqa: BLE001 - 同业失败不阻塞主流程
                message = f"同业对标失败，已降级为单公司模式：{type(exc).__name__}: {exc}"
                warnings.append(message)
                self.rt.log(f"[data_loader] {message}")

        pack = self.rt.fact_builder.build(report, peer_text=peer_text)
        elapsed = time.perf_counter() - started
        self.rt.log(
            f"[data_loader] 事实包就绪：{len(pack.text)} 字符 / 约 {pack.estimated_tokens} tokens / "
            f"锚点 {len(pack.anchors)} 个 / 覆盖 {len(pack.periods)} 年（{elapsed:.2f}s）"
        )

        return {
            "report": report,
            "fact_pack": pack.text,
            "anchors": pack.anchor_dicts(),
            "peer_comparison": peer,
            "warnings": warnings,
            "retry_count": 0,
            "trace": [
                {
                    "node": "data_loader",
                    "event": "facts_loaded",
                    "at": _now(),
                    "elapsed_s": round(elapsed, 3),
                    "fact_pack_chars": len(pack.text),
                    "fact_pack_tokens_est": pack.estimated_tokens,
                    "anchors": len(pack.anchors),
                    "periods": pack.periods,
                    "peers": list(peer.peer_codes) if peer else [],
                    "peer_skipped": list(peer.skipped) if peer else [],
                }
            ],
        }

    # ------------------------------------------------------------------
    # 并行分析层
    # ------------------------------------------------------------------
    def fundamental_analyst_node(self, state: AgentState) -> dict[str, Any]:
        return self._run_agent(
            state,
            tag="fundamental",
            state_key="fundamental",
            label=AGENT_TAGS["fundamental_analyst"],
            system=FUNDAMENTAL_SYSTEM,
            extra_user="请基于事实包完成基本面与杜邦归因分析，严格按 JSON 契约输出。",
        )

    def forensic_auditor_node(self, state: AgentState) -> dict[str, Any]:
        return self._run_agent(
            state,
            tag="forensic",
            state_key="forensic",
            label=AGENT_TAGS["forensic_auditor"],
            system=FORENSIC_SYSTEM,
            extra_user=(
                "请完成 CPA 舞弊排雷审计。特别注意：\n"
                "1) 货币资金与期末现金及现金等价物的勾稽结论（区分受限资金与合并财务公司口径差异）；\n"
                "2) 纸面富贵规则的重要性门槛是否被拦截；\n"
                "3) 非经常性损益是净收益还是净支出；\n"
                "4) footnote_queries 必须覆盖所有被触发的规则，并给出具体到附注小节的检索语句。\n"
                "5) 篇幅克制：每条 assessment 不超过 60 字，优先保证 JSON 结构完整闭合。"
            ),
            critical=True,
        )

    def peer_valuation_analyst_node(self, state: AgentState) -> dict[str, Any]:
        peer = state.get("peer_comparison")
        has_peer = bool(peer is not None and getattr(peer, "available", False))
        hint = (
            "事实包第 9 节含同业对标表，请做四维度对比。"
            if has_peer
            else "本次未提供可比同业数据：请将 peer_available 设为 false，做单公司历史序列分位评估，"
            "严禁编造行业均值。"
        )
        return self._run_agent(
            state,
            tag="peer",
            state_key="peer_analysis",
            label=AGENT_TAGS["peer_valuation_analyst"],
            system=PEER_SYSTEM,
            extra_user=hint,
        )

    # ------------------------------------------------------------------
    # Node 3.5：年报附注穿透核查（Phase 3 RAG）
    # ------------------------------------------------------------------
    def footnote_rag_verifier_node(self, state: AgentState) -> dict[str, Any]:
        """用年报附注原文验证审计疑点，产出带页码引用的 `rag_evidences`。

        退化策略（铁律 3）：`--no-rag`、无 PDF、解析失败、检索为空 → 一律返回空证据
        并记录清晰原因，工作流继续走 Phase 2 的第六章形态，绝不阻塞报告生成。
        """
        started = time.perf_counter()

        if not state.get("rag_enabled", True):
            return self._skip_rag("已关闭附注穿透（--no-rag），第六章将回退为待核查清单", started)

        report = state.get("report")
        forensic = state.get("forensic") or {}
        queries = list(forensic.get("footnote_queries") or [])
        if not queries:
            return self._skip_rag(
                "审计节点未产出 footnote_queries，无法发起附注核查（第六章回退为待核查清单）",
                started,
            )

        financial_period = getattr(getattr(report, "meta", None), "latest_report_period", "") or ""
        stock_code = getattr(getattr(report, "meta", None), "stock_code", "") or state.get("stock_code", "")
        target_year = int(financial_period[:4]) if financial_period[:4].isdigit() else None

        # ---- 1) 双通道获取 PDF ----
        try:
            from src.rag.pdf_fetcher import AnnualReportFetcher

            fetcher = AnnualReportFetcher(verbose=self.rt.verbose)
            artifact = fetcher.fetch(stock_code, target_year, local_path=state.get("pdf_path"))
        except Exception as exc:  # noqa: BLE001 - 任何异常都降级
            return self._skip_rag(f"PDF 获取异常（{type(exc).__name__}: {exc}）", started)

        if artifact is None:
            return self._skip_rag(f"未获取到年报 PDF：{fetcher.last_reason}", started)

        # ---- 2) 解析附注 ----
        try:
            from src.rag.pdf_parser import AnnualReportParser

            document = AnnualReportParser(verbose=self.rt.verbose).parse(
                artifact.path, artifact.pdf_period
            )
        except Exception as exc:  # noqa: BLE001
            return self._skip_rag(f"PDF 解析失败（{type(exc).__name__}: {exc}）", started)

        # ---- 3) 混合检索 ----
        try:
            from src.rag.retriever import HybridRetriever, format_evidence_for_prompt

            retriever = HybridRetriever(document, top_k=int(state.get("rag_top_k", 4)))
            if not retriever.available:
                return self._skip_rag("切片库为空，检索不可用", started)
            outcomes = retriever.search_queries(queries)
            evidence_text = format_evidence_for_prompt(outcomes)
        except Exception as exc:  # noqa: BLE001
            return self._skip_rag(f"检索失败（{type(exc).__name__}: {exc}）", started)

        self.rt.log(
            f"[rag_verifier] {artifact.pdf_period} PDF 第 {document.page_count} 页 → "
            f"{len(document.chunks)} 切片（附注 {len(document.note_chunks)}），"
            f"对 {len(queries)} 条疑点完成检索"
        )
        top_pages = {
            outcome.target_account or "?": [hit.page_label for hit in outcome.hits[:3]]
            for outcome in outcomes
        }
        self.rt.log(f"[rag_verifier] 命中页码：{top_pages}")

        # ---- 4) LLM 判定（只准依据证据原文） ----
        user_message = build_verifier_user_message(
            evidence_text,
            queries,
            pdf_period=artifact.pdf_period,
            financial_period=financial_period or "未知",
        )
        warnings: list[str] = []
        try:
            payload, result = self.rt.client.chat_json(
                FOOTNOTE_VERIFIER_SYSTEM, user_message, tag="rag_verifier"
            )
        except LLMError as exc:
            message = mask_secret(f"附注核查 LLM 调用失败：{exc}")
            warnings.append(message)
            return {
                "rag_evidences": {"evidences": [], "pdf_period": artifact.pdf_period},
                "rag_meta": self._rag_meta(artifact, document, outcomes, llm_ok=False),
                "warnings": [message],
                "trace": [
                    {
                        "node": "footnote_rag_verifier",
                        "event": "llm_failed",
                        "at": _now(),
                        "error": message,
                        "elapsed_s": round(time.perf_counter() - started, 3),
                    }
                ],
            }

        # ---- 5) 确定性校验：页码必须在检索证据范围内 ----
        evidences, validation_warnings = self._validate_evidences(
            payload.get("evidences") or [], outcomes, queries, artifact.pdf_period
        )
        warnings.extend(validation_warnings)

        elapsed = time.perf_counter() - started
        verified = sum(1 for item in evidences if item["verdict"] != "inconclusive")
        self.rt.log(
            f"[rag_verifier] 完成：{len(evidences)} 条结论（已证实 {verified} / "
            f"待核查 {len(evidences) - verified}）/ {result.total_tokens} tokens / {elapsed:.1f}s"
        )

        return {
            "rag_evidences": {
                "pdf_period": artifact.pdf_period,
                "financial_period": financial_period,
                "period_mismatch": artifact.pdf_period != financial_period,
                "period_note": str(payload.get("period_note", "") or ""),
                "evidences": evidences,
            },
            "rag_meta": self._rag_meta(artifact, document, outcomes, llm_ok=True),
            "usage": [result.usage_record("rag_verifier")],
            "warnings": warnings,
            "trace": [
                {
                    "node": "footnote_rag_verifier",
                    "event": "verified",
                    "at": _now(),
                    "elapsed_s": round(elapsed, 3),
                    "pdf_period": artifact.pdf_period,
                    "pages": document.page_count,
                    "chunks": len(document.chunks),
                    "note_chunks": len(document.note_chunks),
                    "top_pages": top_pages,
                    "evidences": len(evidences),
                    "verified": verified,
                    "total_tokens": result.total_tokens,
                    "latency_s": round(result.latency_s, 3),
                    "validation_warnings": validation_warnings,
                }
            ],
        }

    def _skip_rag(self, reason: str, started: float) -> dict[str, Any]:
        """RAG 不可用时的平滑退化。"""
        self.rt.log(f"[rag_verifier] 跳过附注穿透：{reason}")
        return {
            "rag_evidences": {"evidences": [], "pdf_period": "", "note": reason},
            "rag_meta": {"enabled": True, "available": False, "reason": reason},
            "warnings": [f"附注穿透未执行：{reason}"],
            "trace": [
                {
                    "node": "footnote_rag_verifier",
                    "event": "skipped",
                    "at": _now(),
                    "reason": reason,
                    "elapsed_s": round(time.perf_counter() - started, 3),
                }
            ],
        }

    @staticmethod
    def _rag_meta(
        artifact: Any, document: Any, outcomes: list, *, llm_ok: bool
    ) -> dict[str, Any]:
        return {
            "enabled": True,
            "available": True,
            "llm_ok": llm_ok,
            "pdf_path": str(artifact.path),
            "pdf_period": artifact.pdf_period,
            "pdf_source": artifact.source,
            "period_mismatch_note": artifact.period_mismatch_note,
            "page_count": document.page_count,
            "chunks": len(document.chunks),
            "note_chunks": len(document.note_chunks),
            "chunks_with_table": sum(1 for chunk in document.chunks if chunk.has_table),
            "parse_cache_hit": bool(document.stats.get("cache_hit")),
            "parse_elapsed_s": document.stats.get("elapsed_s"),
            "queries": len(outcomes),
            "retrieval_ms": round(
                sum(getattr(outcome.stats, "elapsed_s", 0.0) for outcome in outcomes) * 1000, 1
            ),
            "top_pages": {
                outcome.target_account or "?": [hit.page_label for hit in outcome.hits[:3]]
                for outcome in outcomes
            },
        }

    @staticmethod
    def _validate_evidences(
        raw_evidences: list[dict],
        outcomes: list,
        queries: list[dict],
        pdf_period: str,
    ) -> tuple[list[dict], list[str]]:
        """确定性校验模型给出的页码与结论（防"编造页码"幻觉）。

        * 页码必须落在**该科目实际检索到的证据页**范围内，否则标记 ``verified=False``；
        * 一旦出现未核实页码，``verified_benign`` 必须降级为 ``inconclusive``；
        * ``pdf_period`` 一律用真实 PDF 期次覆盖模型输出（铁律 2）。
        """
        warnings: list[str] = []
        allowed: dict[str, set[int]] = {}
        for outcome in outcomes:
            pages: set[int] = set()
            for hit in outcome.hits:
                pages.update(range(hit.page_num, hit.page_end + 1))
            allowed[(outcome.target_account or "").strip()] = pages

        by_account: dict[str, dict] = {}
        for item in raw_evidences:
            if isinstance(item, dict):
                account = str(item.get("target_account", "")).strip()
                by_account[account] = item

        collected: list[dict] = []
        for query in queries:
            account = str(query.get("target_account", "")).strip()
            item = by_account.get(account)
            if item is None:
                collected.append(
                    {
                        "target_account": account,
                        "verdict": "inconclusive",
                        "finding_summary": "附注未检索到相关明细（模型未返回该科目的核查结论）。",
                        "citations": [],
                    }
                )
                warnings.append(f"{account}：模型未返回核查结论，已置为 inconclusive")
                continue

            verdict = str(item.get("verdict", "")).strip()
            if verdict not in {"verified_benign", "verified_risk", "inconclusive"}:
                warnings.append(f"{account}：非法 verdict={verdict!r}，已降级为 inconclusive")
                verdict = "inconclusive"

            pages_allowed = allowed.get(account, set())
            citations: list[dict] = []
            invalid: list[str] = []
            for citation in item.get("citations") or []:
                if not isinstance(citation, dict):
                    continue
                raw_page = str(citation.get("page", "")).strip()
                page_num = _parse_page_label(raw_page)
                verified = page_num is not None and (not pages_allowed or page_num in pages_allowed)
                if not verified:
                    invalid.append(raw_page)
                citations.append(
                    {
                        "pdf_period": pdf_period,
                        "page": f"P.{page_num}" if page_num is not None else raw_page,
                        "page_num": page_num,
                        "quote": str(citation.get("quote", "")).strip()[:160],
                        "verified": verified,
                    }
                )

            if invalid:
                warnings.append(
                    f"{account}：引用页码 {invalid} 不在检索证据范围内，已标记未核实"
                )
                # 任何依赖"未被核实页码"的结论都不可信 —— 一律降级为待核查
                if verdict != "inconclusive":
                    warnings.append(
                        f"{account}：因存在未核实页码，结论 {verdict} 已降级为 inconclusive"
                    )
                    verdict = "inconclusive"

            collected.append(
                {
                    "target_account": account,
                    "verdict": verdict,
                    "finding_summary": str(item.get("finding_summary", "")).strip(),
                    "citations": citations,
                    "citation_pages": [c["page"] for c in citations if c["verified"]],
                }
            )
        return collected, warnings

    # ------------------------------------------------------------------
    # Node 4：首席撰稿
    # ------------------------------------------------------------------
    def chief_editor_node(self, state: AgentState) -> dict[str, Any]:
        feedback = list(state.get("audit_feedback") or [])
        attempt = int(state.get("retry_count", 0)) + 1
        rag = state.get("rag_evidences") or {}
        has_rag = bool(rag.get("evidences"))
        rag_note = ""
        if not has_rag:
            meta = state.get("rag_meta") or {}
            rag_note = str(meta.get("reason") or rag.get("note") or "未启用附注穿透")

        self.rt.log(
            f"[chief_editor] 第 {attempt} 次撰写"
            + (f"（携带 {len(feedback)} 条质检反馈）" if feedback else "")
            + (f"（含 {len(rag.get('evidences') or [])} 条附注穿透证据）" if has_rag else "（无附注证据）")
            + "……"
        )
        user_message = build_editor_user_message(
            state["fact_pack"],
            state.get("fundamental") or {},
            state.get("forensic") or {},
            state.get("peer_analysis") or {},
            audit_feedback=feedback or None,
            previous_markdown=state.get("report_markdown", ""),
            rag_evidences=rag if has_rag else None,
            rag_note=rag_note,
        )
        result = self.rt.client.chat(
            CHIEF_SYSTEM,
            user_message,
            tag="chief_editor",
            json_mode=False,
            # 六章节纪要通常 5000~8000 tokens；沿用分析节点的 4096 会在第五章被硬截断
            max_tokens=self.rt.client.config.editor_max_tokens,
        )
        markdown = _ensure_title(_strip_code_fence(result.text))
        warnings: list[str] = []
        if result.truncated:
            message = (
                f"首席撰稿输出被 max_tokens={self.rt.client.config.editor_max_tokens} 截断"
                f"（finish_reason=length），报告可能缺少尾部章节；"
                f"请提高 FINSIGHT_EDITOR_MAX_TOKENS 或要求模型精简篇幅。"
            )
            warnings.append(message)
            self.rt.log(f"[chief_editor] {message}")
        self.rt.log(f"[chief_editor] 完成：{len(markdown)} 字符 / {result.total_tokens} tokens")
        return {
            "report_markdown": markdown,
            "usage": [result.usage_record("chief_editor")],
            "warnings": warnings,
            "trace": [
                {
                    "node": "chief_editor",
                    "event": "markdown_written",
                    "at": _now(),
                    "attempt": attempt,
                    "chars": len(markdown),
                    "feedback_items": len(feedback),
                    "latency_s": round(result.latency_s, 3),
                    "total_tokens": result.total_tokens,
                    "finish_reason": result.finish_reason,
                    "truncated": result.truncated,
                }
            ],
        }

    # ------------------------------------------------------------------
    # Node 5：确定性质检 + 条件边
    # ------------------------------------------------------------------
    def fact_checker_node(self, state: AgentState) -> dict[str, Any]:
        report: Optional[FinancialReport] = state.get("report")
        markdown = state.get("report_markdown", "")
        peer: Optional[PeerComparison] = state.get("peer_comparison")

        allowed: set[str] = set(report.meta.report_periods) if report is not None else set()
        if peer is not None:
            allowed.update(row.latest_report_period for row in peer.rows)

        # 事实包中含有"同业数据可信度提示"时，报告必须转述（确定性护栏，不依赖模型自觉）
        fact_pack = state.get("fact_pack", "")
        required_phrases: list[tuple[str, tuple[str, ...]]] = []
        if "同业数据可信度提示" in fact_pack:
            required_phrases.append(
                ("同业数据可信度提示", ("同业数据可信度", "数据可信度提示", "待核实参考"))
            )

        # Phase 3：已核实的附注页码必须在报告中原样出现（可追溯性硬约束）
        page_labels: list[str] = []
        rag = state.get("rag_evidences") or {}
        for item in rag.get("evidences") or []:
            for citation in item.get("citations") or []:
                label = str(citation.get("page") or "").strip()
                if citation.get("verified") and label and label not in page_labels:
                    page_labels.append(label)

        result: FactCheckResult = run_fact_check(
            markdown,
            state.get("anchors") or [],
            allowed_periods=allowed or None,
            required_phrases=tuple(required_phrases),
            required_page_labels=tuple(page_labels),
        )
        current_retry = int(state.get("retry_count", 0))

        base_trace = {
            "node": "fact_checker",
            "event": "checked",
            "at": _now(),
            "passed": result.passed,
            "errors": result.errors,
            "warnings": result.warnings,
            "stats": result.stats,
            "retry_count": current_retry,
        }

        if result.passed:
            self.rt.log(
                f"[fact_checker] 通过：锚点 {result.stats['anchors_ok']}/{result.stats['anchors']} 命中，"
                f"章节 {result.stats['sections_ok']}/6 完整"
            )
            return {
                "fact_check": result.to_dict(),
                "next_action": "accept",
                "audit_feedback": [],
                "trace": [base_trace],
            }

        if current_retry >= self.rt.max_retries:
            message = (
                f"自动质检在 {current_retry} 次重写后仍未通过，已按最大重试次数放行（结果需人工复核）："
                + "；".join(result.errors[:5])
            )
            self.rt.log(f"[fact_checker] {message}")
            return {
                "fact_check": result.to_dict(),
                "next_action": "accept",
                "warnings": [message],
                "trace": [base_trace],
            }

        self.rt.log(
            f"[fact_checker] 未通过（{len(result.errors)} 项错误）→ 打回 chief_editor 重写"
        )
        for item in result.errors[:5]:
            self.rt.log(f"    · {item}")
        return {
            "fact_check": result.to_dict(),
            "next_action": "rewrite",
            "retry_count": current_retry + 1,
            "audit_feedback": result.errors,
            "trace": [base_trace],
        }

    # ------------------------------------------------------------------
    # 通用 LLM 节点
    # ------------------------------------------------------------------
    def _run_agent(
        self,
        state: AgentState,
        *,
        tag: str,
        state_key: str,
        label: str,
        system: str,
        extra_user: str,
        critical: bool = False,
    ) -> dict[str, Any]:
        started = time.perf_counter()
        self.rt.log(f"[{tag}] 开始（{label}）……")
        user_message = build_fact_pack_message(state["fact_pack"]) + "\n\n" + extra_user
        try:
            payload, result = self.rt.client.chat_json(system, user_message, tag=tag)
        except LLMError as first_error:
            # 截断 / 非法 JSON 是最常见失败，先用"精简输出"指令重试一次
            self.rt.log(f"[{tag}] 首次失败（{mask_secret(str(first_error))[:80]}…），压缩重试……")
            try:
                payload, result = self.rt.client.chat_json(
                    system, user_message + _COMPACT_HINT, tag=f"{tag}:retry"
                )
            except LLMError as second_error:
                message = mask_secret(f"{label} 重试后仍失败：{second_error}")
                self.rt.log(f"[{tag}] {message}")
                if critical:
                    raise AgentExecutionError(
                        f"{label}（{tag}）在压缩重试后仍无法产出合法 JSON；"
                        f"该节点的结构化输出是下游契约的一部分（如 footnote_queries 供 Phase 3 使用），"
                        f"因此中止而不是降级。原始错误：{message}"
                    ) from second_error
                return {
                    state_key: {"error": message},
                    "warnings": [message],
                    "trace": [
                        {
                            "node": tag,
                            "event": "failed",
                            "at": _now(),
                            "error": message,
                            "elapsed_s": round(time.perf_counter() - started, 3),
                        }
                    ],
                }

        elapsed = time.perf_counter() - started
        truncated_note = "（finish_reason=length，输出可能被截断）" if result.truncated else ""
        self.rt.log(
            f"[{tag}] 完成：{len(payload)} 个字段 / {result.total_tokens} tokens / "
            f"{elapsed:.2f}s{truncated_note}"
        )
        warnings: list[str] = []
        if result.truncated:
            warnings.append(f"{label} 输出触发 max_tokens 上限，字段可能不完整。")
        return {
            state_key: payload,
            "usage": [result.usage_record(tag)],
            "warnings": warnings,
            "trace": [
                {
                    "node": tag,
                    "event": "analysis_ready",
                    "at": _now(),
                    "label": label,
                    "fields": sorted(payload.keys()),
                    "elapsed_s": round(elapsed, 3),
                    "latency_s": round(result.latency_s, 3),
                    "total_tokens": result.total_tokens,
                    "mode": result.mode,
                    "finish_reason": result.finish_reason,
                    "truncated": result.truncated,
                }
            ],
        }


def route_after_fact_check(state: AgentState) -> str:
    """条件边：``rewrite`` 回到 chief_editor，``accept`` 结束。"""
    return state.get("next_action") or "accept"
