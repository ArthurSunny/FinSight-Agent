"""Phase 2 主入口：LangGraph 多智能体协同投研与排雷编排。

工作流拓扑
----------
::

    START
      │
      ▼
    data_loader  ── 确定性装载 FinancialReport / FactPack / 同业对标
      │
      ├──────────────┬──────────────┐          （并行 fan-out，同一 superstep）
      ▼              ▼              ▼
  fundamental_    forensic_      peer_valuation_
   analyst         auditor         analyst
      │              │              │
      └──────────────┴──────────────┘          （fan-in，等待三者全部完成）
                     ▼
               chief_editor  ── 六章节 Markdown 纪要
                     ▼
               fact_checker  ── 纯 Python 质检
                     │
        ┌────────────┴────────────┐
        ▼ rewrite                 ▼ accept
  chief_editor                 END
  （retry_count < 2）

用法
----
::

    python -m src.agents.graph 600519 --peers 000858,000568
    python -m src.agents.graph 600519 --no-peers
    python -m src.agents.graph 600519 --offline        # 无密钥时的确定性降级
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, Sequence

from langgraph.graph import END, START, StateGraph

from src.agents.context_formatter import FactPackBuilder
from src.agents.llm_client import DeepSeekClient, LLMConfigError, mask_secret
from src.agents.nodes import (
    AgentState,
    WorkflowNodes,
    WorkflowRuntime,
    route_after_fact_check,
)
from src.config import REPORT_OUTPUT_DIR, ensure_directories
from src.tools.peer_comparison import PeerComparison
from src.tools.schemas import FinancialReport

#: 图的最大超步数（含 2 次重写回路后仍有余量）
RECURSION_LIMIT = 30


# ---------------------------------------------------------------------------
# 运行结果
# ---------------------------------------------------------------------------
@dataclass
class WorkflowResult:
    """一次完整编排的产出。"""

    stock_code: str
    stock_name: str
    latest_period: str
    markdown: str
    fact_check: dict
    retry_count: int
    elapsed_s: float
    report: Optional[FinancialReport] = None
    peer: Optional[PeerComparison] = None
    fundamental: dict = field(default_factory=dict)
    forensic: dict = field(default_factory=dict)
    peer_analysis: dict = field(default_factory=dict)
    rag_evidences: dict = field(default_factory=dict)
    rag_meta: dict = field(default_factory=dict)
    usage: list[dict] = field(default_factory=list)
    trace: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    markdown_path: Optional[Path] = None
    trace_path: Optional[Path] = None
    fact_pack_path: Optional[Path] = None
    mode: str = "api"

    # -- 汇总 ---------------------------------------------------------------
    @property
    def total_tokens(self) -> int:
        return sum(int(item.get("total_tokens", 0)) for item in self.usage)

    @property
    def prompt_tokens(self) -> int:
        return sum(int(item.get("prompt_tokens", 0)) for item in self.usage)

    @property
    def completion_tokens(self) -> int:
        return sum(int(item.get("completion_tokens", 0)) for item in self.usage)

    @property
    def passed(self) -> bool:
        return bool(self.fact_check.get("passed"))

    def usage_table(self) -> list[dict]:
        return list(self.usage)

    def to_dict(self) -> dict[str, Any]:
        return {
            "stock_code": self.stock_code,
            "stock_name": self.stock_name,
            "latest_period": self.latest_period,
            "mode": self.mode,
            "elapsed_s": round(self.elapsed_s, 3),
            "retry_count": self.retry_count,
            "fact_check_passed": self.passed,
            "fact_check": self.fact_check,
            "tokens": {
                "prompt": self.prompt_tokens,
                "completion": self.completion_tokens,
                "total": self.total_tokens,
                "calls": len(self.usage),
            },
            "usage": self.usage,
            "trace": self.trace,
            "warnings": self.warnings,
            "expert_outputs": {
                "fundamental": self.fundamental,
                "forensic": self.forensic,
                "peer_analysis": self.peer_analysis,
            },
            "rag_meta": self.rag_meta,
            "rag_evidences": self.rag_evidences,
            "peer_comparison": (
                self.peer.model_dump(mode="json") if self.peer is not None else None
            ),
            "artifacts": {
                "markdown": str(self.markdown_path) if self.markdown_path else None,
                "trace": str(self.trace_path) if self.trace_path else None,
                "fact_pack": str(self.fact_pack_path) if self.fact_pack_path else None,
            },
        }


# ---------------------------------------------------------------------------
# 图构建
# ---------------------------------------------------------------------------
def build_graph(runtime: WorkflowRuntime) -> Any:
    """装配并编译 LangGraph 状态图。"""
    nodes = WorkflowNodes(runtime)

    builder = StateGraph(AgentState)
    builder.add_node("data_loader", nodes.data_loader_node)
    builder.add_node("fundamental_analyst", nodes.fundamental_analyst_node)
    builder.add_node("forensic_auditor", nodes.forensic_auditor_node)
    builder.add_node("peer_valuation_analyst", nodes.peer_valuation_analyst_node)
    builder.add_node("footnote_rag_verifier", nodes.footnote_rag_verifier_node)
    builder.add_node("chief_editor", nodes.chief_editor_node)
    builder.add_node("fact_checker", nodes.fact_checker_node)

    builder.add_edge(START, "data_loader")

    # 并行 fan-out：三个专业角色共享同一份事实包
    builder.add_edge("data_loader", "fundamental_analyst")
    builder.add_edge("data_loader", "forensic_auditor")
    builder.add_edge("data_loader", "peer_valuation_analyst")

    # Phase 3：审计师产出 footnote_queries 后，附注穿透核查紧接着执行，
    # 并与基本面/同业两支并行，不额外拖长墙钟时间。
    builder.add_edge("forensic_auditor", "footnote_rag_verifier")

    # 并行 fan-in：等待三支全部完成后再撰稿（列表形式即"等待全部"语义）
    builder.add_edge(
        ["fundamental_analyst", "peer_valuation_analyst", "footnote_rag_verifier"],
        "chief_editor",
    )

    builder.add_edge("chief_editor", "fact_checker")
    builder.add_conditional_edges(
        "fact_checker",
        route_after_fact_check,
        {"rewrite": "chief_editor", "accept": END},
    )
    return builder.compile()


# ---------------------------------------------------------------------------
# 编排器
# ---------------------------------------------------------------------------
class AgentWorkflow:
    """多智能体工作流的高层封装（构建 → 运行 → 落盘）。"""

    def __init__(
        self,
        *,
        client: Optional[DeepSeekClient] = None,
        offline: bool = False,
        max_retries: int = 2,
        verbose: bool = True,
        include_raw_in_pack: bool = True,
    ) -> None:
        self.client = client or DeepSeekClient(force_mode="offline" if offline else None)
        self.verbose = verbose
        self.runtime = WorkflowRuntime(
            client=self.client,
            fact_builder=FactPackBuilder(include_raw=include_raw_in_pack),
            max_retries=max_retries,
            verbose=verbose,
        )
        self.graph = build_graph(self.runtime)

    # -- 运行 ---------------------------------------------------------------
    def run(
        self,
        stock_code: str,
        *,
        peer_codes: Optional[list[str]] = None,
        lookback_years: int = 4,
        rag_enabled: bool = True,
        pdf_path: Optional[str] = None,
        rag_top_k: int = 4,
        save: bool = True,
        on_event: Optional[Any] = None,
    ) -> WorkflowResult:
        """执行一次完整编排。

        Parameters
        ----------
        on_event:
            可选回调 ``(node_name, update, merged_state)``。传入时改用
            ``graph.stream(stream_mode="updates")`` 以获得**逐节点**进度，
            供 Streamlit 的 ``st.status`` 实时展示；不传则走 ``invoke``（更快）。
        """
        code = "".join(ch for ch in str(stock_code) if ch.isdigit())
        initial: AgentState = {
            "stock_code": code,
            "peer_codes": peer_codes,
            "include_peers": peer_codes != [],
            "lookback_years": int(lookback_years),
            "rag_enabled": bool(rag_enabled),
            "pdf_path": pdf_path,
            "rag_top_k": int(rag_top_k),
            "retry_count": 0,
            "usage": [],
            "trace": [],
            "warnings": [],
        }

        started = time.perf_counter()
        if on_event is None:
            final_state: dict[str, Any] = self.graph.invoke(
                initial, config={"recursion_limit": RECURSION_LIMIT}
            )
        else:
            final_state = self._stream(initial, on_event)
        elapsed = time.perf_counter() - started

        report: Optional[FinancialReport] = final_state.get("report")
        peer: Optional[PeerComparison] = final_state.get("peer_comparison")
        result = WorkflowResult(
            stock_code=report.meta.stock_code if report else code,
            stock_name=report.meta.stock_name if report else code,
            latest_period=report.meta.latest_report_period if report else "",
            markdown=final_state.get("report_markdown", ""),
            fact_check=final_state.get("fact_check", {}),
            retry_count=int(final_state.get("retry_count", 0)),
            elapsed_s=elapsed,
            report=report,
            peer=peer,
            fundamental=dict(final_state.get("fundamental") or {}),
            forensic=dict(final_state.get("forensic") or {}),
            peer_analysis=dict(final_state.get("peer_analysis") or {}),
            rag_evidences=dict(final_state.get("rag_evidences") or {}),
            rag_meta=dict(final_state.get("rag_meta") or {}),
            usage=list(final_state.get("usage", [])),
            trace=list(final_state.get("trace", [])),
            warnings=list(final_state.get("warnings", [])),
            mode="offline" if not self.client.available else "api",
        )

        if save:
            self._save_artifacts(result, final_state.get("fact_pack", ""))
        return result

    def _stream(
        self,
        initial: AgentState,
        on_event: Any,
    ) -> dict[str, Any]:
        """逐节点流式执行并手工合并状态（等价于 LangGraph 的 reducer 语义）。"""
        merged: dict[str, Any] = dict(initial)
        for key in ("usage", "trace", "warnings"):
            merged[key] = list(initial.get(key) or [])

        for chunk in self.graph.stream(
            initial,
            config={"recursion_limit": RECURSION_LIMIT},
            stream_mode="updates",
        ):
            if not isinstance(chunk, dict):
                continue
            for node_name, update in chunk.items():
                if not isinstance(update, dict):
                    continue
                for key, value in update.items():
                    if key in {"usage", "trace", "warnings"}:
                        merged[key] = list(merged.get(key) or []) + list(value or [])
                    else:
                        merged[key] = value
                try:
                    on_event(node_name, update, merged)
                except Exception:  # noqa: BLE001 - UI 回调异常不应中断编排
                    pass
        return merged

    # -- 落盘 ---------------------------------------------------------------
    @staticmethod
    def _save_artifacts(result: WorkflowResult, fact_pack: str) -> None:
        ensure_directories()
        REPORT_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        stem = f"{result.stock_code}_{result.latest_period or 'unknown'}"

        markdown_path = REPORT_OUTPUT_DIR / f"{stem}_tearsheet.md"
        evidences = (result.rag_evidences or {}).get("evidences") or []
        rag_verified = sum(1 for item in evidences if item.get("verdict") != "inconclusive")
        header = (
            f"<!-- FinSight-Agent v1.0 | mode={result.mode} | "
            f"fact_check={'passed' if result.passed else 'failed'} | "
            f"rag_verified={rag_verified}/{len(evidences)} | "
            f"retries={result.retry_count} | tokens={result.total_tokens} | "
            f"elapsed={result.elapsed_s:.2f}s | generated={dt.datetime.now().isoformat(timespec='seconds')} -->\n"
        )
        markdown_path.write_text(header + result.markdown.strip() + "\n", encoding="utf-8")

        trace_path = REPORT_OUTPUT_DIR / f"{stem}_agent_trace.json"
        trace_path.write_text(
            json.dumps(result.to_dict(), ensure_ascii=False, indent=2, allow_nan=False),
            encoding="utf-8",
        )

        fact_pack_path = REPORT_OUTPUT_DIR / f"{stem}_fact_pack.txt"
        fact_pack_path.write_text(fact_pack, encoding="utf-8")

        result.markdown_path = markdown_path
        result.trace_path = trace_path
        result.fact_pack_path = fact_pack_path


# ---------------------------------------------------------------------------
# 便捷函数
# ---------------------------------------------------------------------------
def run_workflow(
    stock_code: str,
    *,
    peer_codes: Optional[list[str]] = None,
    lookback_years: int = 4,
    offline: bool = False,
    max_retries: int = 2,
    verbose: bool = True,
) -> WorkflowResult:
    """一步跑完整条流水线。"""
    workflow = AgentWorkflow(offline=offline, max_retries=max_retries, verbose=verbose)
    return workflow.run(
        stock_code, peer_codes=peer_codes, lookback_years=lookback_years, save=True
    )


# ---------------------------------------------------------------------------
# 命令行
# ---------------------------------------------------------------------------
def _parse_peers(raw: Optional[str]) -> Optional[list[str]]:
    if raw is None:
        return None
    items = [item.strip() for item in raw.replace("，", ",").split(",") if item.strip()]
    return items


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m src.agents.graph",
        description="FinSight-Agent Phase 2：LangGraph 多智能体财报解构与排雷",
    )
    parser.add_argument("stock_code", help="6 位 A 股代码，如 600519")
    parser.add_argument(
        "--peers",
        default=None,
        help='同业代码，逗号分隔，如 "000858,000568"；不传则使用内置默认池',
    )
    parser.add_argument("--no-peers", action="store_true", help="跳过同业对比（单股快速模式）")
    parser.add_argument("--lookback", type=int, default=4, help="回溯年度数（默认 4）")
    parser.add_argument("--offline", action="store_true", help="离线确定性模式（不调用 LLM）")
    parser.add_argument("--max-retries", type=int, default=2, help="质检不通过时的最大重写次数")
    parser.add_argument("--quiet", action="store_true", help="只输出最终摘要")
    parser.add_argument("--show-report", action="store_true", help="在终端打印完整 Markdown 报告")
    parser.add_argument(
        "--no-rag",
        action="store_true",
        help="关闭年报附注穿透核查（不回退到 Phase 2 第六章形态）",
    )
    parser.add_argument(
        "--pdf",
        default=None,
        help="指定本地年报 PDF 路径（跳过本地目录搜索，但仍会推断报告期）",
    )
    parser.add_argument("--top-k", type=int, default=4, help="每条疑点检索的附注证据数（默认 4）")
    return parser


def _print_summary(result: WorkflowResult) -> None:
    print()
    print("=" * 88)
    print("FinSight-Agent Phase 2 · 运行摘要")
    print("=" * 88)
    print(f"标的        : {result.stock_code} {result.stock_name}（{result.latest_period}）")
    print(f"运行模式    : {result.mode}")
    print(f"总耗时      : {result.elapsed_s:.2f}s")
    print(f"质检结果    : {'通过' if result.passed else '未通过（已按最大重试次数放行）'}"
          f" | 重写次数 {result.retry_count}")
    stats = result.fact_check.get("stats", {})
    print(f"锚点命中    : {stats.get('anchors_ok', 0)}/{stats.get('anchors', 0)}"
          f" | 章节完整 {stats.get('sections_ok', 0)}/6 | 报告字数 {stats.get('chars', 0)}")
    print(f"Token 合计  : {result.total_tokens}（prompt {result.prompt_tokens} / "
          f"completion {result.completion_tokens}，共 {len(result.usage)} 次调用）")
    # ---- Phase 3：附注穿透 ----
    meta = result.rag_meta or {}
    evidences = (result.rag_evidences or {}).get("evidences") or []
    if meta.get("available"):
        verified = sum(1 for item in evidences if item.get("verdict") != "inconclusive")
        print(f"附注穿透    : {meta.get('pdf_period')} PDF {meta.get('page_count')} 页 → "
              f"{meta.get('chunks')} 切片（附注 {meta.get('note_chunks')}，含表 {meta.get('chunks_with_table')}）；"
              f"{len(evidences)} 条结论（已证实 {verified}）")
        for account, pages in (meta.get("top_pages") or {}).items():
            print(f"    · {account:<14} 命中 {', '.join(str(p) for p in pages)}")
    else:
        print(f"附注穿透    : 未启用/不可用（{meta.get('reason') or '无 PDF'}）")
    if result.usage:
        print("  " + "-" * 84)
        for item in result.usage:
            print(
                f"  {item.get('tag', '?'):<16} prompt={item.get('prompt_tokens', 0):<6} "
                f"completion={item.get('completion_tokens', 0):<6} "
                f"total={item.get('total_tokens', 0):<6} "
                f"latency={item.get('latency_s', 0):.2f}s"
            )
    if result.warnings:
        print("  警告：")
        for item in result.warnings:
            print(f"    · {mask_secret(item)}")
    print(f"报告文件    : {result.markdown_path}")
    print(f"轨迹文件    : {result.trace_path}")
    print(f"事实包      : {result.fact_pack_path}")


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    peer_codes: Optional[list[str]] = _parse_peers(args.peers)
    if args.no_peers:
        peer_codes = []

    try:
        workflow = AgentWorkflow(
            offline=args.offline, max_retries=args.max_retries, verbose=not args.quiet
        )
        if not args.quiet:
            print(f"[init] LLM: {workflow.client.status_text()}")
            if not workflow.client.available and not args.offline:
                print(
                    "[warn] 未检测到可用 DEEPSEEK_API_KEY，自动进入离线确定性模式。"
                    "请在 .env 配置后重跑以获得真实模型分析。"
                )
        result = workflow.run(
            args.stock_code,
            peer_codes=peer_codes,
            lookback_years=args.lookback,
            rag_enabled=not args.no_rag,
            pdf_path=args.pdf,
            rag_top_k=args.top_k,
        )
    except LLMConfigError as exc:
        print(f"[ERROR] {mask_secret(str(exc))}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - CLI 兜底，保证不打印密钥
        print(f"[ERROR] {mask_secret(f'{type(exc).__name__}: {exc}')}", file=sys.stderr)
        return 1

    if args.show_report:
        print()
        print(result.markdown)
    _print_summary(result)
    return 0 if result.passed else 0  # 报告已落盘即视为运行成功；质检详情见摘要


if __name__ == "__main__":
    raise SystemExit(main())
