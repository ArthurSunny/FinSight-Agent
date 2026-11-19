"""``src`` 包根。

FinSight-Agent：基于多智能体的上市公司财报深度解构与财务排雷系统。

当前阶段（Phase 1）仅实现：
* :mod:`src.tools.financial_data`  —— 财务数据获取与本地缓存
* :mod:`src.tools.financial_engine` —— 财务指标计算与会计排雷引擎

``src.agents`` 与 ``src.rag`` 为 Phase 2 / Phase 3 预留，暂不含实现（见 AGENTS.md）。
"""

from __future__ import annotations

__all__ = ["__version__"]

__version__ = "1.0.0"
