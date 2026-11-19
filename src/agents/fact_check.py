"""确定性数值防幻觉质检（Fact Checker）—— Node 5 的纯 Python 实现。

为什么必须是纯 Python（铁律 1）
------------------------------
"报告里有没有写错数字"这件事**不能交给 LLM 自查**（它会倾向于给自己打高分）。
本模块用确定性的正则与容差比较完成四类校验，并把具体错误文本反馈给
``chief_editor`` 触发 LangGraph 条件边重写：

1. **章节完整性**：六个必选章节齐备、序号正确、正文非空；
2. **核心锚点数值**：最新年度的营收/归母/扣非/毛利率/净利率/ROE/净现比/M-Score
   必须出现且量级正确；
3. **符号方向**：负值写成正值（或反之）会被单独标记为"符号写反"；
4. **单位与报告期**：出现「万元」视为单位错误；出现事实包之外的「20XX年报」视为虚构报告期。

容差策略：取 ``max(0.01, |值| × 0.1%)``。这足以放过"1720.5417 → 1720.54"的正常四舍五入，
同时能抓住"亿元写成万元"（差 1 万倍）与"百分比当成小数"（差 100 倍）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional, Sequence

from src.agents.prompts import REQUIRED_SECTIONS, REPORT_TITLE

#: 每个章节正文的最小字符数
MIN_SECTION_CHARS = 80
#: 全文字数下限
MIN_REPORT_CHARS = 1200
#: 锚点相对容差
REL_TOLERANCE = 1e-3
#: 章节序号 → 标题关键词（防止标题张冠李戴）
_SECTION_KEYWORDS: dict[str, str] = {
    "一": "总览",
    "二": "杜邦",
    "三": "现金流",
    "四": "排雷",
    "五": "同业",
    "六": "附注",
}

_NUM_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")
_PERIOD_RE = re.compile(r"(20\d{2})\s*年报")
_HEADING_RE = re.compile(r"^(#{1,4})\s*([一二三四五六七八九十]+)、\s*(.+?)\s*$", re.MULTILINE)
_MINUS_CHARS = "-−—–"


@dataclass
class CheckIssue:
    """单条质检问题。"""

    code: str
    message: str
    severity: str = "error"  # error 会触发重写；warning 只记录

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message, "severity": self.severity}


@dataclass
class FactCheckResult:
    """质检结论。"""

    passed: bool
    issues: list[CheckIssue] = field(default_factory=list)
    checks: list[dict[str, Any]] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)

    @property
    def errors(self) -> list[str]:
        return [issue.message for issue in self.issues if issue.severity == "error"]

    @property
    def warnings(self) -> list[str]:
        return [issue.message for issue in self.issues if issue.severity == "warning"]

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "error_count": len(self.errors),
            "warning_count": len(self.warnings),
            "issues": [issue.to_dict() for issue in self.issues],
            "checks": self.checks,
            "stats": self.stats,
        }


# ---------------------------------------------------------------------------
# 数字抽取与匹配
# ---------------------------------------------------------------------------
def extract_numbers(text: str) -> list[tuple[float, int, int]]:
    """抽取文本中的全部数字（含符号），返回 ``[(带符号数值, start, end)]``。

    单独处理减号是为了正确识别 ``-2.8351``，同时避免把 ``2024-2025`` 的区间连字符
    误判成负号。
    """
    numbers: list[tuple[float, int, int]] = []
    for match in _NUM_RE.finditer(text):
        start, end = match.start(), match.end()
        token = match.group(0).replace(",", "")
        try:
            value = float(token)
        except ValueError:
            continue

        cursor = start - 1
        while cursor >= 0 and text[cursor] in " \t":
            cursor -= 1
        if cursor >= 0 and text[cursor] in _MINUS_CHARS:
            before = cursor - 1
            # 区间写法（2024-2025）的连字符前面是数字，不视为负号
            if before < 0 or not (text[before].isdigit() or text[before] == "."):
                value = -value
        numbers.append((value, start, end))
    return numbers


def _is_close(value: float, target: float, tolerance: float) -> bool:
    return abs(abs(value) - abs(target)) <= tolerance


def _assess_anchor(anchor: dict[str, Any], numbers: Sequence[tuple[float, int, int]]) -> dict[str, Any]:
    """判定单个锚点是否在报告中正确出现。"""
    target = float(anchor["expected_number"])
    tolerance = max(float(anchor.get("tolerance_abs", 0.01)), abs(target) * REL_TOLERANCE)
    label = anchor.get("label", anchor.get("key", "?"))
    kind = anchor.get("kind", "ratio")

    same_sign: list[float] = []
    opposite: list[float] = []
    for value, _start, _end in numbers:
        if not _is_close(value, target, tolerance):
            continue
        if abs(target) <= tolerance or value * target >= 0:
            same_sign.append(value)
        else:
            opposite.append(value)

    # 量级错位诊断：亿元 ↔ 万元（×1e4）、百分数 ↔ 小数（×100）
    magnitude_hint: Optional[str] = None
    for value, _start, _end in numbers:
        if abs(target) > 0 and abs(abs(value) - abs(target) * 1e4) <= abs(target) * 1e4 * REL_TOLERANCE:
            magnitude_hint = "疑似把「亿元」写成了「万元」（量级差 1 万倍）"
            break
    if magnitude_hint is None and kind == "percent" and abs(target) > 0:
        for value, _start, _end in numbers:
            if abs(abs(value) - abs(target) / 100) <= max(1e-6, abs(target) / 100 * 1e-2):
                magnitude_hint = "疑似把百分数写成了小数（如把 91.18% 写成 0.91）"
                break

    status = "ok"
    if same_sign:
        status = "ok"
    elif opposite:
        status = "sign_flipped"
    elif magnitude_hint:
        status = "magnitude"
    else:
        status = "missing"

    return {
        "key": anchor.get("key"),
        "label": label,
        "expected": anchor.get("display"),
        "period": anchor.get("period"),
        "status": status,
        "hint": magnitude_hint,
        "tolerance": round(tolerance, 6),
    }


# ---------------------------------------------------------------------------
# 章节校验
# ---------------------------------------------------------------------------
def split_sections(markdown: str) -> dict[str, tuple[str, str]]:
    """把 Markdown 按 "## 一、xxx" 切分，返回 ``{序号: (标题, 正文)}``。"""
    headings = list(_HEADING_RE.finditer(markdown))
    sections: dict[str, tuple[str, str]] = {}
    for index, match in enumerate(headings):
        end = headings[index + 1].start() if index + 1 < len(headings) else len(markdown)
        ordinal = match.group(2)
        title = match.group(3)
        body = markdown[match.end() : end].strip()
        sections[ordinal] = (title, body)
    return sections


def _check_sections(markdown: str, issues: list[CheckIssue], checks: list[dict[str, Any]]) -> None:
    sections = split_sections(markdown)
    for ordinal, title in REQUIRED_SECTIONS:
        expected_heading = f"{ordinal}、{title}"
        entry = sections.get(ordinal)
        if entry is None:
            issues.append(
                CheckIssue(
                    "section_missing",
                    f"缺少必选章节「## {expected_heading}」，请按规定的六个二级标题完整输出。",
                )
            )
            checks.append({"check": "section", "target": expected_heading, "status": "missing"})
            continue

        actual_title, body = entry
        keyword = _SECTION_KEYWORDS.get(ordinal, "")
        keyword_ok = (not keyword) or (keyword in actual_title) or (keyword in title and actual_title)
        length_ok = len(body) >= MIN_SECTION_CHARS

        if not keyword_ok:
            issues.append(
                CheckIssue(
                    "section_title",
                    f"章节「{ordinal}」标题疑似错位：应为「{title}」，实际为「{actual_title}」。",
                )
            )
        if not length_ok:
            issues.append(
                CheckIssue(
                    "section_empty",
                    f"章节「## {ordinal}、{actual_title}」正文过短（{len(body)} 字符，"
                    f"要求 ≥ {MIN_SECTION_CHARS}），请补充实质内容。",
                )
            )
        checks.append(
            {
                "check": "section",
                "target": expected_heading,
                "actual_title": actual_title,
                "chars": len(body),
                "status": "ok" if (keyword_ok and length_ok) else "fail",
            }
        )


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------
def run_fact_check(
    markdown: str,
    anchors: Sequence[dict[str, Any]],
    *,
    allowed_periods: Optional[Iterable[str]] = None,
    require_title: bool = True,
    required_phrases: Sequence[tuple[str, tuple[str, ...]]] = (),
    required_page_labels: Sequence[str] = (),
) -> FactCheckResult:
    """对报告执行确定性质检。

    Parameters
    ----------
    markdown:
        首席撰稿人产出的 Markdown 全文。
    anchors:
        :meth:`src.agents.context_formatter.FactPack.anchor_dicts` 的结果。
    allowed_periods:
        允许出现的报告期标签集合（如 ``["2025年报", "2024年报", ...]``）；
        ``None`` 表示跳过虚构报告期检查。
    require_title:
        是否要求出现报告标题。
    required_phrases:
        "必须转述"的短语组，形如 ``[("同业数据可信度提示", ("数据可信度", "待核实参考"))]``。
        只要命中任一备选写法即通过。用于强制转述事实包中的**关键数据质量提示**
        （例如同业营收异常波动的护栏结论），避免模型"看到但没写"。
    required_page_labels:
        必须出现的附注页码（如 ``["P.91", "P.84"]``）。Phase 3 的 RAG 证据若被采纳，
        报告中必须原样引用页码，否则读者无法复核 —— 这是可追溯性的硬约束。
    """
    text = markdown or ""
    issues: list[CheckIssue] = []
    checks: list[dict[str, Any]] = []
    numbers = extract_numbers(text)

    # ---- ① 基础形态 ----
    if len(text) < MIN_REPORT_CHARS:
        issues.append(
            CheckIssue("too_short", f"报告过短（{len(text)} 字符，要求 ≥ {MIN_REPORT_CHARS}）。")
        )
    if require_title and REPORT_TITLE.split("（")[0] not in text:
        issues.append(
            CheckIssue("title_missing", f"缺少报告标题「{REPORT_TITLE}」。", severity="warning")
        )

    # ---- ② 章节完整性 ----
    _check_sections(text, issues, checks)

    # ---- ③ 核心锚点 ----
    anchor_results = [_assess_anchor(anchor, numbers) for anchor in anchors]
    for result in anchor_results:
        if result["status"] == "ok":
            continue
        if result["status"] == "sign_flipped":
            issues.append(
                CheckIssue(
                    "anchor_sign_flipped",
                    f"符号写反：{result['label']} 应为 {result['expected']}（{result['period']}），"
                    f"报告中写成了相反符号。",
                )
            )
        elif result["status"] == "magnitude":
            issues.append(
                CheckIssue(
                    "anchor_magnitude",
                    f"数值量级错误：{result['label']} 应为 {result['expected']}（{result['period']}）。"
                    f"{result['hint'] or ''}",
                )
            )
        else:
            issues.append(
                CheckIssue(
                    "anchor_missing",
                    f"核心数值缺失：报告中未出现「{result['label']} = {result['expected']}」"
                    f"（{result['period']}），请按事实包补全并标注报告期。",
                )
            )
    checks.extend({"check": "anchor", **result} for result in anchor_results)

    # ---- ④ 单位 ----
    # 全项目金额只允许「亿元」或「元」：亿元用于常规规模，元用于不足 0.01 亿元的微额
    # （如附注里的 128.4 万元跌价准备）；「万元」一律禁止，因为它是财报误读的第一大来源。
    wan_matches = list(re.finditer("万元", text))
    if wan_matches:
        fragments = [
            text[max(0, match.start() - 26) : match.start() + 4].replace("\n", " ").strip()
            for match in wan_matches[:3]
        ]
        issues.append(
            CheckIssue(
                "unit_wan_yuan",
                "单位错误：报告中出现「万元」，位置如「…"
                + "」「…".join(fragments)
                + "…」。全项目金额只允许两种单位：① 常规金额用「亿元」；"
                "② 不足 0.01 亿元的微额直接用「元」（如 1,283,984.83 元）。"
                "若引用的是附注原文（原文单位为万元），请先换算为元再括注"
                "（1 万元 = 10,000 元；例如 1,704,578.78 万元写成 17,045,787,800 元）。",
            )
        )
        checks.append(
            {
                "check": "unit",
                "status": "fail",
                "detail": f"出现万元 ×{len(wan_matches)}",
            }
        )
    else:
        checks.append({"check": "unit", "status": "ok", "detail": "未出现万元"})

    # ---- ⑤ 报告期虚构 ----
    if allowed_periods is not None:
        allowed = set(allowed_periods)
        mentioned = {f"{match}年报" for match in _PERIOD_RE.findall(text)}
        unknown = sorted(mentioned - allowed)
        if unknown:
            issues.append(
                CheckIssue(
                    "period_hallucination",
                    f"出现事实包之外的报告期：{', '.join(unknown)}；"
                    f"本次可用报告期仅 {', '.join(sorted(allowed))}。",
                )
            )
        checks.append(
            {
                "check": "period",
                "mentioned": sorted(mentioned),
                "unknown": unknown,
                "status": "ok" if not unknown else "fail",
            }
        )

    # ---- ⑤ 必须转述的关键提示 ----
    for label, alternatives in required_phrases:
        hit = any(alt in text for alt in alternatives)
        if not hit:
            issues.append(
                CheckIssue(
                    "required_phrase_missing",
                    f"必须转述事实包中的「{label}」（可用写法：{' / '.join(alternatives)}）。"
                    f"该类提示属于数据可信度护栏，漏写会让读者误把脏数据当作可靠对标基准。",
                )
            )
        checks.append(
            {
                "check": "required_phrase",
                "label": label,
                "status": "ok" if hit else "fail",
            }
        )

    # ---- ⑥ 附注页码引用（Phase 3）----
    for label in required_page_labels:
        hit = label in text
        if not hit:
            issues.append(
                CheckIssue(
                    "citation_missing",
                    f"缺少附注页码引用「{label}」：第六章必须原样引用 RAG 证据中的页码，"
                    f"否则读者无法回溯年报原文。",
                )
            )
        checks.append(
            {"check": "citation", "label": label, "status": "ok" if hit else "fail"}
        )

    errors = [issue for issue in issues if issue.severity == "error"]
    stats = {
        "chars": len(text),
        "numbers_found": len(numbers),
        "anchors": len(anchor_results),
        "anchors_ok": sum(1 for item in anchor_results if item["status"] == "ok"),
        "sections_ok": sum(
            1 for item in checks if item["check"] == "section" and item["status"] == "ok"
        ),
    }
    return FactCheckResult(passed=not errors, issues=issues, checks=checks, stats=stats)
