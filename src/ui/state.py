"""Phase 4：数据访问层（历史存档回放 / 报告重建 / PDF 检索器缓存）。

设计要点
--------
1. **历史回放 0.5 秒级**：``agent_trace.json`` 不包含完整 ``FinancialReport``，
   但 Phase 1 引擎有 CSV 磁盘缓存，``analyze_stock`` 重建通常 < 0.5s；
   再用 ``st.cache_data`` 包一层，同一会话内重复切换标签页零成本。
2. **不做任何计算**：本模块只负责"取文件 / 反序列化 / 调引擎"，不解释财务数据。
3. **缓存键包含 mtime**：PDF 被替换后检索器会自动重建，避免读到旧切片。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from src.config import PDF_DIR, REPORT_OUTPUT_DIR
from src.tools.financial_engine import analyze_stock
from src.tools.peer_comparison import PeerComparison
from src.tools.schemas import FinancialReport

RAG_VERIFIED_VERDICTS = {"verified_benign", "verified_risk"}


# ---------------------------------------------------------------------------
# 历史存档
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ArchivedRun:
    """一份已落盘的历史运行（trace + markdown 配对）。"""

    trace_path: Path
    markdown_path: Optional[Path]
    stock_code: str
    stock_name: str
    latest_period: str
    mode: str
    elapsed_s: float
    total_tokens: int
    retries: int
    fact_check_passed: bool
    rag_verified: int
    rag_total: int
    modified_at: float

    @property
    def label(self) -> str:
        verified = f"｜附注实证 {self.rag_verified}/{self.rag_total}" if self.rag_total else ""
        return (
            f"{self.stock_name}（{self.stock_code}）· {self.latest_period}"
            f" · {'API' if self.mode == 'api' else '离线'}{verified}"
        )

    @property
    def caption(self) -> str:
        quality = "质检通过" if self.fact_check_passed else "质检放行"
        return (
            f"{_fmt_time(self.modified_at)}｜{self.elapsed_s:.1f}s｜"
            f"{self.total_tokens:,} tokens｜{quality}｜重写 {self.retries} 次"
        )


def _fmt_time(timestamp: float) -> str:
    import datetime as dt

    return dt.datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M")


def scan_archived_runs(directory: Optional[Path] = None) -> list[ArchivedRun]:
    """扫描 ``data/reports/`` 下的 ``*_agent_trace.json``（按修改时间倒序）。"""
    base = Path(directory) if directory is not None else REPORT_OUTPUT_DIR
    if not base.exists():
        return []

    runs: list[ArchivedRun] = []
    for trace_path in base.glob("*_agent_trace.json"):
        try:
            trace = json.loads(trace_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        stem = trace_path.name[: -len("_agent_trace.json")]
        markdown_path = base / f"{stem}_tearsheet.md"
        evidences = (trace.get("rag_evidences") or {}).get("evidences") or []
        tokens = trace.get("tokens") or {}
        runs.append(
            ArchivedRun(
                trace_path=trace_path,
                markdown_path=markdown_path if markdown_path.exists() else None,
                stock_code=str(trace.get("stock_code", "")),
                stock_name=str(trace.get("stock_name", "")),
                latest_period=str(trace.get("latest_period", "")),
                mode=str(trace.get("mode", "api")),
                elapsed_s=float(trace.get("elapsed_s", 0.0) or 0.0),
                total_tokens=int(tokens.get("total", 0) or 0),
                retries=int(trace.get("retry_count", 0) or 0),
                fact_check_passed=bool(trace.get("fact_check_passed")),
                rag_verified=sum(
                    1 for item in evidences if item.get("verdict") in RAG_VERIFIED_VERDICTS
                ),
                rag_total=len(evidences),
                modified_at=trace_path.stat().st_mtime,
            )
        )
    runs.sort(key=lambda run: run.modified_at, reverse=True)
    return runs


def load_trace(trace_path: Path | str) -> dict[str, Any]:
    return json.loads(Path(trace_path).read_text(encoding="utf-8"))


def load_markdown_text(markdown_path: Optional[Path | str]) -> str:
    if markdown_path is None:
        return ""
    path = Path(markdown_path)
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 报告重建（带 Streamlit 缓存）
# ---------------------------------------------------------------------------
def load_report(code: str, lookback_years: int = 4) -> FinancialReport:
    """从本地缓存重建 ``FinancialReport``（命中引擎磁盘缓存，通常 < 0.5s）。"""
    import streamlit as st

    @st.cache_data(show_spinner=False, ttl=1800)
    def _cached(stock_code: str, lookback: int) -> dict[str, Any]:
        return analyze_stock(stock_code, lookback_years=lookback).model_dump(mode="json")

    return FinancialReport.model_validate(_cached(code, int(lookback_years)))


def peer_from_trace(trace: Optional[dict[str, Any]]) -> Optional[PeerComparison]:
    """从 trace 反序列化 ``PeerComparison``（无则返回 None）。"""
    if not trace:
        return None
    payload = trace.get("peer_comparison")
    if not payload:
        return None
    try:
        return PeerComparison.model_validate(payload)
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# PDF / 检索器
# ---------------------------------------------------------------------------
def resolve_trace_pdf(trace: Optional[dict[str, Any]]) -> Optional[tuple[Path, str]]:
    """从 trace 的 ``rag_meta`` 中恢复 PDF 路径与报告期（文件仍存在才算）。"""
    if not trace:
        return None
    meta = trace.get("rag_meta") or {}
    raw_path = meta.get("pdf_path")
    period = meta.get("pdf_period")
    if not raw_path or not period:
        return None
    path = Path(raw_path)
    if not path.exists():
        return None
    return path, str(period)


def list_local_pdfs(code: str) -> list[Path]:
    """列出 ``data/pdfs/`` 下与代码匹配的年报 PDF。"""
    if not PDF_DIR.exists():
        return []
    return sorted(PDF_DIR.glob(f"{code}*.pdf"))


def infer_pdf_period(pdf_path: Path) -> str:
    """从文件名推断报告期（``600519_2025年报.pdf`` → ``2025年报``）。"""
    import re

    matches = re.findall(r"(20\d{2})", pdf_path.name)
    return f"{max(int(item) for item in matches)}年报" if matches else "未知期次"


def load_retriever(pdf_path: Path | str, pdf_period: str):
    """构建（并缓存）附注检索器；返回 ``(retriever, document)``。"""
    import streamlit as st

    @st.cache_resource(show_spinner=False)
    def _cached(path_str: str, period: str, mtime: float):
        from src.rag.pdf_parser import AnnualReportParser
        from src.rag.retriever import HybridRetriever

        document = AnnualReportParser(verbose=False).parse(Path(path_str), period)
        return HybridRetriever(document), document

    path = Path(pdf_path)
    return _cached(str(path), pdf_period, path.stat().st_mtime)


def save_uploaded_pdf(filename: str, payload: bytes) -> tuple[Path, dict[str, Any]]:
    """保存上传的 PDF 并按内容自动归档为标准命名 ``{code}_{year}年报.pdf``。

    Returns
    -------
    (最终路径, 识别出的身份信息)
    """
    target = PDF_DIR / Path(filename).name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(payload)
    return archive_pdf(target)


#: 标准命名：``{6位代码}_{4位年份}年报.pdf``
STANDARD_PDF_PATTERN = re.compile(r"^\d{6}_\d{4}年报\.pdf$")


def archive_pdf(path: Path | str) -> tuple[Path, dict[str, Any]]:
    """识别 PDF 的代码与年度，并把文件重命名为标准名（识别失败则原样保留）。"""
    from src.tools.pdf_financial_extractor import infer_pdf_identity

    source = Path(path)
    try:
        identity = infer_pdf_identity(source)
    except Exception:  # noqa: BLE001 - 识别失败不影响流程，用户仍可手动指定代码
        return source, {}

    code = str(identity.get("code") or "")
    year = identity.get("report_year")
    if not code or not year:
        return source, identity

    standard = source.with_name(f"{code}_{int(year)}年报.pdf")
    if standard == source:
        return source, identity
    try:
        if standard.exists():
            if standard.stat().st_size == source.stat().st_size:
                source.unlink()  # 同一份文件，删掉重复的
                return standard, identity
        source.replace(standard)
        return standard, identity
    except OSError:
        return source, identity


def adopt_local_pdfs() -> list[tuple[str, str]]:
    """把 ``data/pdfs/`` 里未按标准命名的 PDF 自动归档（如巨潮原始文件名）。"""
    if not PDF_DIR.exists():
        return []
    adopted: list[tuple[str, str]] = []
    for candidate in sorted(PDF_DIR.glob("*.pdf")):
        if STANDARD_PDF_PATTERN.match(candidate.name):
            continue
        final, identity = archive_pdf(candidate)
        if final != candidate:
            adopted.append((candidate.name, final.name))
    return adopted


# ---------------------------------------------------------------------------
# 可观测性派生指标（只读 trace，不做财务计算）
# ---------------------------------------------------------------------------
def token_breakdown(trace: Optional[dict[str, Any]]) -> dict[str, Any]:
    """Token / 延迟 / KV-Cache 命中率汇总（纯统计，不含财务语义）。"""
    usage = (trace or {}).get("usage") or []
    prompt = sum(int(item.get("prompt_tokens", 0) or 0) for item in usage)
    completion = sum(int(item.get("completion_tokens", 0) or 0) for item in usage)
    cached = sum(int(item.get("cached_tokens", 0) or 0) for item in usage)
    latency = sum(float(item.get("latency_s", 0.0) or 0.0) for item in usage)
    return {
        "calls": len(usage),
        "prompt": prompt,
        "completion": completion,
        "total": prompt + completion,
        "cached": cached,
        "cache_hit_rate": (cached / prompt) if prompt else 0.0,
        "llm_latency_s": latency,
        "rows": usage,
    }


def fact_check_rows(trace: Optional[dict[str, Any]]) -> list[dict[str, Any]]:
    """把 ``fact_check.checks`` 拍平成可展示的表格行。"""
    checks = ((trace or {}).get("fact_check") or {}).get("checks") or []
    labels = {
        "section": "章节完整性",
        "anchor": "核心锚点",
        "unit": "金额单位",
        "period": "报告期真实性",
        "citation": "附注页码引用",
        "required_phrase": "必转述提示",
    }
    rows: list[dict[str, Any]] = []
    for item in checks:
        kind = str(item.get("check", ""))
        target = (
            item.get("target")
            or item.get("label")
            or item.get("key")
            or item.get("detail")
            or "—"
        )
        detail = ""
        if kind == "anchor":
            detail = f"{item.get('expected', '')}（容差 {item.get('tolerance', '')}）"
        elif kind == "section":
            detail = f"{item.get('chars', 0)} 字符"
        elif kind == "period":
            detail = "、".join(item.get("mentioned") or []) or "—"
        rows.append(
            {
                "校验类型": labels.get(kind, kind),
                "对象": target,
                "结果": "通过" if item.get("status") == "ok" else "未通过",
                "细节": detail,
            }
        )
    return rows
