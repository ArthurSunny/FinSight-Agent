"""财报附注感知解析与表格无损切片（Phase 3）。

设计目标（A 股年报特化）
-----------------------
1. **章节定界**：A 股年报常见层级为
   ``第八节 财务报告`` → ``七、合并财务报表项目注释`` → ``10、存货`` → ``(1).存货分类``。
   解析器按这四级标题维护"章节路径"，把每个 Chunk 归属到具体附注科目，
   从而让检索可以"按会计科目精确路由"，而不是在 300 页里盲搜。
2. **表格不截断**：用 pymupdf 的 ``find_tables()`` 抽取表格并转成 Markdown，
   再按 y 坐标插回正文流中，保证"存货分类明细表（账面余额/跌价准备/账面价值）"
   连表头一起进同一个 Chunk。

   > 若不按 y 坐标插回，同一页可能含多个附注科目（实测茅台 p84 一页就有
   > `1、货币资金` / `2、拆出资金` / `3、交易性金融资产` 三个标题），
   > 表格会被错误归属到页面最后一个科目上。

3. **页码溯源**：每个 Chunk 记录 1-based 的 ``page_num``（起始页）与 ``page_end``，
   报告中可精确引用 ``P.91``。这是铁律 2 在 PDF 场景的落地。

缓存
----
解析结果写入 ``data/cache/rag/{stem}_chunks.json``；缓存键包含 PDF 大小 + 修改时间 +
解析器版本，任一变即失效重解析（避免换了 PDF 还读旧切片）。
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

from src.config import RAG_CACHE_DIR

#: 解析器版本（改动切片逻辑时递增，自动使旧缓存失效）
PARSER_VERSION = "1.3.0"

#: 单个 Chunk 的目标上限（字符）。超长会在行边界切开，绝不切断表格。
MAX_CHUNK_CHARS = 2600
#: 过滤过短的碎片
MIN_CHUNK_CHARS = 60

# ---- 标题层级正则（作用于 strip 后的短行）----------------------------------
_RE_L1 = re.compile(r"^第([一二三四五六七八九十]+)节\s*(.*)$")
_RE_L2 = re.compile(r"^([一二三四五六七八九十]+)、\s*(\S.*)$")
_RE_L3 = re.compile(r"^(\d{1,2})、\s*(\S.*)$")
_RE_L4 = re.compile(r"^[\(（](\d{1,2})[\)）][\.、]?\s*(\S.*)$")
#: 新三板/北交所年报常用「（一）合同负债」式的附注标题（中文数字 + 全角括号）
_RE_L2_CN = re.compile(r"^[\(（]([一二三四五六七八九十]{1,3})[）)]\s*(\S.*)$")
#: 期次标签：只允许「YYYY年报」；含 .pdf 等文件名残留时重新提取
_RE_PERIOD = re.compile(r"(20\d{2})\s*年?\s*年报?")


def normalize_period(raw: str) -> str:
    """把任意期次写法规整成 ``YYYY年报``（取不到年份时原样返回）。

    >>> normalize_period("2025年报.pdf")
    '2025年报'
    >>> normalize_period("600519_2024年报")
    '2024年报'
    """
    text = str(raw or "")
    match = _RE_PERIOD.search(text)
    if match:
        return f"{int(match.group(1))}年报"
    fallback = re.search(r"(20\d{2})", text)
    return f"{int(fallback.group(1))}年报" if fallback else text

#: 目录行（"（一）货币资金…… 120"）不能当作标题
_RE_TOC = re.compile(r"[\.·…]{3,}\s*\d+\s*$")

#: 表格行标签黑名单：附注明细表的行名（"四、账面价值"、"一、账面原值"）
#: 如果把它们当成章节标题，会把「七、合并财务报表项目注释」这一层覆盖掉，
#: 导致后续所有附注切片丢失科目归属（实测踩坑，见 AGENTS.md §6）。
_TABLE_ROW_LABELS: frozenset[str] = frozenset(
    {
        "账面原值",
        "账面价值",
        "账面余额",
        "累计折旧",
        "累计摊销",
        "减值准备",
        "存货跌价准备",
        "合同履约成本减值准备",
        "期初余额",
        "期末余额",
        "期初账面价值",
        "期末账面价值",
        "本期增加",
        "本期减少",
        "本期计提",
        "本期转回",
        "本期转销",
        "本期增加金额",
        "本期减少金额",
        "购置",
        "处置",
        "计提",
        "转回",
        "转销",
        "其他",
        "合计",
        "小计",
        "原值",
        "净值",
        "成本",
        "公允价值",
        "外币折算",
        "汇率变动",
        "在建工程转入",
        "企业合并增加",
    }
)

#: 中文数字 → 整数（1~99）
_CN_DIGITS = {"零": 0, "一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}


def _cn_to_int(text: str) -> Optional[int]:
    """把"七""十二""二十"等中文数字转成整数；无法解析返回 None。"""
    text = text.strip()
    if not text:
        return None
    if text.isdigit():
        return int(text)
    if "十" in text:
        head, _, tail = text.partition("十")
        tens = _CN_DIGITS.get(head, 1) if head else 1
        ones = _CN_DIGITS.get(tail, 0) if tail else 0
        if head and head not in _CN_DIGITS:
            return None
        if tail and tail not in _CN_DIGITS:
            return None
        return tens * 10 + ones
    if len(text) == 1 and text in _CN_DIGITS:
        return _CN_DIGITS[text]
    return None


#: 二级标题"锚点"：出现即强制接受，绕过编号单调性检查，并锁死 L2。
#:
#: 为什么需要：合并利润表的行标签也用中文数字编号（"七、综合收益总额"、"八、每股收益"），
#: 与"七、合并财务报表项目注释"同构，单调性检查无法区分。实测这两行把 L2 计数器顶到 8，
#: 使紧随其后的"七、合并财务报表项目注释"被拒绝 —— 附注科目归属全部丢失。
_ANCHOR_L2: tuple[str, ...] = (
    "合并财务报表项目注释",
    "母公司财务报表项目注释",
    "合并财务报表主要项目注释",
    "母公司财务报表主要项目注释",
    "财务报表附注",
)


class _HeadingTracker:
    """带**编号单调性**校验的章节标题状态机。

    为什么必须校验单调性
    --------------------
    附注明细表里大量出现 ``四、账面价值`` / ``一、账面原值`` 这类行标签。
    若把它们当成二级标题，"七、合并财务报表项目注释"会被覆盖，后续所有附注
    切片都会丢失科目归属（实测：不做校验时 note 切片只有 13 页，修正后覆盖全部附注）。

    规则
    ----
    * 同级标题编号必须**递增**（L1/L2/L3）；L4 允许重复（每张表都从 (1) 开始）；
    * 接受第 k 级标题时，清空更深的层级与计数器；
    * ``第X节`` 与标题分行的排版（``第八节`` / ``财务报告``）需合并处理。
    """

    def __init__(self, allow_cn_paren_l2: bool = False) -> None:
        self.numbers: dict[int, int] = {1: 0, 2: 0, 3: 0, 4: 0}
        self.titles: dict[int, str] = {1: "", 2: "", 3: "", 4: ""}
        self._pending_l1: Optional[tuple[int, int]] = None  # (number, cn)
        self._pending_l2_cn: Optional[int] = None
        #: 进入"财务报表项目注释"后锁死 L2，防止后续报表行标签再次篡改层级
        self._locked_l2: bool = False
        #: 文档没有「第X节」结构时（新三板/北交所年报），启用「（中文数字）标题」识别
        self._allow_cn_paren_l2: bool = allow_cn_paren_l2

    def reset(self) -> None:
        self.numbers = {1: 0, 2: 0, 3: 0, 4: 0}
        self.titles = {1: "", 2: "", 3: "", 4: ""}
        self._pending_l1 = None
        self._pending_l2_cn = None
        self._locked_l2 = False

    @property
    def path(self) -> str:
        return " > ".join(self.titles[level] for level in (1, 2, 3, 4) if self.titles[level])

    @property
    def title(self) -> str:
        for level in (4, 3, 2, 1):
            if self.titles[level]:
                return self.titles[level]
        return "未标章节"

    @property
    def level1(self) -> str:
        return self.titles[1]

    def _accept(self, level: int, number: int, title: str) -> tuple[int, str]:
        self.titles[level] = title
        self.numbers[level] = number
        if level == 1:
            self._locked_l2 = False
        for deeper in range(level + 1, 5):
            self.titles[deeper] = ""
            self.numbers[deeper] = 0
        return level, title

    def feed(self, line: str) -> Optional[tuple[int, str]]:
        """喂入一行文本；若判定为标题则返回 ``(level, title)``。"""
        if not line or len(line) > 40 or _RE_TOC.search(line):
            self._pending_l1 = None
            return None

        # ---- 处理"第X节"与节标题分行的排版 ----
        if self._pending_l1 is not None:
            number, cn = self._pending_l1
            self._pending_l1 = None
            if not re.match(r"^[\(（\d一二三四五六七八九十]", line) and number > self.numbers[1]:
                return self._accept(1, number, f"第{cn}节 {line}")

        match = _RE_L1.match(line)
        if match:
            number = _cn_to_int(match.group(1))
            title = match.group(2).strip()
            if number is None:
                return None
            if not title:
                # "第八节" 单独一行：记住编号，等下一行作为标题
                self._pending_l1 = (number, match.group(1))
                return None
            if number > self.numbers[1]:
                return self._accept(1, number, f"第{match.group(1)}节 {title}")
            return None

        # ---- 「（中文数字）标题」格式（新三板/北交所年报的附注科目）----
        # 进入"合并财务报表项目注释"（L2 锚点）后，这些标题作为**三级**科目处理；
        # 无可锚定的 L2 时（无「第X节」结构的年报）作为二级处理。
        if self._allow_cn_paren_l2 or self._locked_l2:
            cn_level = 3 if self._locked_l2 else 2
            if self._pending_l2_cn is not None:
                number = self._pending_l2_cn
                self._pending_l2_cn = None
                if (
                    number == self.numbers[cn_level] + 1
                    and 2 <= len(line) <= 40
                    and line not in _TABLE_ROW_LABELS
                    and not re.fullmatch(r"[\d,\.\s%（）()／/]+", line)
                ):
                    return self._accept(cn_level, number, line.strip())
            match_cn = _RE_L2_CN.match(line)
            if match_cn:
                number = _cn_to_int(match_cn.group(1))
                title = match_cn.group(2).strip()
                if number is None:
                    return None
                if not title:
                    # 「（八）」单独一行，标题在下一行
                    self._pending_l2_cn = number
                    return None
                if (
                    len(title) >= 2
                    and title not in _TABLE_ROW_LABELS
                    and not re.fullmatch(r"[\d,\.\s%（）()／/]+", title)
                    and number == self.numbers[cn_level] + 1  # 严格 +1，挡住表格行乱跳
                ):
                    return self._accept(cn_level, number, title)
                return None

        for level, pattern in ((2, _RE_L2), (3, _RE_L3), (4, _RE_L4)):
            match = pattern.match(line)
            if not match:
                continue
            number = _cn_to_int(match.group(1))
            title = match.group(2).strip()
            if number is None or len(title) < 2 or title in _TABLE_ROW_LABELS:
                return None
            # 行标签常带数量单位/数字，标题不会（"账面价值"已进黑名单，这里兜底）
            if re.fullmatch(r"[\d,\.\s%（）()／/]+", title):
                return None

            if level == 2:
                is_anchor = any(anchor in title for anchor in _ANCHOR_L2)
                if is_anchor:
                    # 锚点强制接受：绕过单调性，并锁死 L2
                    result = self._accept(2, number, title)
                    self._locked_l2 = True
                    return result
                if self._locked_l2:
                    return None
                if number <= self.numbers[2]:
                    return None
                return self._accept(2, number, title)

            strict = level == 3
            if strict and number <= self.numbers[level]:
                return None
            return self._accept(level, number, title)
        return None

#: 页眉页脚
_HEADER_FOOTER = (
    re.compile(r"^\d+\s*/\s*\d+$"),
    re.compile(r"^[\u4e00-\u9fa5A-Za-z0-9（）()]{2,30}年年度报告$"),
    re.compile(r"^第\s*\d+\s*页\s*共\s*\d+\s*页$"),
)

#: 核心区域判定关键词
_NOTE_KEYWORDS = ("财务报表项目注释", "项目注释", "财务报表附注", "合并财务报表", "母公司财务报表")
_MDA_KEYWORDS = ("管理层讨论与分析", "经营情况讨论与分析", "主营业务分析", "产销量", "主要销售客户")
_STATEMENT_KEYWORDS = ("合并资产负债表", "合并利润表", "合并现金流量表", "合并所有者权益变动表")


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------
@dataclass
class Chunk:
    """一个可检索的附注切片。"""

    chunk_id: str
    text: str
    page_num: int
    page_end: int
    section_title: str
    section_path: str
    pdf_period: str
    chunk_kind: str = "other"  # note | statement | mda | other
    has_table: bool = False
    table_count: int = 0
    char_len: int = 0

    @property
    def page_label(self) -> str:
        """人类可读页码，如 ``P.91`` 或 ``P.91-92``。"""
        return f"P.{self.page_num}" if self.page_end <= self.page_num else f"P.{self.page_num}-{self.page_end}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "chunk_id": self.chunk_id,
            "text": self.text,
            "page_num": self.page_num,
            "page_end": self.page_end,
            "section_title": self.section_title,
            "section_path": self.section_path,
            "pdf_period": self.pdf_period,
            "chunk_kind": self.chunk_kind,
            "has_table": self.has_table,
            "table_count": self.table_count,
            "char_len": self.char_len,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "Chunk":
        return cls(**payload)


@dataclass
class RagDocument:
    """一份解析完成的年报附注库。"""

    pdf_path: Path
    stem: str
    pdf_period: str
    page_count: int
    chunks: list[Chunk] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)
    parsed_at: str = ""

    # -- 查询辅助 -----------------------------------------------------------
    @property
    def note_chunks(self) -> list[Chunk]:
        return [chunk for chunk in self.chunks if chunk.chunk_kind == "note"]

    @property
    def core_chunks(self) -> list[Chunk]:
        """核心区域切片（附注 / 报表 / 经营分析），检索默认只在这部分进行。"""
        return [chunk for chunk in self.chunks if chunk.chunk_kind != "other"]

    def to_dict(self) -> dict[str, Any]:
        return {
            "parser_version": PARSER_VERSION,
            "pdf_path": str(self.pdf_path),
            "stem": self.stem,
            "pdf_period": self.pdf_period,
            "page_count": self.page_count,
            "parsed_at": self.parsed_at,
            "stats": self.stats,
            "chunks": [chunk.to_dict() for chunk in self.chunks],
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "RagDocument":
        return cls(
            pdf_path=Path(payload["pdf_path"]),
            stem=payload["stem"],
            pdf_period=payload["pdf_period"],
            page_count=int(payload["page_count"]),
            chunks=[Chunk.from_dict(item) for item in payload.get("chunks", [])],
            stats=payload.get("stats", {}),
            parsed_at=payload.get("parsed_at", ""),
        )


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------
def _is_header_footer(line: str) -> bool:
    return any(pattern.match(line) for pattern in _HEADER_FOOTER)


def _detect_heading(line: str) -> Optional[tuple[int, str]]:
    """识别标题层级，返回 ``(level, title)``；非标题返回 None。"""
    if not line or len(line) > 40 or _RE_TOC.search(line):
        return None
    match = _RE_L1.match(line)
    if match:
        title = match.group(2).strip()
        return (1, f"第{match.group(1)}节 {title}".strip()) if title else None
    match = _RE_L2.match(line)
    if match and len(match.group(2)) >= 2:
        return (2, f"{match.group(1)}、{match.group(2).strip()}")
    match = _RE_L3.match(line)
    if match and len(match.group(2)) >= 2:
        return (3, f"{match.group(1)}、{match.group(2).strip()}")
    match = _RE_L4.match(line)
    if match and len(match.group(2)) >= 2:
        return (4, f"({match.group(1)}){match.group(2).strip()}")
    return None


def _table_to_markdown(rows: Sequence[Sequence[Any]]) -> str:
    """把 ``find_tables`` 的二维结果转成 Markdown 表格（保留表头）。"""
    cleaned: list[list[str]] = []
    for row in rows:
        cells = [
            re.sub(r"\s+", " ", str(cell)).strip() if cell is not None else "" for cell in row
        ]
        if any(cells):
            cleaned.append(cells)
    if len(cleaned) < 2:
        return ""
    width = max(len(row) for row in cleaned)
    cleaned = [row + [""] * (width - len(row)) for row in cleaned]
    if width < 2:
        return ""
    header = "| " + " | ".join(cleaned[0]) + " |"
    divider = "|" + "|".join(["---"] * width) + "|"
    body = "\n".join("| " + " | ".join(row) + " |" for row in cleaned[1:])
    return f"{header}\n{divider}\n{body}"


def _classify(section_path: str, level1: str) -> str:
    """根据章节路径判定切片类型。"""
    joined = f"{section_path} {level1}"
    if any(keyword in joined for keyword in _STATEMENT_KEYWORDS):
        return "statement"
    if any(keyword in joined for keyword in _NOTE_KEYWORDS):
        return "note"
    if any(keyword in joined for keyword in _MDA_KEYWORDS):
        return "mda"
    return "other"


# ---------------------------------------------------------------------------
# 解析器
# ---------------------------------------------------------------------------
class AnnualReportParser:
    """年报 PDF → 附注 Chunk 库。"""

    def __init__(
        self,
        cache_dir: Optional[Path] = None,
        *,
        max_chunk_chars: int = MAX_CHUNK_CHARS,
        table_mode: str = "core",
        verbose: bool = True,
    ) -> None:
        """Parameters
        ----------
        table_mode:
            * ``core``（默认）：只对核心区域（附注/报表/经营分析）的页面抽表，兼顾质量与速度；
            * ``all``：所有页面抽表（慢，约 0.4s/页）；
            * ``off``：关闭表格抽取，仅保留纯文本流。
        """
        self.cache_dir = Path(cache_dir) if cache_dir is not None else RAG_CACHE_DIR
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.max_chunk_chars = max_chunk_chars
        self.table_mode = table_mode.lower()
        self.verbose = verbose

    # -- 日志 ---------------------------------------------------------------
    def _log(self, message: str) -> None:
        if self.verbose:
            print(f"[pdf_parser] {message}", flush=True)

    # -- 缓存 ---------------------------------------------------------------
    def cache_path(self, pdf_path: Path) -> Path:
        return self.cache_dir / f"{pdf_path.stem}_chunks.json"

    def _cache_signature(self, pdf_path: Path) -> str:
        stat = pdf_path.stat()
        raw = f"{PARSER_VERSION}|{pdf_path.name}|{stat.st_size}|{int(stat.st_mtime)}|{self.table_mode}|{self.max_chunk_chars}"
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]

    # -- 对外入口 -----------------------------------------------------------
    def parse(
        self,
        pdf_path: Path | str,
        pdf_period: str,
        *,
        use_cache: bool = True,
        force: bool = False,
    ) -> RagDocument:
        path = Path(pdf_path)
        if not path.exists():
            raise FileNotFoundError(f"年报 PDF 不存在：{path}")

        # 期次标签必须是「YYYY年报」这一种形态，缺年份时退回文件名推断，
        # 任何情况下都不允许把 ".pdf" 之类的文件名残留带进引用（铁律 2）。
        pdf_period = normalize_period(pdf_period or path.name)

        target = self.cache_path(path)
        signature = self._cache_signature(path)
        if use_cache and not force and target.exists():
            try:
                payload = json.loads(target.read_text(encoding="utf-8"))
                if payload.get("signature") == signature:
                    document = RagDocument.from_dict(payload["document"])
                    document.pdf_period = pdf_period
                    for chunk in document.chunks:
                        chunk.pdf_period = pdf_period
                    self._log(
                        f"命中切片缓存：{target.name}（{len(document.chunks)} 个切片，毫秒级加载）"
                    )
                    document.stats["cache_hit"] = True
                    return document
            except (OSError, ValueError, KeyError, TypeError) as exc:
                self._log(f"切片缓存不可用（{type(exc).__name__}: {exc}），重新解析")

        document = self._parse_pdf(path, pdf_period)
        try:
            target.write_text(
                json.dumps(
                    {"signature": signature, "document": document.to_dict()},
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            self._log(f"切片已缓存：{target.name}")
        except OSError as exc:
            self._log(f"切片缓存写入失败（{exc}），不影响本次结果")
        return document

    # -- 解析主流程 ---------------------------------------------------------
    def _parse_pdf(self, path: Path, pdf_period: str) -> RagDocument:
        import pymupdf  # 延迟导入：仅真正解析时才需要

        started = dt.datetime.now()
        doc = pymupdf.open(path)
        try:
            page_total = doc.page_count
            chunks: list[Chunk] = []
            has_sections = self._has_section_headings(doc, page_total)
            tracker = _HeadingTracker(allow_cn_paren_l2=not has_sections)
            if not has_sections:
                self._log("未检测到「第X节」结构，按新三板/北交所版式解析附注")
            pos = {"page_start": 1}
            buffer: list[str] = []
            table_count = 0
            core_start = self._detect_core_start(doc, page_total)
            self._log(f"核心区域自第 {core_start} 页开始（表格抽取模式={self.table_mode}）")

            def flush(
                page_end: int,
                *,
                title: Optional[str] = None,
                path: Optional[str] = None,
                level1: Optional[str] = None,
            ) -> None:
                """把当前缓冲区落成一个 Chunk。

                注意：必须由调用方传入**切换标题前**的快照，否则切片会被贴上下一节的标题
                （实测踩坑：'货币资金' 的表格被标成 '拆出资金'）。
                """
                nonlocal buffer, table_count
                text = "\n".join(line for line in buffer if line.strip()).strip()
                buffer = []
                if len(text) < MIN_CHUNK_CHARS:
                    table_count = 0
                    return
                path_text = tracker.path if path is None else path
                title_text = tracker.title if title is None else title
                level1_text = tracker.level1 if level1 is None else level1
                kind = _classify(path_text, level1_text)
                if kind == "other" and any(
                    keyword in text for keyword in ("合并资产负债表", "合并利润表", "合并现金流量表")
                ):
                    kind = "statement"
                chunks.append(
                    Chunk(
                        chunk_id=f"c{len(chunks) + 1:04d}",
                        text=text,
                        page_num=int(pos["page_start"]),
                        page_end=int(page_end),
                        section_title=title_text,
                        section_path=path_text,
                        pdf_period=pdf_period,
                        chunk_kind=kind,
                        has_table=table_count > 0,
                        table_count=table_count,
                        char_len=len(text),
                    )
                )
                table_count = 0

            for page_index in range(page_total):
                page_no = page_index + 1
                page = doc[page_index]
                run_tables = self.table_mode == "all" or (
                    self.table_mode == "core" and page_no >= core_start
                )
                items = self._page_items(page, page_no, run_tables=run_tables)

                for kind, payload in items:
                    if kind == "table":
                        buffer.append(payload)
                        table_count += 1
                        continue
                    line = str(payload).strip()
                    if not line or _is_header_footer(line):
                        continue
                    snapshot = (tracker.title, tracker.path, tracker.level1)
                    heading = tracker.feed(line)
                    if heading is not None:
                        # 用"切换前"的快照落盘，保证切片归属于它实际的章节
                        flush(
                            page_no,
                            title=snapshot[0],
                            path=snapshot[1],
                            level1=snapshot[2],
                        )
                        pos["page_start"] = page_no
                        continue
                    if not buffer:
                        pos["page_start"] = page_no
                    buffer.append(line)
                    if sum(len(item) for item in buffer) > self.max_chunk_chars:
                        flush(page_no)
                        pos["page_start"] = page_no

            flush(page_total)

            stats = {
                "page_count": page_total,
                "chunks": len(chunks),
                "note_chunks": sum(1 for chunk in chunks if chunk.chunk_kind == "note"),
                "statement_chunks": sum(1 for chunk in chunks if chunk.chunk_kind == "statement"),
                "mda_chunks": sum(1 for chunk in chunks if chunk.chunk_kind == "mda"),
                "other_chunks": sum(1 for chunk in chunks if chunk.chunk_kind == "other"),
                "chunks_with_table": sum(1 for chunk in chunks if chunk.has_table),
                "table_mode": self.table_mode,
                "elapsed_s": round((dt.datetime.now() - started).total_seconds(), 2),
                "cache_hit": False,
            }
            self._log(
                f"解析完成：{page_total} 页 → {stats['chunks']} 个切片"
                f"（附注 {stats['note_chunks']} / 报表 {stats['statement_chunks']} / "
                f"经营 {stats['mda_chunks']} / 含表 {stats['chunks_with_table']}），"
                f"耗时 {stats['elapsed_s']}s"
            )
            return RagDocument(
                pdf_path=path,
                stem=path.stem,
                pdf_period=pdf_period,
                page_count=page_total,
                chunks=chunks,
                stats=stats,
                parsed_at=dt.datetime.now().isoformat(timespec="seconds"),
            )
        finally:
            doc.close()

    def _has_section_headings(self, doc: Any, page_total: int) -> bool:
        """判断文档是否使用「第X节」结构（沪深主板年报）；新三板/北交所年报通常没有。"""
        for index in range(min(page_total, 30)):
            for line in doc[index].get_text("text").splitlines():
                if _RE_L1.match(line.strip()):
                    return True
        return False

    def _detect_core_start(self, doc: Any, page_total: int) -> int:
        """定位"核心区域"起始页（预扫描，只读文本，约 1s）。

        优先找 ``财务报告`` 节，其次 ``管理层讨论与分析``；都找不到则从第 1 页开始
        （宁可多抽表，也不漏掉附注）。
        """
        fallback = 1
        for index in range(page_total):
            try:
                text = doc[index].get_text("text")
            except Exception:  # noqa: BLE001
                continue
            if "管理层讨论与分析" in text or "经营情况讨论与分析" in text:
                fallback = max(1, index + 1)
            if "合并财务报表项目注释" in text or "财务报表附注" in text:
                return max(1, index + 1)
            if "财务报告" in text and "第" in text:
                fallback = min(fallback, max(1, index + 1)) if fallback > 1 else max(1, index + 1)
        return fallback

    def _page_items(self, page: Any, page_no: int, *, run_tables: bool) -> list[tuple[str, str]]:
        """把一页拆成按 y 坐标排序的 (类型, 内容) 序列，表格按位置插回正文流。"""
        items: list[tuple[float, str, str]] = []

        try:
            blocks = page.get_text("blocks")
        except Exception:  # noqa: BLE001 - 极端排版下退回纯文本
            blocks = [(0, 0, 0, 0, page.get_text("text"), 0, 0)]

        for block in blocks:
            if len(block) >= 7 and block[6] != 0:  # 非文本块（图片）
                continue
            x0, y0 = float(block[0]), float(block[1])
            text = str(block[4])
            items.append((y0, "text", text))

        if run_tables:
            try:
                tables = page.find_tables()
                for table in tables.tables:
                    markdown = _table_to_markdown(table.extract())
                    if markdown:
                        y0 = float(table.bbox[1]) if getattr(table, "bbox", None) else 0.0
                        items.append((y0, "table", markdown))
            except Exception as exc:  # noqa: BLE001 - 表格抽取失败不影响文本流
                if page_no <= 5:
                    self._log(f"第 {page_no} 页表格抽取失败（{type(exc).__name__}），已跳过")

        items.sort(key=lambda item: item[0])

        ordered: list[tuple[str, str]] = []
        for _y0, kind, payload in items:
            if kind == "text":
                for line in str(payload).splitlines():
                    ordered.append(("text", line))
            else:
                ordered.append((kind, payload))
        return ordered


# ---------------------------------------------------------------------------
# 便捷函数
# ---------------------------------------------------------------------------
def parse_annual_report(
    pdf_path: Path | str,
    pdf_period: str,
    *,
    use_cache: bool = True,
    table_mode: str = "core",
    verbose: bool = True,
) -> RagDocument:
    """一步解析年报 PDF（带缓存）。"""
    return AnnualReportParser(table_mode=table_mode, verbose=verbose).parse(
        pdf_path, pdf_period, use_cache=use_cache
    )
