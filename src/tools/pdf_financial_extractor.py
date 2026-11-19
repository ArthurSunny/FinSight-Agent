"""本地年报 PDF → 标准化三大表（100% 纯 Python 确定性提取）。

为什么需要这一层
----------------
部分标的（新三板挂牌、北交所旧号段）在免费接口里**根本没有**标准化三大表
（见 AGENTS.md §2.7）。但它们的年报 PDF 是公开的，而且报表页的版式高度规范：
「项目 / 附注 / 本期 / 上期」四列、金额千分位、空白用「-」。因此可以**确定性**地把
PDF 还原成与东方财富接口完全同构的三张表，让下游引擎、多智能体、附注 RAG 全部复用。

铁律遵守
--------
* 铁律 1：本模块**只做文本→数字的搬运**，不计算任何财务比率；一切比率仍由
  :mod:`src.tools.financial_engine` 计算。
* 铁律 2：每个数字都带页码溯源（``source_pages``），并记录 PDF 文件名与抽取版本。
* 铁律 3：空白 / `-` / `—` **一律转 None**，绝不填 0；单位按表头「单位：元/万元」精确换算；
  跨表勾稽不一致只记警告，不改数。

实测依据（874886 映日科技 2025 年报，147 页）
--------------------------------------------
* 合并报表与母公司报表**交替出现**，必须按标题切片，否则会把母公司数字当成合并数字；
* 标签会**跨行折断**（``2.归属于母公司所有者的净利润（净亏损以`` + ``“-”号填列）``）；
* 摘要表「主要会计数据和财务指标」是唯一能拿到**扣非归母净利润**的地方；
* 新三板用「归属于挂牌公司股东的净利润 / 净资产」，正文报表里仍用「归属于母公司所有者」。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

from src.config import PDF_DIR, RAG_CACHE_DIR

#: 抽取器版本：变更解析规则时递增，缓存签名随之失效
EXTRACTOR_VERSION = "1.0.0"

#: 单位 → 换算为「元」的乘数
UNIT_MULTIPLIERS: dict[str, float] = {
    "元": 1.0,
    "千元": 1_000.0,
    "万元": 10_000.0,
    "百万元": 1_000_000.0,
    "亿元": 100_000_000.0,
}

#: 报表标题 → (表 key, 是否合并口径)
STATEMENT_HEADINGS: dict[str, tuple[str, bool]] = {
    "合并资产负债表": ("balance_sheet", True),
    "母公司资产负债表": ("balance_sheet", False),
    "合并利润表": ("profit_sheet", True),
    "母公司利润表": ("profit_sheet", False),
    "合并现金流量表": ("cash_flow_sheet", True),
    "母公司现金流量表": ("cash_flow_sheet", False),
}

#: 报表列 → 东方财富字段名（与 ``financial_data._COLUMN_MAP`` 对齐，保证下游零改动）
BALANCE_LABELS: dict[str, str] = {
    "货币资金": "MONETARYFUNDS",
    "应收票据": "NOTE_RECE",
    "应收账款": "ACCOUNTS_RECE",
    "预付款项": "PREPAYMENT",
    "存货": "INVENTORY",
    "其他流动资产": "OTHER_CURRENT_ASSET",
    "流动资产合计": "TOTAL_CURRENT_ASSETS",
    "固定资产": "FIXED_ASSET",
    "在建工程": "CIP",
    "无形资产": "INTANGIBLE_ASSET",
    "商誉": "GOODWILL",
    "资产总计": "TOTAL_ASSETS",
    "短期借款": "SHORT_LOAN",
    "一年内到期的非流动负债": "NONCURRENT_LIAB_1YEAR",
    "长期借款": "LONG_LOAN",
    "应付债券": "BOND_PAYABLE",
    "租赁负债": "LEASE_LIAB",
    "应付账款": "ACCOUNTS_PAYABLE",
    "合同负债": "CONTRACT_LIAB",
    "流动负债合计": "TOTAL_CURRENT_LIAB",
    "负债合计": "TOTAL_LIABILITIES",
    "归属于母公司所有者权益合计": "TOTAL_PARENT_EQUITY",
    "归属于母公司所有者权益（或股东权益）合计": "TOTAL_PARENT_EQUITY",
    "归属于母公司股东权益合计": "TOTAL_PARENT_EQUITY",
    "归属于挂牌公司股东的净资产": "TOTAL_PARENT_EQUITY",
    "所有者权益（或股东权益）合计": "TOTAL_EQUITY",
    "股东权益合计": "TOTAL_EQUITY",
    "少数股东权益": "MINORITY_EQUITY",
}

PROFIT_LABELS: dict[str, str] = {
    "营业总收入": "TOTAL_OPERATE_INCOME",
    "营业收入": "OPERATE_INCOME",
    "营业总成本": "TOTAL_OPERATE_COST",
    "营业成本": "OPERATE_COST",
    "税金及附加": "OPERATE_TAX_ADD",
    "销售费用": "SALE_EXPENSE",
    "管理费用": "MANAGE_EXPENSE",
    "研发费用": "RESEARCH_EXPENSE",
    "财务费用": "FINANCE_EXPENSE",
    "其他收益": "OTHER_INCOME",
    "投资收益": "INVEST_INCOME",
    "公允价值变动收益": "FAIRVALUE_CHANGE_INCOME",
    "信用减值损失": "CREDIT_IMPAIRMENT_LOSS",
    "资产减值损失": "ASSET_IMPAIRMENT_LOSS",
    "营业利润": "OPERATE_PROFIT",
    "利润总额": "TOTAL_PROFIT",
    "所得税费用": "INCOME_TAX",
    "净利润": "NETPROFIT",
    "归属于母公司所有者的净利润": "PARENT_NETPROFIT",
    "归属于挂牌公司股东的净利润": "PARENT_NETPROFIT",
    "少数股东损益": "MINORITY_INTEREST",
}

CASH_LABELS: dict[str, str] = {
    "销售商品、提供劳务收到的现金": "SALES_SERVICES",
    "经营活动产生的现金流量净额": "NETCASH_OPERATE",
    "投资活动产生的现金流量净额": "NETCASH_INVEST",
    "筹资活动产生的现金流量净额": "NETCASH_FINANCE",
    "购建固定资产、无形资产和其他长期资产支付的现金": "CONSTRUCT_LONG_ASSET",
    "期末现金及现金等价物余额": "END_CCE",
    "固定资产折旧、油气资产折耗、生产性生物资产折旧": "FA_IR_DEPR",
    "固定资产折旧": "FA_IR_DEPR",
    "无形资产摊销": "IA_AMORTIZE",
    "存货的减少（增加以“－”号填列）": "INVENTORY_REDUCE",
    "存货的减少": "INVENTORY_REDUCE",
}

#: 摘要表标签 → 字段（唯一能取到扣非归母净利润的地方）
SUMMARY_LABELS: dict[str, str] = {
    "营业收入": "OPERATE_INCOME",
    "营业总收入": "TOTAL_OPERATE_INCOME",
    "归属于挂牌公司股东的净利润": "PARENT_NETPROFIT",
    "归属于母公司所有者的净利润": "PARENT_NETPROFIT",
    "归属于挂牌公司股东的扣除非经常性损益后的净利润": "DEDUCT_PARENT_NETPROFIT",
    "扣除非经常性损益后的净利润": "DEDUCT_PARENT_NETPROFIT",
    "资产总计": "TOTAL_ASSETS",
    "负债总计": "TOTAL_LIABILITIES",
    "负债合计": "TOTAL_LIABILITIES",
    "归属于挂牌公司股东的净资产": "TOTAL_PARENT_EQUITY",
    "归属于母公司所有者的净资产": "TOTAL_PARENT_EQUITY",
}

#: 摘要表里出现的「比例类」行，不参与金额提取（避免把 19.63% 当成金额）
_SUMMARY_SKIP_TOKENS: tuple[str, ...] = ("%", "率", "每股", "比率", "倍数", "周转")

#: 需要从报表主体之外（附注·补充资料）补齐的字段
SECONDARY_FIELDS: frozenset[str] = frozenset({"FA_IR_DEPR", "IA_AMORTIZE", "INVENTORY_REDUCE"})

_RE_PAGE_NO = re.compile(r"^\d{1,3}$")
_RE_UNIT = re.compile(r"单位[：:]\s*(人民币)?\s*(亿元|百万元|万元|千元|元)")
_RE_NOTE_REF = re.compile(r"^[（(][一二三四五六七八九十百]+[）)]$")
_RE_COLUMN_HEADER = re.compile(
    r"^(项目|附注|本期|上期|年末余额|年初余额|本期期末|上年期末|本期金额|上期金额|"
    r"\d{4}\s*年(\d{1,2}\s*月(\d{1,2}\s*日)?)?|\d{4}\s*年度)$"
)
_RE_NUMBER = re.compile(r"^[（(]?-?\d[\d,]*(?:\.\d+)?[）)]?$")
_RE_DASH_ONLY = re.compile(r"^[-—－–]{1,2}$")
_RE_LEADING_INDEX = re.compile(
    r"^(?:[一二三四五六七八九十]+、|[（(][一二三四五六七八九十\d]+[）)]|\d+[.、．]|"
    r"其中[：:]|减[：:]|加[：:]|其他[：:])"
)
#: 只剥离这些**已知的**尾部修饰语，保留「（或股东权益）」这类语义括号
_TRAILING_QUALIFIERS: tuple[str, ...] = (
    "（损失以“-”号填列）",
    "（损失以“－”号填列）",
    "（亏损以“-”号填列）",
    "（亏损以“－”号填列）",
    "（亏损总额以“-”号填列）",
    "（亏损总额以“－”号填列）",
    "（净亏损以“-”号填列）",
    "（净亏损以“－”号填列）",
    "（增加以“－”号填列）",
    "（减少以“－”号填列）",
    "（增加以“-”号填列）",
    "（减少以“-”号填列）",
    "（元/股）",
    "（元／股）",
)


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------
@dataclass
class ExtractedPeriod:
    """一个会计年度的三大表原始数值（单位：元）。"""

    year: int
    period_name: str
    balance: dict[str, Optional[float]] = field(default_factory=dict)
    profit: dict[str, Optional[float]] = field(default_factory=dict)
    cash: dict[str, Optional[float]] = field(default_factory=dict)
    source_pages: dict[str, int] = field(default_factory=dict)

    def field_count(self) -> int:
        return sum(
            1
            for block in (self.balance, self.profit, self.cash)
            for value in block.values()
            if value is not None
        )


@dataclass
class PdfStatementExtraction:
    """一次 PDF 抽取的完整结果（含溯源与警告）。"""

    pdf_path: Path
    code: str
    stock_name: str
    report_year: int
    unit: str
    consolidated: bool
    periods: list[ExtractedPeriod]
    summary: dict[str, list[Optional[float]]]
    summary_pages: list[int] = field(default_factory=list)
    statement_pages: dict[str, list[int]] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    extractor_version: str = EXTRACTOR_VERSION

    @property
    def years(self) -> list[int]:
        return [period.year for period in self.periods]

    @property
    def field_count(self) -> int:
        return sum(period.field_count() for period in self.periods)

    def to_dict(self) -> dict[str, Any]:
        return {
            "pdf_path": str(self.pdf_path),
            "code": self.code,
            "stock_name": self.stock_name,
            "report_year": self.report_year,
            "unit": self.unit,
            "consolidated": self.consolidated,
            "summary_pages": self.summary_pages,
            "statement_pages": self.statement_pages,
            "warnings": self.warnings,
            "extractor_version": self.extractor_version,
            "summary": self.summary,
            "periods": [
                {
                    "year": item.year,
                    "period_name": item.period_name,
                    "balance": item.balance,
                    "profit": item.profit,
                    "cash": item.cash,
                    "source_pages": item.source_pages,
                }
                for item in self.periods
            ],
        }


# ---------------------------------------------------------------------------
# 文本与数字清洗
# ---------------------------------------------------------------------------
def normalize_label(raw: str) -> str:
    """把跨行/带序号的标签规整成可比对的科目名。

    >>> normalize_label("其中：营业收入")
    '营业收入'
    >>> normalize_label("2.归属于母公司所有者的净利润（净亏损以“-”号填列）")
    '归属于母公司所有者的净利润'
    """
    text = re.sub(r"\s+", "", str(raw or ""))
    text = text.replace("（", "（").replace("）", "）")
    for _ in range(3):  # 允许 "一、其中：xxx" 这类多重前缀
        new = _RE_LEADING_INDEX.sub("", text)
        if new == text:
            break
        text = new
    for qualifier in _TRAILING_QUALIFIERS:
        if text.endswith(qualifier):
            text = text[: -len(qualifier)]
    return text.rstrip("：:").strip()


def parse_amount(raw: str, multiplier: float = 1.0) -> Optional[float]:
    """把报表单元格解析成「元」为单位的 float；空白/短横一律返回 None（铁律 3）。

    >>> parse_amount("1,234.56")
    1234.56
    >>> parse_amount("(1,234.56)")
    -1234.56
    >>> parse_amount("-") is None
    True
    """
    text = str(raw or "").strip().replace("，", ",").replace(" ", "")
    if not text or _RE_DASH_ONLY.match(text) or text in {"-", "--", "—", "－"}:
        return None
    negative = False
    if (text.startswith("(") and text.endswith(")")) or (
        text.startswith("（") and text.endswith("）")
    ):
        negative = True
        text = text[1:-1]
    if text.startswith("-"):
        negative = True
        text = text[1:]
    elif text.startswith("－"):
        negative = True
        text = text[1:]
    if not _RE_NUMBER.match(text.replace(",", "")) and not re.fullmatch(
        r"\d[\d,]*(?:\.\d+)?", text
    ):
        return None
    try:
        value = float(text.replace(",", ""))
    except ValueError:
        return None
    return -value * multiplier if negative else value * multiplier


def _is_number_line(text: str) -> bool:
    return parse_amount(text) is not None


def normalized_map(label_map: dict[str, str]) -> dict[str, str]:
    """把标签表规整成「已归一化 → 字段名」，避免跨行/修饰语导致的漏匹配。"""
    return {normalize_label(key): value for key, value in label_map.items()}


def _looks_like_column_header(text: str) -> bool:
    compact = re.sub(r"\s+", "", text)
    return bool(_RE_COLUMN_HEADER.match(compact))


# ---------------------------------------------------------------------------
# 抽取器
# ---------------------------------------------------------------------------
class PdfFinancialExtractor:
    """把年报 PDF 的三大表还原为结构化数据。"""

    def __init__(self, verbose: bool = False) -> None:
        self.verbose = verbose
        self._log_lines: list[str] = []
        # 归整后的标签表：源字典保留可读的字面量，运行时统一用归一化版本比对
        self._balance_map = normalized_map(BALANCE_LABELS)
        self._profit_map = normalized_map(PROFIT_LABELS)
        self._cash_map = normalized_map(CASH_LABELS)
        self._secondary_map = {
            label: column
            for label, column in self._cash_map.items()
            if column in SECONDARY_FIELDS
        }

    def _log(self, message: str) -> None:
        self._log_lines.append(message)
        if self.verbose:
            print(f"[pdf_tables] {message}")

    # -- 对外入口 ---------------------------------------------------------
    def extract(
        self,
        pdf_path: Path | str,
        *,
        code: Optional[str] = None,
        report_year: Optional[int] = None,
    ) -> PdfStatementExtraction:
        import pymupdf  # 延迟导入

        path = Path(pdf_path)
        document = pymupdf.open(path)
        try:
            lines = self._collect_lines(document)
            # 先切报表区间（只依赖标题），再用表头日期锚定会计年度
            warnings: list[str] = []
            ranges = self._locate_statements(lines, warnings)
            identity = self._identify(
                lines, ranges=ranges, fallback_code=code, fallback_year=report_year
            )
            self._log(
                f"{path.name}：{identity['stock_name']}（{identity['code']}）"
                f"{identity['report_year']}年报｜{len(lines)} 行文本"
            )

            # ---- 1) 摘要表（唯一来源：扣非归母净利润） ----
            summary, summary_pages = self._parse_summary_table(document, identity, warnings)
            periods_from_summary = self._summary_years(identity["report_year"], summary)

            # ---- 2) 三大表 ----
            balance = self._parse_statement(
                lines, ranges.get("balance_sheet"), self._balance_map, "资产负债表", warnings
            )
            profit = self._parse_statement(
                lines, ranges.get("profit_sheet"), self._profit_map, "利润表", warnings
            )
            cash = self._parse_statement(
                lines, ranges.get("cash_flow_sheet"), self._cash_map, "现金流量表", warnings
            )
            secondary = self._parse_secondary(lines, cash, warnings)

            # ---- 3) 合并成年度序列 ----
            years = periods_from_summary or identity["years"]
            periods: list[ExtractedPeriod] = []
            for index, year in enumerate(years):
                period = ExtractedPeriod(
                    year=year,
                    period_name=f"{year}年报",
                    balance=self._pick_column(balance, index),
                    profit=self._pick_column(profit, index),
                    cash=self._pick_column(cash, index),
                )
                period.source_pages = self._collect_pages(ranges, index)
                periods.append(period)

            # ---- 4) 摘要表补齐 + 勾稽校验 ----
            self._merge_summary(periods, summary, warnings)
            self._apply_secondary(periods, secondary)
            self._validate(periods, warnings)

            pages = {
                key: sorted({line[0] for line in value})
                for key, value in ranges.items()
                if key.endswith("_sheet") and value
            }
            return PdfStatementExtraction(
                pdf_path=path,
                code=identity["code"],
                stock_name=identity["stock_name"],
                report_year=identity["report_year"],
                unit=identity["unit"],
                consolidated=ranges.get("consolidated", True),
                periods=periods,
                summary=summary,
                summary_pages=summary_pages,
                statement_pages=pages,
                warnings=warnings,
            )
        finally:
            document.close()

    # -- 文本收集 ---------------------------------------------------------
    @staticmethod
    def _collect_lines(document: Any) -> list[tuple[int, str]]:
        """把所有页的文本拆成 ``(页码, 行文本)``，保持文档顺序。"""
        collected: list[tuple[int, str]] = []
        for index in range(document.page_count):
            for raw in document[index].get_text("text").splitlines():
                text = raw.strip().replace("\u3000", " ")
                if not text or _RE_PAGE_NO.match(text):
                    continue
                collected.append((index + 1, text))
        return collected

    # -- 封面识别 ---------------------------------------------------------
    def _identify(
        self,
        lines: Sequence[tuple[int, str]],
        *,
        ranges: Optional[dict[str, Any]] = None,
        fallback_code: Optional[str] = None,
        fallback_year: Optional[int] = None,
        require_year: bool = True,
    ) -> dict[str, Any]:
        head = [text for page, text in lines if page <= 8]

        def _after(keyword: str) -> Optional[str]:
            for index, text in enumerate(head):
                if text.replace(" ", "").startswith(keyword):
                    tail = text.replace(keyword, "").strip(" ：:")
                    if tail:
                        return tail
                    for offset in (1, 2):  # 值常换行
                        if index + offset < len(head):
                            candidate = head[index + offset].strip(" ：:")
                            if candidate and not _RE_PAGE_NO.match(candidate):
                                return candidate
            return None

        code = None
        for keyword in ("证券代码", "股票代码"):
            value = _after(keyword)
            if value:
                match = re.search(r"(\d{6})", value)
                if match:
                    code = match.group(1)
                    break
        if code is None:  # 兜底：标签与代码同行的情形
            joined = "\n".join(head)
            match = re.search(r"(?:证券代码|股票代码)[：:]?\s*(\d{6})", joined)
            if match:
                code = match.group(1)
        if code is None:  # 再兜底：前 8 页里任何独占一行的 6 位数字
            for text in head:
                if re.fullmatch(r"\d{6}", text.strip()):
                    code = text.strip()
                    break
        stock_name = None
        for keyword in ("证券简称", "公司简称", "股票简称"):
            value = _after(keyword)
            if value and re.fullmatch(r"[\u4e00-\u9fa5A-Za-z0-9*·]{2,12}", value):
                stock_name = value
                break
        if not stock_name:
            value = _after("公司中文全称") or _after("公司名称")
            if value:
                stock_name = re.sub(r"(股份有限公司|有限公司)$", "", value) or value

        year = None
        # 优先用资产负债表表头的「20XX 年 12 月 31 日」——最权威的会计年度锚点
        if ranges:
            header_lines = [
                text.replace(" ", "") for _page, text in (ranges.get("balance_sheet") or ())[:40]
            ]
            years = [
                int(match.group(1))
                for text in header_lines
                for match in [re.search(r"(20\d{2})年12月31日", text)]
                if match
            ]
            if years:
                year = max(years)
        if year is None:
            for _page, text in lines[:600]:
                compact = text.replace(" ", "")
                match = re.search(r"(20\d{2})年年度报告", compact) or re.search(
                    r"(20\d{2})年度报告", compact
                )
                if match:
                    year = int(match.group(1))
                    break

        unit = "元"
        for page, text in lines:
            match = _RE_UNIT.search(text)
            if match:
                unit = match.group(2)
                break

        if year is None:
            for _page, text in lines[:3000]:
                compact = text.replace(" ", "")
                match = re.search(r"(20\d{2})年12月31日", compact) or re.search(
                    r"(20\d{2})年年度报告", compact
                )
                if match:
                    year = int(match.group(1))
                    break
        code = code or (fallback_code or "")
        if not code:
            raise ValueError("无法从 PDF 识别证券代码")
        year = year or fallback_year
        if not year and require_year:
            raise ValueError("无法从 PDF 识别报告年度")

        return {
            "code": code,
            "stock_name": stock_name or code,
            "report_year": int(year) if year else None,
            "unit": unit,
            "years": [int(year), int(year) - 1] if year else [],
        }

    # -- 报表区间 ---------------------------------------------------------
    def _locate_statements(
        self, lines: Sequence[tuple[int, str]], warnings: list[str]
    ) -> dict[str, Any]:
        """按标题把文档切成「合并/母公司 × 三表」六个区间。"""
        positions: list[tuple[int, str, bool]] = []
        for index, (page, text) in enumerate(lines):
            compact = text.replace(" ", "")
            if compact in STATEMENT_HEADINGS:
                kind, consolidated = STATEMENT_HEADINGS[compact]
                positions.append((index, kind, consolidated))
        if not positions:
            raise ValueError("未在 PDF 中找到「合并资产负债表 / 合并利润表 / 合并现金流量表」标题")

        ranges: dict[str, Any] = {}
        for order, (start, kind, consolidated) in enumerate(positions):
            end = positions[order + 1][0] if order + 1 < len(positions) else len(lines)
            if consolidated or kind not in ranges:
                ranges[kind] = lines[start + 1 : end]
                if consolidated:
                    ranges[f"{kind}_consolidated"] = True
        ranges["consolidated"] = all(
            ranges.get(f"{kind}_consolidated", False)
            for kind in ("balance_sheet", "profit_sheet", "cash_flow_sheet")
            if kind in ranges
        )
        missing = [
            name
            for name, key in (("资产负债表", "balance_sheet"), ("利润表", "profit_sheet"),
                              ("现金流量表", "cash_flow_sheet"))
            if key not in ranges
        ]
        if missing:
            warnings.append(f"PDF 中未定位到：{'、'.join(missing)}")
        self._log(
            "报表区间："
            + "；".join(
                f"{key} {len(ranges[key])} 行" for key in ranges if key.endswith("_sheet")
            )
        )
        return ranges

    # -- 单表解析 ---------------------------------------------------------
    def _parse_statement(
        self,
        lines: Sequence[tuple[int, str]],
        block: Optional[Sequence[tuple[int, str]]],
        label_map: dict[str, str],
        label: str,
        warnings: list[str],
    ) -> dict[str, list[Optional[float]]]:
        """把一段报表文本解析成 ``{字段: [本期, 上期]}``。"""
        if not block:
            return {}
        result = self._parse_two_columns(block, label_map)
        self._log(
            f"{label}：{sum(1 for pair in result.values() if pair[0] is not None)} 个科目"
            f"（{sum(1 for pair in result.values() if pair[1] is not None)} 个含上期数）"
        )
        if not result:
            warnings.append(f"{label}未能解析出任何科目，请检查 PDF 版式")
        return result

    def _parse_two_columns(
        self,
        block: Sequence[tuple[int, str]],
        label_map: dict[str, str],
    ) -> dict[str, list[Optional[float]]]:
        """双列扫描：标签后紧跟的 1~2 个数字分别是「本期 / 上期」。"""
        result: dict[str, list[Optional[float]]] = {}
        pending: list[str] = []
        last_column: Optional[str] = None
        index = 0
        total = len(block)
        while index < total:
            page, text = block[index]
            if _looks_like_column_header(text) or _RE_NOTE_REF.match(text):
                index += 1
                continue
            if _RE_DASH_ONLY.match(text):
                pending.clear()
                index += 1
                continue
            if not _is_number_line(text):
                pending.append(text)
                if len(pending) > 4:
                    pending = pending[-4:]
                index += 1
                continue

            # 数字行：连续收集 1~2 个数字
            numbers: list[Optional[float]] = []
            while index < total and _is_number_line(block[index][1]) and len(numbers) < 2:
                numbers.append(parse_amount(block[index][1]))
                index += 1
            column = self._match_label(pending or [""], label_map, last_column)
            pending.clear()
            if column is None or column in result:
                continue
            last_column = column
            result[column] = [numbers[0] if numbers else None,
                              numbers[1] if len(numbers) > 1 else None]
        return result

    def _match_label(
        self,
        pending: Sequence[str],
        label_map: dict[str, str],
        last_column: Optional[str],
    ) -> Optional[str]:
        """从「待定标签行」里**由远及近**拼接匹配，兼容跨行折断的标签。

        必须优先用最长跨度：``归属于母公司所有者权益（或股东`` + ``权益）合计``
        的**末行**恰好等于另一个合法标签 ``所有者权益（或股东权益）合计``，
        若先试短跨度，就会把归母权益错记成所有者权益合计（实测踩坑）。
        """
        context = last_column
        for span in range(min(len(pending), 4), 0, -1):
            candidate = normalize_label("".join(pending[-span:]))
            if not candidate:
                continue
            if candidate in label_map:
                return label_map[candidate]
            # 上下文相关：财务费用下的「利息费用 / 利息收入」
            if candidate in {"利息费用", "利息收入"} and context in {
                "FINANCE_EXPENSE",
                "FE_INTEREST_EXPENSE",
            }:
                return "FE_INTEREST_EXPENSE" if candidate == "利息费用" else "FE_INTEREST_INCOME"
        return None

    # -- 摘要表 -----------------------------------------------------------
    def _parse_summary_table(
        self,
        document: Any,
        identity: dict[str, Any],
        warnings: list[str],
    ) -> tuple[dict[str, list[Optional[float]]], list[int]]:
        summary: dict[str, list[Optional[float]]] = {}
        pages: list[int] = []
        keys = ("主要会计数据和财务指标", "主要会计数据", "主要财务数据")
        target_pages: list[int] = []
        for index in range(document.page_count):
            text = document[index].get_text("text")
            if any(key in text for key in keys):
                target_pages.append(index)
        for page_index in target_pages[:2]:
            page = document[page_index]
            multiplier = 1.0
            match = _RE_UNIT.search(page.get_text("text"))
            if match:
                multiplier = UNIT_MULTIPLIERS.get(match.group(2), 1.0)
            parsed = False
            for table in page.find_tables().tables:
                rows = table.extract()
                if not rows:
                    continue
                _header_index, value_columns = self._detect_summary_columns(rows)
                if value_columns is None:
                    continue
                for row in rows:
                    label = normalize_label(str(row[0] or ""))
                    if not label or label in summary:
                        continue
                    if any(token in label for token in _SUMMARY_SKIP_TOKENS):
                        continue
                    field_name = SUMMARY_LABELS.get(label)
                    if field_name is None:
                        continue
                    # 表头列号可能与数据行错位（合并单元格），
                    # 因此按「数据行里出现的第 1/2 个数值」取值，而不是照抄表头列号
                    numbers = [
                        value * multiplier for _index, value in self._row_numbers(row)
                    ]
                    pair = [
                        numbers[offset] if offset < len(numbers) else None for offset in range(2)
                    ]
                    if any(value is not None for value in pair):
                        summary[field_name] = pair
                        parsed = True
            if parsed:
                pages.append(page_index + 1)
        if not summary:
            warnings.append("未解析到「主要会计数据和财务指标」摘要表，扣非净利润将缺失")
        else:
            self._log(f"摘要表：{len(summary)} 项（页码 {pages}）")
        return summary, pages

    @staticmethod
    def _row_numbers(row: Sequence[Any]) -> list[tuple[int, float]]:
        """取一行里「标签之后」的数值单元格（顺序保留，跳过空白与百分数）。"""
        numbers: list[tuple[int, float]] = []
        for index, cell in enumerate(row):
            if index == 0:
                continue
            text = str(cell or "").strip()
            if not text or "%" in text or "％" in text:
                continue
            value = parse_amount(text)
            if value is not None:
                numbers.append((index, value))
        return numbers

    @staticmethod
    def _detect_summary_columns(rows: Sequence[Sequence[Any]]) -> tuple[int, Optional[list[int]]]:
        """确认这张表是否「主要会计数据和财务指标」（表头必须含本期/上年同期）。"""
        for header_index, row in enumerate(rows[:3]):
            cells = [re.sub(r"\s+", "", str(cell or "")) for cell in row]
            current = next(
                (i for i, cell in enumerate(cells) if cell in {"本期", "本期金额", "本期期末", "本年"}),
                None,
            )
            prior = next(
                (i for i, cell in enumerate(cells)
                 if cell in {"上年同期", "上期", "上期金额", "上年期末", "上年"}),
                None,
            )
            if current is not None and prior is not None:
                return header_index, [current, prior]
        return 0, None

    @staticmethod
    def _summary_years(
        report_year: int, summary: dict[str, list[Optional[float]]]
    ) -> list[int]:
        if not summary:
            return []
        width = max(len(values) for values in summary.values())
        return [report_year - offset for offset in range(max(2, width))]

    # -- 合并与校验 -------------------------------------------------------
    @staticmethod
    def _pick_column(
        parsed: dict[str, list[Optional[float]]], index: int
    ) -> dict[str, Optional[float]]:
        return {
            column: (values[index] if index < len(values) else None)
            for column, values in parsed.items()
        }

    @staticmethod
    def _collect_pages(ranges: dict[str, Any], index: int) -> dict[str, int]:
        pages: dict[str, int] = {}
        for key in ("balance_sheet", "profit_sheet", "cash_flow_sheet"):
            block = ranges.get(key) or ()
            if block:
                pages[key] = block[0][0]
        return pages

    def _merge_summary(
        self,
        periods: list[ExtractedPeriod],
        summary: dict[str, list[Optional[float]]],
        warnings: list[str],
    ) -> None:
        """用摘要表补齐报表里拿不到的字段（扣非 / 归母净资产），并做勾稽校验。"""
        for index, period in enumerate(periods):
            for field_name, values in summary.items():
                value = values[index] if index < len(values) else None
                if value is None:
                    continue
                target = None
                if field_name in {"OPERATE_INCOME", "TOTAL_OPERATE_INCOME"}:
                    target = period.profit
                elif field_name in {"TOTAL_ASSETS", "TOTAL_LIABILITIES", "TOTAL_PARENT_EQUITY"}:
                    target = period.balance
                elif field_name == "DEDUCT_PARENT_NETPROFIT":
                    target = period.profit
                if target is None:
                    continue
                existing = target.get(field_name)
                if existing is None:
                    target[field_name] = value
                elif abs(existing - value) > 0.01:
                    warnings.append(
                        f"{period.period_name} {field_name}：报表 {existing:,.2f} 与摘要表 "
                        f"{value:,.2f} 不一致（差 {existing - value:,.2f}），以报表为准"
                    )
                elif field_name == "TOTAL_PARENT_EQUITY":
                    continue

    @staticmethod
    def _apply_secondary(
        periods: list[ExtractedPeriod], secondary: dict[str, list[Optional[float]]]
    ) -> None:
        for index, period in enumerate(periods):
            for field_name, values in secondary.items():
                value = values[index] if index < len(values) else None
                if value is not None and period.cash.get(field_name) is None:
                    period.cash[field_name] = value

    def _validate(self, periods: list[ExtractedPeriod], warnings: list[str]) -> None:
        latest = periods[0] if periods else None
        if latest is None:
            return
        balance = latest.balance
        assets = balance.get("TOTAL_ASSETS")
        liabilities = balance.get("TOTAL_LIABILITIES")
        equity = balance.get("TOTAL_EQUITY")
        parent_equity = balance.get("TOTAL_PARENT_EQUITY")
        if assets and liabilities and equity:
            gap = assets - liabilities - equity
            if abs(gap) > 1.0:
                warnings.append(
                    f"{latest.period_name} 资产负债表未完全平衡：资产总计 {assets:,.2f} − "
                    f"负债 {liabilities:,.2f} − 所有者权益 {equity:,.2f} = {gap:,.2f} 元"
                )
        if assets and parent_equity and assets < parent_equity:
            warnings.append(f"{latest.period_name} 归母净资产大于资产总计，请复核解析结果")
        profit = latest.profit
        net = profit.get("NETPROFIT")
        parent = profit.get("PARENT_NETPROFIT")
        minority = profit.get("MINORITY_INTEREST")
        if net is not None and parent is not None and minority is not None:
            if abs(net - parent - minority) > 1.0:
                warnings.append(
                    f"{latest.period_name} 净利润({net:,.2f}) ≠ 归母({parent:,.2f}) + "
                    f"少数股东损益({minority:,.2f})"
                )
        if profit.get("DEDUCT_PARENT_NETPROFIT") is None:
            warnings.append("未能取到「扣除非经常性损益后的净利润」，非经常性损益规则将跳过")

    # -- 附注中的补充资料 -------------------------------------------------
    def _parse_secondary(
        self,
        lines: Sequence[tuple[int, str]],
        cash: dict[str, list[Optional[float]]],
        warnings: list[str],
    ) -> dict[str, list[Optional[float]]]:
        """补充资料（折旧/摊销/存货减少）在附注里，单独扫一遍取首次出现的两列。"""
        targets = self._secondary_map
        result: dict[str, list[Optional[float]]] = {}

        # 优先从「现金流量表补充资料」小节开始扫，否则附注里的同名科目会被误取
        # （实测：固定资产附注的「本期计提折旧」先出现，会覆盖补充资料的固定资产折旧）
        start_index = 0
        for index, (_page, text) in enumerate(lines):
            if "将净利润调节为经营活动现金流量" in text or "现金流量表补充资料" in text:
                start_index = index
                break

        for begin in ((start_index,) if start_index else (0,)) + ((0,) if start_index else ()):
            result = {}
            pending: list[str] = []
            index = begin
            total = len(lines)
            while index < total:
                page, text = lines[index]
                if _looks_like_column_header(text) or _RE_NOTE_REF.match(text):
                    index += 1
                    continue
                if _RE_DASH_ONLY.match(text):
                    pending.clear()
                    index += 1
                    continue
                if not _is_number_line(text):
                    pending.append(text)
                    if len(pending) > 4:
                        pending = pending[-4:]
                    index += 1
                    continue

                numbers: list[Optional[float]] = []
                while index < total and _is_number_line(lines[index][1]) and len(numbers) < 2:
                    numbers.append(parse_amount(lines[index][1]))
                    index += 1
                column = None
                for span in range(min(len(pending), 4), 0, -1):
                    candidate = normalize_label("".join(pending[-span:]))
                    if candidate in targets:
                        column = targets[candidate]
                        break
                pending.clear()
                if column and column not in result:
                    result[column] = [
                        numbers[0] if numbers else None,
                        numbers[1] if len(numbers) > 1 else None,
                    ]
            if len(result) >= len(SECONDARY_FIELDS):
                break
        if not result:
            warnings.append("未在附注中找到折旧/摊销补充资料，相关指标将返回 null")
        return result


# ---------------------------------------------------------------------------
# 与 financial_data 的对接
# ---------------------------------------------------------------------------
def build_em_frames(extraction: PdfStatementExtraction) -> dict[str, Any]:
    """把抽取结果组装成与东方财富接口**完全同构**的三张 DataFrame。"""
    import pandas as pd

    blocks = {
        "balance_sheet": [period.balance for period in extraction.periods],
        "profit_sheet": [period.profit for period in extraction.periods],
        "cash_flow_sheet": [period.cash for period in extraction.periods],
    }
    frames: dict[str, Any] = {}
    for key, rows in blocks.items():
        records: list[dict[str, Any]] = []
        for period, values in zip(extraction.periods, rows):
            record: dict[str, Any] = {
                "SECURITY_CODE": extraction.code,
                "SECURITY_NAME_ABBR": extraction.stock_name,
                "ORG_TYPE": "通用",
                "REPORT_TYPE": "年报",
                "REPORT_DATE": pd.Timestamp(year=period.year, month=12, day=31),
                "REPORT_DATE_NAME": period.period_name,
                "CURRENCY": "CNY",
                "NOTICE_DATE": pd.NaT,
                "UPDATE_DATE": pd.NaT,
            }
            record.update(values)
            records.append(record)
        frames[key] = pd.DataFrame(records)
    return frames


def extraction_cache_path(pdf_path: Path) -> Path:
    return RAG_CACHE_DIR / f"{pdf_path.stem}_tables.json"


def extract_statements(
    pdf_path: Path | str,
    *,
    code: Optional[str] = None,
    verbose: bool = False,
    use_cache: bool = True,
) -> PdfStatementExtraction:
    """抽取（带 JSON 缓存，键含文件名 + 大小 + mtime + 版本）。"""
    path = Path(pdf_path)
    if not path.exists():
        raise FileNotFoundError(f"本地年报 PDF 不存在：{path}")
    cache_path = extraction_cache_path(path)
    signature = {
        "version": EXTRACTOR_VERSION,
        "name": path.name,
        "size": path.stat().st_size,
        "mtime": int(path.stat().st_mtime),
    }
    if use_cache and cache_path.exists():
        try:
            payload = json.loads(cache_path.read_text(encoding="utf-8"))
            if payload.get("signature") == signature:
                return _from_dict(payload["result"])
        except (OSError, ValueError, KeyError):
            pass

    result = PdfFinancialExtractor(verbose=verbose).extract(path, code=code)
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(
            json.dumps({"signature": signature, "result": result.to_dict()},
                       ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except OSError:
        pass
    return result


def _from_dict(payload: dict[str, Any]) -> PdfStatementExtraction:
    periods = [
        ExtractedPeriod(
            year=int(item["year"]),
            period_name=str(item["period_name"]),
            balance=dict(item.get("balance") or {}),
            profit=dict(item.get("profit") or {}),
            cash=dict(item.get("cash") or {}),
            source_pages=dict(item.get("source_pages") or {}),
        )
        for item in payload.get("periods") or []
    ]
    return PdfStatementExtraction(
        pdf_path=Path(payload["pdf_path"]),
        code=str(payload["code"]),
        stock_name=str(payload["stock_name"]),
        report_year=int(payload["report_year"]),
        unit=str(payload.get("unit", "元")),
        consolidated=bool(payload.get("consolidated", True)),
        periods=periods,
        summary={k: list(v) for k, v in (payload.get("summary") or {}).items()},
        summary_pages=list(payload.get("summary_pages") or []),
        statement_pages={k: list(v) for k, v in (payload.get("statement_pages") or {}).items()},
        warnings=list(payload.get("warnings") or []),
        extractor_version=str(payload.get("extractor_version", EXTRACTOR_VERSION)),
    )


# ---------------------------------------------------------------------------
# 本地 PDF 定位 / 身份识别（供上传绑定与自动回退共用）
# ---------------------------------------------------------------------------
def infer_pdf_identity(pdf_path: Path | str) -> dict[str, Any]:
    """从任意命名的 PDF 中识别（代码、简称、年度），用于上传自动重命名。

    即使 PDF 里没有完整三大表（如专项公告），也尽量识别出代码与年度，
    以便正确归档到 ``{code}_{year}年报.pdf``。
    """
    import pymupdf

    path = Path(pdf_path)
    document = pymupdf.open(path)
    try:
        extractor = PdfFinancialExtractor(verbose=False)
        lines = extractor._collect_lines(document)
        try:
            ranges = extractor._locate_statements(lines, [])
        except ValueError:
            ranges = None
        return extractor._identify(lines, ranges=ranges, require_year=False)
    finally:
        document.close()


def find_local_pdf(
    code: str,
    *,
    pdf_dir: Optional[Path] = None,
    scan_unnamed: bool = True,
) -> Optional[Path]:
    """按代码定位本地年报 PDF：先看标准命名，再扫描未规范命名的文件内容。"""
    digits = "".join(ch for ch in str(code) if ch.isdigit())
    if not digits:
        return None
    directory = Path(pdf_dir) if pdf_dir is not None else PDF_DIR
    if not directory.exists():
        return None

    named = sorted(directory.glob(f"{digits}*.pdf"))
    if named:
        return named[0]

    if not scan_unnamed:
        return None
    for candidate in sorted(directory.glob("*.pdf")):
        if candidate.name.startswith(digits):
            continue
        try:
            identity = infer_pdf_identity(candidate)
        except Exception:  # noqa: BLE001 - 损坏 PDF 直接跳过
            continue
        if identity.get("code") == digits:
            return candidate
    return None
