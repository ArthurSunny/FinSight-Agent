"""``src.tools`` —— FinSight-Agent 的数据层与计算引擎。

模块职责（严格单向依赖，禁止反向 import）：

* :mod:`src.tools.schemas`          —— Pydantic 结构化模型（唯一的数据契约）
* :mod:`src.tools.diagnostics`      —— 数值安全工具 + 结构化警告收集
* :mod:`src.tools.financial_data`   —— akshare 取数 + CSV/JSON 本地缓存 + 列名归一
* :mod:`src.tools.financial_engine` —— ★ 四大维度财务计算与会计排雷
* :mod:`src.tools.cross_validation` —— 与外部指标源的独立交叉校验（可选）

导出采用 PEP 562 惰性加载：``import src.tools`` 不会连带导入 akshare 依赖，
既让 ``python -m src.tools.financial_engine`` 不产生重复导入告警，
也保证只做模型校验的单元测试无需联网。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # 仅供类型检查器与 IDE 补全，运行时不导入
    from src.tools.financial_engine import (
        FinancialEngine,
        RuleThresholds,
        analyze_stock,
        report_to_json,
        save_report,
    )
    from src.tools.schemas import FinancialReport

__all__ = [
    "FinancialEngine",
    "FinancialReport",
    "RuleThresholds",
    "analyze_stock",
    "report_to_json",
    "save_report",
]

_ENGINE_EXPORTS = frozenset(
    {"FinancialEngine", "RuleThresholds", "analyze_stock", "report_to_json", "save_report"}
)
_SCHEMA_EXPORTS = frozenset({"FinancialReport"})


def __getattr__(name: str) -> Any:
    """惰性导出（PEP 562）。"""
    if name in _ENGINE_EXPORTS:
        from src.tools import financial_engine

        return getattr(financial_engine, name)
    if name in _SCHEMA_EXPORTS:
        from src.tools import schemas

        return getattr(schemas, name)
    raise AttributeError(f"module 'src.tools' has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted(__all__)
