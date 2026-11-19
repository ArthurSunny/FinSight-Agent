"""Phase 3：年报 PDF 附注解析与穿透式混合 RAG。

模块职责
--------
* :mod:`src.rag.pdf_fetcher` —— 双通道获取年报 PDF（本地 `data/pdfs/` 优先 + 巨潮资讯下载）
* :mod:`src.rag.pdf_parser`  —— 附注感知解析：章节定界、表格保留、页码溯源、Chunk 缓存
* :mod:`src.rag.retriever`   —— 科目路由 + jieba/BM25 混合检索（**无需任何 Embedding API**）

设计约束（见 AGENTS.md §6）
--------------------------
1. **零额外付费 Key**：DeepSeek 不提供 ``/embeddings``，因此不做向量检索，
   改用"附注科目路由 + BM25"的混合方案；
2. **页码强制溯源**：每个 Chunk 都带 1-based ``page_num``，报告必须引用 ``P.xx``；
3. **可退化**：任何环节失败都不阻塞主报告，只是回退到 Phase 2 的第六章形态。

典型用法
--------
>>> from src.rag.pdf_fetcher import fetch_annual_report_pdf          # doctest: +SKIP
>>> from src.rag.pdf_parser import parse_annual_report               # doctest: +SKIP
>>> from src.rag.retriever import HybridRetriever                    # doctest: +SKIP
>>> artifact = fetch_annual_report_pdf("600519", 2025)               # doctest: +SKIP
>>> doc = parse_annual_report(artifact.path, artifact.pdf_period)    # doctest: +SKIP
>>> hits = HybridRetriever(doc).search("存货分类明细", target_account="存货").hits  # doctest: +SKIP
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # 仅类型检查时导入，避免 `import src.rag` 就加载 pymupdf/jieba
    from src.rag.pdf_fetcher import AnnualReportFetcher, PdfArtifact, fetch_annual_report_pdf
    from src.rag.pdf_parser import AnnualReportParser, Chunk, RagDocument, parse_annual_report
    from src.rag.retriever import HybridRetriever, RetrievedChunk, SearchOutcome

__all__ = [
    "AnnualReportFetcher",
    "AnnualReportParser",
    "Chunk",
    "HybridRetriever",
    "PdfArtifact",
    "RagDocument",
    "RetrievedChunk",
    "SearchOutcome",
    "fetch_annual_report_pdf",
    "parse_annual_report",
]

_EXPORTS: dict[str, str] = {
    "AnnualReportFetcher": "pdf_fetcher",
    "PdfArtifact": "pdf_fetcher",
    "fetch_annual_report_pdf": "pdf_fetcher",
    "AnnualReportParser": "pdf_parser",
    "Chunk": "pdf_parser",
    "RagDocument": "pdf_parser",
    "parse_annual_report": "pdf_parser",
    "HybridRetriever": "retriever",
    "RetrievedChunk": "retriever",
    "SearchOutcome": "retriever",
}


def __getattr__(name: str) -> Any:
    """惰性导出（PEP 562）。"""
    module_name = _EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module 'src.rag' has no attribute {name!r}")
    from importlib import import_module

    module = import_module(f"src.rag.{module_name}")
    return getattr(module, name)


def __dir__() -> list[str]:
    return sorted(__all__)
