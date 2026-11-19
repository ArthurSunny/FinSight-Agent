"""Phase 4：机构级 Streamlit 投研工作台。

模块职责
--------
* :mod:`src.ui.theme`      —— 配色与全局 CSS（券商深蓝 / 石板灰 / 琥珀金 / 警示红）
* :mod:`src.ui.charts`     —— Plotly 交互图表（**只读** FinancialReport，绝不重算指标）
* :mod:`src.ui.exporter`   —— 自包含可打印 HTML 报告导出（内联 CSS + @media print A4）
* :mod:`src.ui.state`      —— 历史存档扫描、报告回放、PDF 检索器缓存
* :mod:`src.ui.components` —— KPI 条、排雷规则卡片、附注证据卡片、可观测性面板

**铁律 1 在 UI 层的延伸**：本包只做"读取 + 渲染"，
不得出现任何财务比率计算（除法、求和、复利一律禁止），所有数字均来自
:class:`src.tools.schemas.FinancialReport` / ``PeerComparison`` / ``rag_evidences``。
"""

from __future__ import annotations

__all__ = ["charts", "components", "exporter", "state", "theme"]
