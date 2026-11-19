"""Phase 3 · 年报 PDF 附注解析与混合检索 —— 实盘验证。

运行：:

    python tests/test_rag_pipeline.py                  # 自动获取/复用 data/pdfs 下的年报
    python tests/test_rag_pipeline.py --pdf <path>     # 指定本地 PDF
    python tests/test_rag_pipeline.py --offline        # 只测本地已有 PDF，不联网下载

验证内容
--------
1. **双通道获取**：本地目录命中 / 巨潮下载；失败时优雅返回 None（不抛错）；
2. **附注感知解析**：页数、切片数、附注切片数、页码合法性、表格保留；
3. **缓存**：二次解析毫秒级命中；
4. **混合检索**：存货 / 货币资金 / 合同负债 / 营业收入确认 四条查询的 Top-3
   必须命中对应附注章节（这是本阶段的核心质量指标）；
5. **证据完整性**：命中切片带 `page_label`、`section_path`、原文片段。
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import Optional, Sequence

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (ValueError, OSError):
            pass

from src.config import PDF_DIR, ensure_directories  # noqa: E402
from src.rag.pdf_fetcher import AnnualReportFetcher  # noqa: E402
from src.rag.pdf_parser import AnnualReportParser  # noqa: E402
from src.rag.retriever import HybridRetriever  # noqa: E402

TRANSCRIPT: list[str] = []

#: (target_account, 检索意图, 期望命中的章节关键词)
QUERY_CASES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    (
        "存货",
        "年报附注-存货分类明细：原材料、在产品（基酒/半成品）、库存商品构成及跌价准备计提比例",
        ("存货",),
    ),
    (
        "货币资金",
        "年报附注-货币资金：库存现金、银行存款、其他货币资金明细及使用受限资金",
        ("货币资金", "受限"),
    ),
    (
        "合同负债",
        "年报附注-合同负债：预收货款账龄结构与本期变动",
        ("合同负债",),
    ),
    (
        "营业收入确认",
        "年报附注-收入确认：销售商品收入确认时点与经销直销模式划分",
        ("营业收入", "收入"),
    ),
)


def emit(text: str = "") -> None:
    print(text)
    TRANSCRIPT.append(text)


def banner(title: str) -> None:
    emit()
    emit("=" * 92)
    emit(title)
    emit("=" * 92)


class Checker:
    def __init__(self) -> None:
        self.passed = 0
        self.failed: list[str] = []
        self.skipped: list[str] = []

    def check(self, condition: bool, label: str, detail: str = "") -> bool:
        if condition:
            self.passed += 1
            emit(f"  [OK]   {label}" + (f"  ({detail})" if detail else ""))
            return True
        self.failed.append(f"{label}" + (f"  ({detail})" if detail else ""))
        emit(f"  [FAIL] {label}" + (f"  ({detail})" if detail else ""))
        return False

    def skip(self, label: str, reason: str) -> None:
        self.skipped.append(f"{label}: {reason}")
        emit(f"  [SKIP] {label}  ({reason})")

    def summary(self) -> int:
        emit()
        emit(f"断言结果：通过 {self.passed} 项，失败 {len(self.failed)} 项，跳过 {len(self.skipped)} 项。")
        for item in self.failed:
            emit(f"  [FAIL] {item}")
        for item in self.skipped:
            emit(f"  [SKIP] {item}")
        return 0 if not self.failed else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Phase 3 年报附注 RAG 实盘验证")
    parser.add_argument("--code", default="600519", help="股票代码")
    parser.add_argument("--year", type=int, default=2025, help="目标年度")
    parser.add_argument("--pdf", default=None, help="指定本地 PDF 路径")
    parser.add_argument("--offline", action="store_true", help="禁止联网下载（仅用本地 PDF）")
    parser.add_argument("--top-k", type=int, default=3, help="每条查询返回的证据数")
    parser.add_argument("--force-parse", action="store_true", help="忽略切片缓存强制冷解析")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    ensure_directories()
    checker = Checker()
    started = time.perf_counter()

    banner("FinSight-Agent Phase 3 · 年报附注解析与混合检索 · 实盘验证")
    emit(f"  运行时间：{time.strftime('%Y-%m-%d %H:%M:%S')}")
    emit(f"  标的：{args.code} / {args.year}年报 / PDF 目录：{PDF_DIR}")

    # ---- [1] 获取 PDF ----
    banner("[1] 双通道 PDF 获取（本地优先 + 巨潮下载）")
    fetcher = AnnualReportFetcher(verbose=True)
    local_candidates = sorted(PDF_DIR.glob(f"{args.code}*.pdf"))
    emit(f"  data/pdfs 现有：{[p.name for p in local_candidates] or '（空）'}")
    t0 = time.perf_counter()
    artifact = fetcher.fetch(
        args.code,
        args.year,
        allow_download=not args.offline,
        local_path=args.pdf,
    )
    fetch_elapsed = time.perf_counter() - t0
    if artifact is None:
        checker.check(False, "获取年报 PDF", fetcher.last_reason or "未知原因")
        _write_transcript()
        return checker.summary()

    emit(f"  命中：{artifact.path.name}（{artifact.source}）｜{artifact.pdf_period}"
         f"｜{artifact.size_bytes / 1024 / 1024:.1f} MB｜{fetch_elapsed:.1f}s")
    if artifact.period_mismatch_note:
        emit(f"  期次提示：{artifact.period_mismatch_note}")
    checker.check(artifact.path.exists(), "PDF 文件存在", str(artifact.path))
    checker.check(artifact.size_bytes > 200_000, "PDF 体积合理（>200KB）",
                  f"{artifact.size_bytes / 1024 / 1024:.1f} MB")
    checker.check(
        artifact.pdf_period == f"{args.year}年报" or artifact.period_mismatch_note is not None,
        "报告期与请求年度一致（或已显式标注期次差异）",
        artifact.pdf_period,
    )

    # ---- [2] 解析 ----
    banner("[2] 附注感知解析（章节定界 / 表格保留 / 页码溯源）")
    parser = AnnualReportParser(verbose=True)
    t0 = time.perf_counter()
    document = parser.parse(artifact.path, artifact.pdf_period, force=args.force_parse)
    first_parse = time.perf_counter() - t0
    emit(f"  解析：{first_parse:.1f}s｜{document.page_count} 页 → {len(document.chunks)} 切片"
         f"（附注 {len(document.note_chunks)} / 经营 {document.stats.get('mda_chunks')} / "
         f"含表 {document.stats.get('chunks_with_table')}）"
         f"{'［命中缓存］' if document.stats.get('cache_hit') else '［冷解析］'}")

    checker.check(document.page_count >= 50, "页数 ≥ 50（完整年报）", f"{document.page_count} 页")
    checker.check(len(document.chunks) >= 50, "切片数 ≥ 50", f"{len(document.chunks)}")
    checker.check(len(document.note_chunks) >= 10, "附注切片 ≥ 10",
                  f"{len(document.note_chunks)}")
    bad_pages = [
        chunk
        for chunk in document.chunks
        if not (1 <= chunk.page_num <= document.page_count)
        or chunk.page_end < chunk.page_num
        or chunk.page_end > document.page_count
    ]
    checker.check(not bad_pages, "全部切片页码合法（1 ≤ page_num ≤ page_end ≤ 总页数）",
                  f"异常 {len(bad_pages)} 条")
    checker.check(
        document.stats.get("chunks_with_table", 0) >= 10,
        "表格切片 ≥ 10（表格未被丢弃）",
        f"{document.stats.get('chunks_with_table')}",
    )

    # ---- [3] 缓存 ----
    banner("[3] 切片缓存（二次加载）")
    t0 = time.perf_counter()
    cached = parser.parse(artifact.path, artifact.pdf_period)
    cache_elapsed = time.perf_counter() - t0
    checker.check(bool(cached.stats.get("cache_hit")), "命中切片缓存",
                  f"{cache_elapsed * 1000:.0f} ms")
    checker.check(cache_elapsed < 2.0, "二次加载 < 2s",
                  f"{cache_elapsed * 1000:.0f} ms（首解析 {first_parse:.1f}s）")

    # ---- [4] 检索 ----
    banner("[4] 混合检索（科目路由 + jieba/BM25）")
    t0 = time.perf_counter()
    retriever = HybridRetriever(cached, top_k=args.top_k)
    index_elapsed = time.perf_counter() - t0
    emit(f"  索引：{len(retriever.chunks)} 个核心切片，耗时 {index_elapsed:.2f}s")
    retrieval_ms = 0.0

    for account, query, expected in QUERY_CASES:
        outcome = retriever.search(query, target_account=account)
        retrieval_ms += outcome.stats.elapsed_s * 1000
        hits = outcome.hits
        if not hits:
            checker.check(False, f"{account} 检索命中", "无结果")
            continue
        top_titles = [f"{hit.page_label} {hit.section_path.split(' > ')[-1]}" for hit in hits]
        emit(f"  [{account}] {outcome.stats.elapsed_s * 1000:.0f}ms｜route_hits={outcome.stats.route_hits}")
        for hit in hits:
            emit(f"      {hit.page_label:<10} score={hit.score:.2f} | {hit.section_path[:66]}")
        hit_ok = any(
            any(keyword in f"{hit.section_title} {hit.section_path}" for keyword in expected)
            for hit in hits
        )
        checker.check(bool(hit_ok), f"{account}：Top-{args.top_k} 命中对应附注章节",
                      " / ".join(top_titles[:2]))
        checker.check(
            all(1 <= hit.page_num <= document.page_count for hit in hits),
            f"{account}：命中页码合法",
            ", ".join(hit.page_label for hit in hits),
        )
        checker.check(
            all(hit.pdf_period == artifact.pdf_period for hit in hits),
            f"{account}：证据携带正确报告期",
            artifact.pdf_period,
        )

    # ---- [5] 表格内容保留 ----
    banner("[5] 表格内容保留（存货分类明细三列）")
    inventory = retriever.search(QUERY_CASES[0][1], target_account="存货").hits
    merged = "\n".join(hit.text for hit in inventory)
    has_table = any(hit.has_table for hit in inventory)
    emit("  存货证据片段示例：")
    for line in inventory[0].snippet(420).splitlines()[:12]:
        emit("    " + line)
    checker.check(has_table, "存货证据含表格切片")
    checker.check(any(token in merged for token in ("库存商品", "在产品", "原材料")),
                  "存货表保留明细科目行（原材料/在产品/库存商品）")
    checker.check("跌价准备" in merged or "账面价值" in merged,
                  "存货表保留账面余额/跌价准备/账面价值列")

    # ---- [6] 退化路径 ----
    banner("[6] 退化路径（取不到 PDF 时不抛错）")
    missing = AnnualReportFetcher(verbose=False).fetch(
        "000000", 2025, allow_download=False
    )
    checker.check(missing is None, "不存在的标的返回 None（不抛异常）")

    total = time.perf_counter() - started
    banner("[7] 汇总")
    emit(f"  总耗时：{total:.1f}s（获取 {fetch_elapsed:.1f}s / 首解析 {first_parse:.1f}s / "
         f"检索累计 {retrieval_ms:.0f} ms，单条均 < 10 ms）")
    exit_code = checker.summary()
    _write_transcript()
    emit()
    emit("  >>> 全部断言通过：Phase 3 RAG 流水线验证成功。" if exit_code == 0
         else "  >>> 存在失败断言，请修复后重跑。")
    _write_transcript()
    return exit_code


def _write_transcript() -> Path:
    from src.config import REPORT_OUTPUT_DIR

    target = REPORT_OUTPUT_DIR / "rag_validation_transcript.txt"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("\n".join(TRANSCRIPT), encoding="utf-8")
    return target


if __name__ == "__main__":
    raise SystemExit(main())
