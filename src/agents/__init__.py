"""Phase 2：基于 LangGraph + DeepSeek 的多智能体协同投研与排雷编排。

本包**只消费** Phase 1 产出的 :class:`src.tools.schemas.FinancialReport`
（经 :mod:`src.agents.context_formatter` 转成零歧义 Fact Pack），
**不得**自行重算或口算任何财务数字（AGENTS.md 铁律 1）。

模块职责
--------
* :mod:`src.agents.context_formatter` —— 小数/百分数零歧义格式化 + 锚点 + warnings 聚合
* :mod:`src.agents.llm_client`        —— DeepSeek（openai 兼容）客户端，密钥脱敏、用量记账
* :mod:`src.agents.prompts`           —— 提示词与 JSON 契约（必选章节唯一来源）
* :mod:`src.agents.fact_check`        —— 确定性数值质检（章节/锚点/符号/单位/报告期）
* :mod:`src.agents.nodes`             —— 5 个节点 + 条件边路由
* :mod:`src.agents.graph`             —— 图装配、编排器、CLI
* :mod:`src.agents.offline_templates` —— 无密钥时的确定性降级模板

快速开始
--------
>>> from src.agents.graph import run_workflow
>>> result = run_workflow("600519", peer_codes=["000858", "000568"])  # doctest: +SKIP
>>> print(result.markdown_path)  # doctest: +SKIP
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # 仅类型检查时导入，避免 import 该包即拉起重依赖
    from src.agents.context_formatter import FactPack, FactPackBuilder
    from src.agents.graph import AgentWorkflow, WorkflowResult, build_graph, run_workflow
    from src.agents.llm_client import DeepSeekClient, LLMConfig, LLMResult

__all__ = [
    "AgentWorkflow",
    "DeepSeekClient",
    "FactPack",
    "FactPackBuilder",
    "LLMConfig",
    "LLMResult",
    "WorkflowResult",
    "build_graph",
    "run_workflow",
]

_GRAPH_EXPORTS = frozenset({"AgentWorkflow", "WorkflowResult", "build_graph", "run_workflow"})
_CLIENT_EXPORTS = frozenset({"DeepSeekClient", "LLMConfig", "LLMResult"})
_FORMAT_EXPORTS = frozenset({"FactPack", "FactPackBuilder"})


def __getattr__(name: str) -> Any:
    """惰性导出（PEP 562），避免 `import src.agents` 就加载 langgraph / openai。"""
    if name in _GRAPH_EXPORTS:
        from src.agents import graph as _graph

        return getattr(_graph, name)
    if name in _CLIENT_EXPORTS:
        from src.agents import llm_client

        return getattr(llm_client, name)
    if name in _FORMAT_EXPORTS:
        from src.agents import context_formatter

        return getattr(context_formatter, name)
    raise AttributeError(f"module 'src.agents' has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted(__all__)
