"""数值安全工具与结构化警告收集器 —— 铁律 3（容错与防御性编程）的落地组件。

为什么单独成模块
----------------
财报数据里 ``NaN`` / ``None`` / 分母为 0 是常态而非异常。如果每个计算点各写一遍
``if pd.isna(x)``，必然遗漏。这里提供两个统一出口：

1. :class:`WarningCollector` —— 任何"算不出来"都必须登记，最终进入报告 ``warnings``；
2. :func:`finite_or_none` / :func:`round_amount` / :func:`round_ratio` ——
   保证输出模型里**只可能出现有限浮点数或 None**，从而 ``json.dumps(allow_nan=False)``
   永远不会失败（避免 NaN 污染喂给 LLM 的 JSON）。
"""

from __future__ import annotations

import math
from typing import Iterable, Optional

from src.config import AMOUNT_DECIMALS, RATIO_DECIMALS
from src.tools.schemas import EngineWarning


# ---------------------------------------------------------------------------
# 数值安全
# ---------------------------------------------------------------------------
def finite_or_none(value: object) -> Optional[float]:
    """把任意输入收敛为"有限浮点数或 None"。

    * ``None`` / ``NaN`` / ``±inf`` / 不可转换对象 → ``None``
    * 其余 → ``float``

    这是所有指标计算的最后一道闸门：模型字段拿到的要么是真实数字，要么是 None，
    绝不会是 NaN（NaN 无法被 JSON 标准序列化，会静默污染下游）。
    """
    if value is None:
        return None
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def _normalize_zero(number: float) -> float:
    """把 -0.0 归一为 0.0，避免报告里出现 "-0.0000000000" 这类视觉噪音。"""
    return 0.0 if number == 0 else number


def round_amount(value: object) -> Optional[float]:
    """金额四舍五入到「亿元」的展示精度（4 位小数 = 万元级）。"""
    number = finite_or_none(value)
    return None if number is None else _normalize_zero(round(number, AMOUNT_DECIMALS))


def round_ratio(value: object) -> Optional[float]:
    """比率四舍五入（6 位小数，即精确到 0.0001%）。"""
    number = finite_or_none(value)
    return None if number is None else _normalize_zero(round(number, RATIO_DECIMALS))


# ---------------------------------------------------------------------------
# 警告收集
# ---------------------------------------------------------------------------
class WarningCollector:
    """去重后的结构化警告收集器。

    Parameters
    ----------
    code:
        股票代码，会写入警告的上下文，方便在长日志里定位。
    max_items:
        上限，防止极端脏数据把报告撑爆。
    """

    def __init__(self, code: str = "", max_items: int = 400) -> None:
        self.code = code
        self.max_items = max_items
        self._items: list[EngineWarning] = []
        self._seen: set[tuple[str, Optional[str], Optional[str]]] = set()

    # -- 写入 ---------------------------------------------------------------
    def add(
        self,
        code: str,
        message: str,
        *,
        level: str = "warning",
        period: Optional[str] = None,
        field: Optional[str] = None,
    ) -> None:
        """登记一条警告（相同 code+period+field 只记一次，避免刷屏）。"""
        key = (code, period, field)
        if key in self._seen or len(self._items) >= self.max_items:
            return
        self._seen.add(key)
        self._items.append(
            EngineWarning(
                code=code,
                level="info" if level == "info" else "warning",
                period=period,
                field=field,
                message=message,
            )
        )

    def missing(self, table: str, column: str, period: str, *, label: str = "") -> None:
        """登记"科目缺失"（最常见的警告）。"""
        name = f"{label}({column})" if label else column
        self.add(
            "missing_item",
            f"报告期 {period} 的科目 {name} 在数据源 {table} 中为空，相关指标返回 null（不填充 0）。",
            period=period,
            field=column,
        )

    def zero_denominator(self, label: str, period: str, denominator: Optional[float] = None) -> None:
        """登记"分母为 0 或近似 0"，比率短路返回 null。"""
        tail = "" if denominator is None else f"（分母={denominator:.6g}）"
        self.add(
            "zero_denominator",
            f"报告期 {period} 的指标「{label}」分母为 0 或近似 0{tail}，比率无经济意义，返回 null。",
            period=period,
            field=label,
        )

    def not_applicable(self, label: str, period: str, reason: str) -> None:
        """登记"口径不适用"（如金融类公司不适用毛利率）。"""
        self.add(
            "not_applicable",
            f"报告期 {period} 的指标「{label}」不适用：{reason}",
            level="info",
            period=period,
            field=label,
        )

    def extend(self, messages: Iterable[str], *, code: str = "general") -> None:
        for index, message in enumerate(messages):
            self.add(f"{code}_{index}", message)

    # -- 读取 ---------------------------------------------------------------
    @property
    def items(self) -> list[EngineWarning]:
        return list(self._items)

    def __len__(self) -> int:
        return len(self._items)

    def count_by_level(self, level: str) -> int:
        return sum(1 for item in self._items if item.level == level)

    def merge(self, other: "WarningCollector") -> None:
        """合并另一个收集器（数据层 → 引擎层）。"""
        for item in other._items:
            self.add(
                item.code,
                item.message,
                level=item.level,
                period=item.period,
                field=item.field,
            )
