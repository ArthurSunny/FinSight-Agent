"""FinSight-Agent 全局配置。

集中管理路径、缓存策略、单位换算与默认参数，避免散落在各模块中的魔法数字。
"""

from __future__ import annotations

import os
from pathlib import Path

# ---------------------------------------------------------------------------
# 路径
# ---------------------------------------------------------------------------
PROJECT_ROOT: Path = Path(__file__).resolve().parents[1]
DATA_DIR: Path = PROJECT_ROOT / "data"
CACHE_DIR: Path = DATA_DIR / "cache"
STATEMENT_CACHE_DIR: Path = CACHE_DIR / "statements"
REPORT_OUTPUT_DIR: Path = DATA_DIR / "reports"
#: Phase 3：年报 PDF 落地目录（本地优先通道也从这里找）
PDF_DIR: Path = DATA_DIR / "pdfs"
#: Phase 3：解析后的 Chunk 缓存（二次运行毫秒级加载）
RAG_CACHE_DIR: Path = CACHE_DIR / "rag"

# ---------------------------------------------------------------------------
# 单位换算
# ---------------------------------------------------------------------------
#: 数据源（东方财富）金额单位为「元」，输出统一换算为「亿元」
YUAN_PER_YI: float = 1e8

#: 金额输出保留小数位（亿元，4 位 = 万元级精度）
AMOUNT_DECIMALS: int = 4
#: 比率输出保留小数位（0.9193 = 91.93%）
RATIO_DECIMALS: int = 6

# ---------------------------------------------------------------------------
# 缓存
# ---------------------------------------------------------------------------
#: 原始三表缓存有效期（小时）。过期后自动重新拉取，避免年报更新后读到旧数据。
DEFAULT_CACHE_TTL_HOURS: float = float(os.getenv("FINSIGHT_CACHE_TTL_HOURS", "12"))

# ---------------------------------------------------------------------------
# 分析窗口
# ---------------------------------------------------------------------------
#: 默认展示的完整会计年度数量（近 4 年）
DEFAULT_LOOKBACK_YEARS: int = 4
#: 允许的最大回溯年数（防止一次性拉取过多历史数据）
MAX_LOOKBACK_YEARS: int = 10
#: 计算同比 / 杜邦均值 / Beneish 指数至少需要的前置年度数量
MIN_REQUIRED_YEARS: int = 2

# ---------------------------------------------------------------------------
# 数值容差
# ---------------------------------------------------------------------------
#: 分母绝对值低于该阈值时视为 0，比率短路返回 None（铁律 3）
ZERO_DENOMINATOR_EPS: float = 1e-9

ENGINE_VERSION: str = "1.0.0"


def ensure_directories() -> None:
    """确保运行期所需目录存在（幂等）。"""
    for path in (STATEMENT_CACHE_DIR, REPORT_OUTPUT_DIR, PDF_DIR, RAG_CACHE_DIR):
        path.mkdir(parents=True, exist_ok=True)
