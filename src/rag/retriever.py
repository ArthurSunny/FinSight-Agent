"""会计科目精准混合检索器（无外部 Embedding API）。

为什么不用向量检索
------------------
DeepSeek 官方不提供 ``/embeddings`` 接口，而本项目**不允许新增付费 Key**。
更重要的是：财报附注检索的关键从来不是"语义相似"，而是**科目精确命中**——
问"存货"，就必须落到 `10、存货` 这一节，而不是语义上相似的"营业成本"。

因此本模块用两条互补的召回路径：

1. **科目路由（高权重）**：把 ``target_account`` 与 Chunk 的 ``section_path`` /
   ``section_title`` 做精确与同义词匹配（"有息负债" → 短期借款/长期借款/应付债券/
   一年内到期的非流动负债/租赁负债）。这是精度来源。
2. **BM25 全文（细粒度）**：``jieba`` 分词（补充"基酒""存放同业""跌价准备"等会计专有词）
   + ``rank_bm25``，对 ``suggested_rag_query`` 做段落级召回。这是召回来源。

两者加权融合后返回带 **页码 + 章节路径 + 证据原文** 的 Top-K。
"""

from __future__ import annotations

import math
import re
import time
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional, Sequence

from src.rag.pdf_parser import Chunk, RagDocument

#: 会计专有词（补进 jieba 词典，避免被切碎导致 BM25 失配）
_ACCOUNTING_WORDS: tuple[str, ...] = (
    "基酒",
    "半成品",
    "在产品",
    "库存商品",
    "原材料",
    "跌价准备",
    "存货跌价准备",
    "合同履约成本",
    "存放同业",
    "存放中央银行",
    "集团财务公司",
    "财务公司",
    "合同负债",
    "预收款项",
    "非经常性损益",
    "政府补助",
    "资产处置",
    "有息负债",
    "短期借款",
    "长期借款",
    "应付债券",
    "一年内到期的非流动负债",
    "租赁负债",
    "货币资金",
    "其他货币资金",
    "现金及现金等价物",
    "应收账款",
    "坏账准备",
    "账龄",
    "商誉减值",
    "在建工程",
    "利息资本化",
    "营业收入",
    "营业成本",
    "销售费用",
    "广告宣传费",
    "市场推广费",
    "关联方",
    "受限资金",
    "使用受限",
    # ---- Phase 4：科技/制造/医药高频附注科目 ----
    "研发费用",
    "研发投入",
    "开发支出",
    "资本化研发",
    "费用化研发",
    "递延所得税资产",
    "递延所得税负债",
    "可抵扣暂时性差异",
    "应纳税暂时性差异",
    "固定资产",
    "累计折旧",
    "房屋建筑物",
    "机器设备",
    "应付票据",
    "应付账款",
    "其他应收款",
    "坏账准备",
    "预期信用损失",
    "信用减值损失",
    "资产减值损失",
    "前五名客户",
    "客户集中度",
    "使用权资产",
)

#: 科目同义词 / 关联词表（第 1 路召回的燃料）
_ACCOUNT_SYNONYMS: dict[str, tuple[str, ...]] = {
    "货币资金": (
        "货币资金",
        "库存现金",
        "银行存款",
        "其他货币资金",
        "现金及现金等价物",
        "存放同业",
        "拆出资金",
        "受限",
    ),
    "受限资金": ("货币资金", "使用受限", "受限", "质押", "冻结", "保证金"),
    "有息负债": (
        "短期借款",
        "长期借款",
        "应付债券",
        "一年内到期的非流动负债",
        "租赁负债",
        "借款",
        "债券",
    ),
    "借款": ("短期借款", "长期借款", "应付债券", "一年内到期的非流动负债", "租赁负债"),
    "存货": (
        "存货",
        "原材料",
        "在产品",
        "半成品",
        "基酒",
        "库存商品",
        "跌价准备",
        "存货分类",
        "合同履约成本",
    ),
    "合同负债": ("合同负债", "预收款项", "预收账款"),
    "营业收入确认": ("营业收入", "收入确认", "销售模式", "经销", "直销"),
    "营业收入": ("营业收入", "营业成本", "主营业务", "收入"),
    "非经常性损益": ("非经常性损益", "政府补助", "资产处置", "营业外收入", "营业外支出"),
    "商誉": ("商誉", "商誉减值", "资产组", "减值测试"),
    "应收账款": ("应收账款", "应收票据", "坏账准备", "账龄"),
    "在建工程": ("在建工程", "工程进度", "利息资本化", "转入固定资产"),
    "销售费用": ("销售费用", "广告宣传费", "市场推广费", "运输费"),
    # ---- Phase 4 新增：科技 / 制造 / 医药高频科目 ----
    "研发费用": (
        "研发费用",
        "研发投入",
        "费用化研发",
        "研发人员",
        "职工薪酬",
        "开发支出",
        "资本化研发",
    ),
    "开发支出": ("开发支出", "资本化研发", "资本化率", "无形资产", "研发项目"),
    "固定资产": (
        "固定资产",
        "房屋建筑物",
        "机器设备",
        "运输工具",
        "累计折旧",
        "折旧",
        "在建工程转入",
        "未办妥产权证书",
    ),
    "递延所得税": (
        "递延所得税资产",
        "递延所得税负债",
        "可抵扣暂时性差异",
        "应纳税暂时性差异",
        "未确认递延所得税资产",
        "税率",
    ),
    "递延所得税资产": ("递延所得税资产", "可抵扣暂时性差异", "未确认递延所得税资产"),
    "递延所得税负债": ("递延所得税负债", "应纳税暂时性差异"),
    "资产减值损失": ("资产减值损失", "存货跌价准备", "固定资产减值", "商誉减值", "减值测试", "计提"),
    "信用减值损失": ("信用减值损失", "坏账准备", "预期信用损失", "应收账款", "账龄组合"),
    "应付票据": ("应付票据", "银行承兑票据", "商业承兑票据", "应付账款"),
    "应付账款": ("应付账款", "应付工程款", "应付材料款", "账龄超过一年", "应付票据"),
    "其他应收款": ("其他应收款", "应收利息", "应收股利", "备用金", "押金", "保证金", "往来款"),
    "前五名客户": ("前五名客户", "客户集中度", "销售客户", "前五名供应商", "关联方"),
    "使用权资产": ("使用权资产", "租赁负债", "租赁合同", "短期租赁"),
}

#: 表格类问题特征词（命中时给"含表切片"加权）
_TABLE_HINTS: tuple[str, ...] = ("明细", "构成", "分类", "余额", "跌价", "账龄", "金额", "比例")

#: 停用词
_STOPWORDS: frozenset[str] = frozenset(
    {
        "的",
        "了",
        "和",
        "与",
        "及",
        "或",
        "在",
        "是",
        "为",
        "对",
        "从",
        "中",
        "上",
        "下",
        "请",
        "问",
        "年",
        "报",
        "年报",
        "附注",
        "情况",
        "说明",
        "以及",
        "是否",
        "有无",
        "如何",
    }
)

_JIEBA_READY = False


def _ensure_jieba() -> Any:
    """初始化 jieba 并注入会计词典（只做一次）。"""
    global _JIEBA_READY
    import logging

    import jieba  # 延迟导入

    if not _JIEBA_READY:
        # 关闭 jieba 的初始化日志：它会写 stderr，在 PowerShell 管道里会被误判为错误
        jieba.setLogLevel(logging.ERROR)
        for word in _ACCOUNTING_WORDS:
            jieba.add_word(word, freq=20000)
        _JIEBA_READY = True
    return jieba


def tokenize(text: str) -> list[str]:
    """中文分词 + 停用词过滤（BM25 的输入）。"""
    if not text:
        return []
    jieba = _ensure_jieba()
    tokens: list[str] = []
    for token in jieba.lcut(text):
        token = token.strip()
        if not token or token in _STOPWORDS:
            continue
        if re.fullmatch(r"[\s\W_]+", token):
            continue
        tokens.append(token.lower())
    return tokens


@dataclass
class RetrievedChunk:
    """一条检索命中的证据。"""

    chunk_id: str
    text: str
    page_num: int
    page_end: int
    page_label: str
    section_title: str
    section_path: str
    pdf_period: str
    score: float = 0.0
    route_score: float = 0.0
    bm25_score: float = 0.0
    matched_terms: tuple[str, ...] = ()
    has_table: bool = False
    chunk_kind: str = "other"

    def snippet(self, max_chars: int = 700) -> str:
        """用于喂给 LLM 的原文片段（去重空行）。"""
        compact = re.sub(r"\n{2,}", "\n", self.text.strip())
        return compact[:max_chars] + ("…" if len(compact) > max_chars else "")

    def to_dict(self, *, snippet_chars: int = 700) -> dict[str, Any]:
        return {
            "chunk_id": self.chunk_id,
            "page": self.page_num,
            "page_label": self.page_label,
            "section_title": self.section_title,
            "section_path": self.section_path,
            "pdf_period": self.pdf_period,
            "score": round(self.score, 4),
            "route_score": round(self.route_score, 4),
            "bm25_score": round(self.bm25_score, 4),
            "matched_terms": list(self.matched_terms),
            "has_table": self.has_table,
            "snippet": self.snippet(snippet_chars),
        }


@dataclass
class RetrievalStats:
    """一次检索的统计信息（写入 trace，便于评估召回质量）。"""

    searched_chunks: int
    top_score: float
    elapsed_s: float
    route_hits: int
    table_hits: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "searched_chunks": self.searched_chunks,
            "top_score": round(self.top_score, 4),
            "elapsed_s": round(self.elapsed_s, 3),
            "route_hits": self.route_hits,
            "table_hits": self.table_hits,
        }


@dataclass
class SearchOutcome:
    """单次查询的检索结果。"""

    query: str
    target_account: str
    hits: list[RetrievedChunk] = field(default_factory=list)
    stats: Optional[RetrievalStats] = None


class HybridRetriever:
    """科目路由 + BM25 的混合检索器。"""

    def __init__(
        self,
        document: RagDocument,
        *,
        top_k: int = 5,
        include_non_core: bool = False,
        route_weight: float = 0.62,
        bm25_weight: float = 0.38,
    ) -> None:
        self.document = document
        self.top_k = top_k
        self.route_weight = route_weight
        self.bm25_weight = bm25_weight
        pool = document.chunks if include_non_core else document.core_chunks
        self.chunks: list[Chunk] = [chunk for chunk in pool if chunk.char_len >= 40]
        self._bm25 = None
        self._tokenized: list[list[str]] = []
        self._build_index()

    # -- 索引 ---------------------------------------------------------------
    def _build_index(self) -> None:
        from rank_bm25 import BM25Okapi  # 延迟导入

        self._tokenized = [tokenize(chunk.text) for chunk in self.chunks]
        # 防御：空语料时 BM25Okapi 会除零
        corpus = [tokens if tokens else ["空"] for tokens in self._tokenized]
        self._bm25 = BM25Okapi(corpus) if corpus else None

    @property
    def available(self) -> bool:
        return bool(self.chunks) and self._bm25 is not None

    # -- 同义词扩展 ---------------------------------------------------------
    @staticmethod
    def expand_terms(account: str) -> tuple[str, ...]:
        """把 ``target_account`` 扩展成同义词/关联词集合。"""
        account = (account or "").strip()
        if not account:
            return ()
        terms: list[str] = [account]
        for key, values in _ACCOUNT_SYNONYMS.items():
            if key == account or key in account or account in key:
                terms.extend(values)
        # 去重保序
        seen: set[str] = set()
        ordered: list[str] = []
        for term in terms:
            if term and term not in seen:
                seen.add(term)
                ordered.append(term)
        return tuple(ordered)

    # -- 打分 ---------------------------------------------------------------
    def _route_score(self, chunk: Chunk, terms: Sequence[str]) -> tuple[float, tuple[str, ...]]:
        """第 1 路：科目路由分。"""
        if not terms:
            return 0.0, ()
        head = f"{chunk.section_title} {chunk.section_path}"
        matched: list[str] = []

        primary = terms[0]
        in_title = primary in head
        if in_title:
            matched.append(primary)
            return 1.0, tuple(matched)

        # 同义词出现在章节路径里 → 高权重
        for term in terms[1:]:
            if term in head:
                matched.append(term)
        if matched:
            return 0.85, tuple(matched)

        # 退而求其次：出现在正文前 300 字
        body_head = chunk.text[:300]
        for term in terms:
            if term in body_head:
                matched.append(term)
        if matched:
            return 0.55, tuple(matched)

        # 全文命中但位置靠后 → 弱相关
        for term in terms:
            if term in chunk.text:
                matched.append(term)
        if matched:
            return 0.3, tuple(matched)
        return 0.0, ()

    def search(
        self,
        query: str,
        *,
        target_account: str = "",
        top_k: Optional[int] = None,
    ) -> SearchOutcome:
        """执行一次混合检索。"""
        limit = top_k or self.top_k
        started = time.perf_counter()
        terms = self.expand_terms(target_account)

        # ---- 第 2 路：BM25 ----
        query_text = f"{target_account} {query}".strip()
        tokens = tokenize(query_text)
        bm25_scores: list[float] = []
        if self._bm25 is not None and tokens:
            raw = self._bm25.get_scores(tokens)
            peak = max(raw) if len(raw) else 0.0
            bm25_scores = [float(score) / peak if peak > 0 else 0.0 for score in raw]
        else:
            bm25_scores = [0.0] * len(self.chunks)

        wants_table = any(hint in query_text for hint in _TABLE_HINTS)

        scored: list[RetrievedChunk] = []
        for index, chunk in enumerate(self.chunks):
            route, matched = self._route_score(chunk, terms)
            bm25 = bm25_scores[index] if index < len(bm25_scores) else 0.0
            score = self.route_weight * route + self.bm25_weight * bm25
            if chunk.has_table and wants_table:
                score += 0.06
            if chunk.chunk_kind == "note":
                score += 0.04
            if score <= 0.01:
                continue
            scored.append(
                RetrievedChunk(
                    chunk_id=chunk.chunk_id,
                    text=chunk.text,
                    page_num=chunk.page_num,
                    page_end=chunk.page_end,
                    page_label=chunk.page_label,
                    section_title=chunk.section_title,
                    section_path=chunk.section_path,
                    pdf_period=chunk.pdf_period,
                    score=min(score, 1.0),
                    route_score=route,
                    bm25_score=bm25,
                    matched_terms=matched,
                    has_table=chunk.has_table,
                    chunk_kind=chunk.chunk_kind,
                )
            )

        scored.sort(key=lambda item: (-item.score, item.page_num))
        hits = scored[:limit]
        elapsed = time.perf_counter() - started
        stats = RetrievalStats(
            searched_chunks=len(self.chunks),
            top_score=hits[0].score if hits else 0.0,
            elapsed_s=elapsed,
            route_hits=sum(1 for hit in hits if hit.route_score >= 0.85),
            table_hits=sum(1 for hit in hits if hit.has_table),
        )
        return SearchOutcome(
            query=query, target_account=target_account, hits=hits, stats=stats
        )

    # -- 批量 ---------------------------------------------------------------
    def search_queries(
        self, queries: Iterable[dict[str, Any]], *, top_k: Optional[int] = None
    ) -> list[SearchOutcome]:
        """对 ``footnote_queries`` 逐条检索。"""
        outcomes: list[SearchOutcome] = []
        for item in queries:
            account = str(item.get("target_account", "") or "").strip()
            query = str(item.get("suggested_rag_query", "") or "").strip() or account
            outcomes.append(self.search(query, target_account=account, top_k=top_k))
        return outcomes


def build_retriever(document: RagDocument, **kwargs: Any) -> HybridRetriever:
    """便捷构造。"""
    return HybridRetriever(document, **kwargs)


def format_evidence_for_prompt(
    outcomes: Sequence[SearchOutcome], *, snippet_chars: int = 700, max_hits: int = 4
) -> str:
    """把检索结果渲染成给 LLM 的证据块（带页码与章节标题，强制可引用）。"""
    lines: list[str] = []
    for index, outcome in enumerate(outcomes, start=1):
        lines.append(
            f"### 查询 {index}：{outcome.target_account or '（未指定科目）'}\n"
            f"检索意图：{outcome.query}"
        )
        if not outcome.hits:
            lines.append("（该查询未命中任何附注切片）")
            continue
        for rank, hit in enumerate(outcome.hits[:max_hits], start=1):
            lines.append(
                f"[证据 {index}-{rank}] {hit.page_label} | 章节：{hit.section_path} | "
                f"含表：{'是' if hit.has_table else '否'} | 相关度 {hit.score:.2f}\n"
                f"{hit.snippet(snippet_chars)}"
            )
        lines.append("")
    return "\n".join(lines)
