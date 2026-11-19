"""Phase 2/3 · 多智能体协同投研与排雷工作流 —— 实盘闭环验证。

运行：:

    python tests/test_agent_workflow.py                 # 真实 DeepSeek API + 附注穿透（需 .env）
    python tests/test_agent_workflow.py --offline       # 确定性离线模式（无需密钥）
    python tests/test_agent_workflow.py --no-peer-run   # 只跑单股，不跑同业
    python tests/test_agent_workflow.py --no-rag        # 关闭 Phase 3 附注穿透

验证内容
--------
1. **零歧义格式化层**：小数比率必须被渲染成百分号字符串，金额带「亿元」，杜绝 100 倍误读；
2. **同业对标工具**：内置默认池解析、Markdown 对标表结构、目标行标记、异常波动护栏；
3. **确定性 fact_checker 的六个反例**：符号写反 / 单位写成万元 / 缺章节 / 正常通过 /
   漏写必转述提示 / 虚构报告期；
4. **LangGraph 全流程**：data_loader → 3 个并行节点 → 附注穿透 → chief_editor → fact_checker；
5. **footnote_queries**：非空、字段齐全、且精准覆盖存货附注核查（Phase 3 RAG 接口）；
6. **产物落盘**：Markdown 纪要 + 运行轨迹 JSON + 事实包；
7. **安全**：任何产物中都不允许出现 API Key 字符串；
8. **Phase 3 附注穿透**：`rag_evidences` 结构、页码必须落在检索证据范围内、
   报告正文必须引用 `P.xx` 且通过 fact_checker。

任一项失败即以非 0 退出码结束。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import traceback
from pathlib import Path
from typing import Any, Optional, Sequence

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (ValueError, OSError):
            pass

from src.agents.context_formatter import (  # noqa: E402
    FactPackBuilder,
    aggregate_warnings,
    fmt_amount,
    fmt_pct,
    fmt_pct_point,
    fmt_turnover,
)
from src.agents.fact_check import run_fact_check  # noqa: E402
from src.agents.graph import AgentWorkflow, WorkflowResult  # noqa: E402
from src.agents.llm_client import LLMConfig, mask_secret  # noqa: E402
from src.agents.prompts import REQUIRED_SECTIONS  # noqa: E402
from src.config import REPORT_OUTPUT_DIR, ensure_directories  # noqa: E402
from src.tools.financial_engine import analyze_stock  # noqa: E402
from src.tools.peer_comparison import compare_peers  # noqa: E402

TRANSCRIPT: list[str] = []
_SECRET_SCAN = re.compile(r"sk-[A-Za-z0-9]{16,}")


# ---------------------------------------------------------------------------
# 输出与断言
# ---------------------------------------------------------------------------
def emit(text: str = "") -> None:
    print(mask_secret(text))
    TRANSCRIPT.append(mask_secret(text))


def banner(title: str, char: str = "=") -> None:
    emit()
    emit(char * 96)
    emit(title)
    emit(char * 96)


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
        self.failed.append(label + (f"  ({detail})" if detail else ""))
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


# ---------------------------------------------------------------------------
# [1] 格式化层
# ---------------------------------------------------------------------------
def validate_formatter(checker: Checker, report) -> str:
    banner("[1] 零歧义事实包格式化层（消除小数/百分数误读）")

    checker.check(fmt_pct(-0.0120, signed=True) == "-1.20%", "同比小数 → 带符号百分数",
                  fmt_pct(-0.0120, signed=True))
    checker.check(fmt_pct(0.911796) == "91.18%", "水平比率 → 百分数", fmt_pct(0.911796))
    checker.check(fmt_pct_point(0.0344) == "+3.44pct", "百分点差 → pct 单位", fmt_pct_point(0.0344))
    checker.check(fmt_amount(1720.5417) == "1720.5417 亿元", "金额 → 亿元", fmt_amount(1720.5417))
    checker.check(fmt_turnover(0.5602) == "0.5602 次", "周转率 → 次", fmt_turnover(0.5602))
    checker.check(fmt_amount(None) == "数据缺失", "缺失值 → 数据缺失（不填 0）", fmt_amount(None))

    pack = FactPackBuilder().build(report)
    text = pack.text
    emit(f"  事实包：{len(text)} 字符 / 约 {pack.estimated_tokens} tokens / 锚点 {len(pack.anchors)} 个")

    checker.check("亿元" in text and text.count("亿元") > 40, "金额统一带「亿元」",
                  f"{text.count('亿元')} 处")
    checker.check(text.count("%") > 20, "比率统一带「%」", f"{text.count('%')} 处")
    checker.check("次" in text and "天" in text, "周转率带「次」、天数带「天」")
    checker.check("2025年报" in text, "所有指标带报告期标签")
    checker.check("-0.012001" not in text and "0.911796" not in text,
                  "原始小数比率未直接暴露给 LLM（已被渲染为百分数）")
    checker.check(len(pack.anchors) >= 6, "核心锚点数量 ≥ 6", f"{len(pack.anchors)} 个")
    checker.check(len(pack.warning_summary) <= 12, "warnings 已聚合去重（≤12 行）",
                  f"{len(pack.warning_summary)} 行")
    checker.check(
        any("科目为空" in line for line in pack.warning_summary),
        "缺失科目已合并为一行摘要",
        pack.warning_summary[0][:60] if pack.warning_summary else "无",
    )
    return text


# ---------------------------------------------------------------------------
# [2] 同业对标
# ---------------------------------------------------------------------------
def validate_peer(checker: Checker) -> Any:
    banner("[2] 同业横向对标工具（复用已验证引擎 + 本地缓存）")
    started = time.perf_counter()
    comparison = compare_peers("600519", ["000858", "000568"], lookback_years=3)
    elapsed = time.perf_counter() - started

    emit(f"  同业池：{comparison.pool_source}")
    emit(f"  参与对比：{[row.stock_code for row in comparison.rows]}（耗时 {elapsed:.2f}s，含缓存复用）")
    if comparison.skipped:
        emit(f"  跳过：{comparison.skipped}")

    checker.check(len(comparison.rows) >= 2, "至少含目标 + 1 家同业",
                  f"{len(comparison.rows)} 行")
    checker.check(comparison.rows[0].is_target and comparison.rows[0].stock_code == "600519",
                  "首行为目标公司且已标记 is_target")
    checker.check(bool(comparison.rows[0].m_score), "目标公司 M-Score 已计算",
                  f"{comparison.rows[0].m_score}")
    checker.check("| 指标 |" in comparison.markdown_table, "Markdown 对标表结构正确")
    checker.check(
        all(code in comparison.markdown_table for code in comparison.peer_codes),
        "对标表包含全部同业代码",
        ", ".join(comparison.peer_codes),
    )
    checker.check(
        any("亿" in line for line in comparison.markdown_table.splitlines()),
        "对标表标注了金额单位",
    )
    abnormal = [
        row
        for row in comparison.rows
        if row.revenue_yoy is not None and abs(row.revenue_yoy) > 0.40
    ]
    if abnormal:
        checker.check(
            all(row.data_caution for row in abnormal) and "数据提示" in comparison.markdown_table,
            "同业营收异常波动已触发数据可信度护栏",
            f"{[row.stock_code for row in abnormal]}",
        )
    else:
        checker.skip("同业异常波动提示", "本次样本无异常波动")
    emit()
    for line in comparison.markdown_table.splitlines()[:8]:
        emit("    " + line)
    return comparison


# ---------------------------------------------------------------------------
# [3] fact_check 反例
# ---------------------------------------------------------------------------
_FILLER = "本节用于验证章节完整性与字数下限判断逻辑是否生效，内容为自动生成的测试占位文本。" * 8


def _synthetic_report(value: float = 1720.5417, unit: str = "亿元") -> tuple[str, list[dict]]:
    anchors = [
        {
            "key": "total_operating_revenue",
            "label": "营业总收入",
            "expected_number": value,
            "kind": "amount",
            "period": "2025年报",
            "tolerance_abs": 0.01,
            "display": f"{value:.4f} 亿元",
        }
    ]
    body = f"""# FinSight 深度财务体检与排雷纪要（Tear Sheet）

## 一、核心投研结论与财务健康度总览
2025年报 营业总收入 {value:.4f} {unit}。{_FILLER}

## 二、成长性与杜邦 ROE 归因拆解
{_FILLER}ROE 归因涉及销售净利率、总资产周转率与权益乘数。

## 三、现金流含金量与资产负债表稳健性
{_FILLER}

## 四、CPA 财务排雷与 M-Score 审计意见
{_FILLER}

## 五、同业横向竞争财务对标
{_FILLER}

## 六、尽调追问与待核查年报附注清单
{_FILLER}
"""
    return body, anchors


def validate_fact_check(checker: Checker) -> None:
    banner("[3] 确定性质检（fact_checker）四个反例")

    good_md, anchors = _synthetic_report()
    good = run_fact_check(good_md, anchors, allowed_periods=["2025年报"])
    checker.check(good.passed, "正常报告 → 通过", f"字符 {good.stats['chars']}")

    flip_md, anchors_flip = _synthetic_report(value=-1720.5417)
    flip = run_fact_check(flip_md, anchors, allowed_periods=["2025年报"])
    checker.check(
        not flip.passed and any("符号写反" in item for item in flip.errors),
        "正负号写反 → 被识别为「符号写反」",
        flip.errors[0][:40] if flip.errors else "无",
    )

    unit_md, _ = _synthetic_report(unit="万元")
    unit = run_fact_check(unit_md, anchors, allowed_periods=["2025年报"])
    checker.check(
        not unit.passed and any("万元" in item for item in unit.errors),
        "把亿元写成万元 → 单位错误被识别",
    )

    missing_md = good_md.split("## 五、")[0]
    missing = run_fact_check(missing_md, anchors, allowed_periods=["2025年报"])
    checker.check(
        not missing.passed and any("缺少必选章节" in item for item in missing.errors),
        "缺少章节 → 被识别为章节缺失",
    )

    period_md = good_md.replace("2025年报 营业总收入", "2021年报 营业总收入", 1)
    period = run_fact_check(period_md, anchors, allowed_periods=["2025年报"])
    checker.check(
        not period.passed and any("事实包之外的报告期" in item for item in period.errors),
        "虚构报告期（2021年报）→ 被拦截",
    )

    phrase_rule = (("同业数据可信度提示", ("数据可信度", "待核实参考")),)
    missing_phrase = run_fact_check(
        good_md, anchors, allowed_periods=["2025年报"], required_phrases=phrase_rule
    )
    checker.check(
        not missing_phrase.passed
        and any("必须转述" in item for item in missing_phrase.errors),
        "漏写必转述的数据质量提示 → 被拦截",
    )
    with_phrase = run_fact_check(
        good_md + "\n> 同业数据可信度提示：五粮液营收同比异常，仅作待核实参考。\n",
        anchors,
        allowed_periods=["2025年报"],
        required_phrases=phrase_rule,
    )
    checker.check(with_phrase.passed, "补上转述后 → 通过")


# ---------------------------------------------------------------------------
# [4][5] 工作流实盘
# ---------------------------------------------------------------------------
def render_result(result: WorkflowResult, checker: Checker, label: str, *, expect_peers: bool) -> None:
    trace_nodes = [item.get("node") for item in result.trace]
    emit(f"  节点执行顺序：{' → '.join(str(node) for node in trace_nodes)}")
    emit(f"  模式 {result.mode} | 耗时 {result.elapsed_s:.2f}s | 重写 {result.retry_count} 次 | "
         f"Token {result.total_tokens}")

    required_nodes = {
        "data_loader",
        "fundamental",
        "forensic",
        "peer",
        "chief_editor",
        "fact_checker",
    }
    checker.check(required_nodes.issubset(set(trace_nodes)), f"{label}：六个节点全部执行",
                  f"缺失 {required_nodes - set(trace_nodes) or '无'}")
    checker.check(trace_nodes[0] == "data_loader", f"{label}：首节点为确定性装载")
    checker.check(trace_nodes[-1] == "fact_checker", f"{label}：末节点为质检")

    checker.check(result.fact_check.get("passed") is True, f"{label}：fact_checker 质检通过",
                  json.dumps(result.fact_check.get("stats", {}), ensure_ascii=False))
    checker.check(result.retry_count <= 2, f"{label}：重试次数未超上限",
                  f"{result.retry_count}")

    # ---- footnote_queries（Phase 3 接口）----
    checker.check(
        not result.forensic.get("error"),
        f"{label}：审计节点未降级（无 error 字段）",
        str(result.forensic.get("error", ""))[:60],
    )
    queries = result.forensic.get("footnote_queries") or []
    checker.check(isinstance(queries, list) and len(queries) >= 3,
                  f"{label}：footnote_queries 非空且 ≥3 条", f"{len(queries)} 条")
    schema_ok = len(queries) >= 3 and all(
        isinstance(item, dict)
        and {"target_account", "risk_reason", "suggested_rag_query"}.issubset(item.keys())
        for item in queries
    )
    checker.check(schema_ok, f"{label}：每条查询含 target_account/risk_reason/suggested_rag_query")
    joined = json.dumps(queries, ensure_ascii=False)
    checker.check("存货" in joined, f"{label}：查询精准命中存货附注核查")
    emit("  footnote_queries 摘录：")
    for item in queries[:3]:
        account = item.get("target_account", "?")
        query = str(item.get("suggested_rag_query", ""))[:70]
        emit(f"    · [{account}] {query}…")

    # ---- 报告结构 ----
    markdown = result.markdown
    missing_sections = [
        f"{idx}、{title}" for idx, title in REQUIRED_SECTIONS if f"{idx}、" not in markdown
    ]
    checker.check(not missing_sections, f"{label}：六章节齐全", f"缺 {missing_sections or '无'}")
    checker.check("万元" not in markdown, f"{label}：报告未出现「万元」")
    checker.check(len(markdown) > 2000, f"{label}：报告长度充足", f"{len(markdown)} 字符")

    # ---- 落盘 ----
    checker.check(
        result.markdown_path is not None and result.markdown_path.exists()
        and result.markdown_path.stat().st_size > 2000,
        f"{label}：Markdown 纪要已落盘",
        str(result.markdown_path),
    )
    checker.check(
        result.trace_path is not None and result.trace_path.exists(),
        f"{label}：运行轨迹 JSON 已落盘",
        str(result.trace_path),
    )
    checker.check(
        result.fact_pack_path is not None and result.fact_pack_path.exists(),
        f"{label}：事实包已落盘",
        str(result.fact_pack_path),
    )
    if result.trace_path is not None and result.trace_path.exists():
        payload = json.loads(result.trace_path.read_text(encoding="utf-8"))
        checker.check(
            payload.get("tokens", {}).get("total", 0) == result.total_tokens,
            f"{label}：轨迹中的 Token 记账与结果一致",
            f"{result.total_tokens}",
        )

    if expect_peers:
        checker.check(
            result.peer is not None and getattr(result.peer, "available", False),
            f"{label}：同业对标数据已进入工作流",
            ", ".join(result.peer.peer_codes) if result.peer else "无",
        )
        checker.check(
            "| 指标 |" in markdown or "对标" in markdown,
            f"{label}：报告中包含同业对标内容",
        )
        caveat_present = (
            result.peer is not None
            and any(row.data_caution for row in result.peer.rows)
        )
        if caveat_present:
            checker.check(
                any(token in markdown for token in ("同业数据可信度", "数据可信度提示", "待核实参考")),
                f"{label}：已转述同业数据可信度提示（确定性护栏）",
            )
    if result.mode == "api":
        checker.check(result.total_tokens > 0, f"{label}：真实 API 产生 Token 消耗",
                      f"{result.total_tokens} tokens")
    else:
        checker.skip(f"{label}：Token 消耗断言", "离线模式无 Token")


def validate_rag(result: WorkflowResult, checker: Checker, label: str) -> None:
    """Phase 3：附注穿透证据的结构、页码与报告引用。"""
    meta = result.rag_meta or {}
    evidences = (result.rag_evidences or {}).get("evidences") or []
    if not meta.get("available"):
        checker.skip(f"{label}：附注穿透", str(meta.get("reason") or "RAG 未启用"))
        return

    checker.check(meta.get("page_count", 0) >= 50, f"{label}：PDF 页数合理",
                  f"{meta.get('page_count')} 页")
    checker.check(meta.get("note_chunks", 0) >= 10, f"{label}：附注切片充足",
                  f"{meta.get('note_chunks')} 个")
    checker.check(bool(evidences), f"{label}：rag_evidences 非空", f"{len(evidences)} 条")
    if not evidences:
        return

    schema_ok = all(
        {"target_account", "verdict", "finding_summary", "citations"}.issubset(item.keys())
        for item in evidences
    )
    checker.check(schema_ok, f"{label}：每条证据含 target_account/verdict/finding_summary/citations")

    verdicts_ok = all(
        item.get("verdict") in {"verified_benign", "verified_risk", "inconclusive"}
        for item in evidences
    )
    checker.check(verdicts_ok, f"{label}：verdict 取值合法",
                  str([item.get("verdict") for item in evidences]))

    all_citations = [
        {**citation, "_account": item.get("target_account")}
        for item in evidences
        for citation in item.get("citations") or []
    ]
    if not all_citations:
        if result.mode == "offline":
            checker.skip(f"{label}：引用页码断言", "离线模式无 LLM 判定，不产生页码引用")
            return
        checker.check(False, f'{label}：引用带 "P.xx" 页码', "0 条引用（真实 API 不应为空）")
        return
    page_pattern = re.compile(r"^P\.\d+")
    checker.check(
        bool(all_citations) and all(page_pattern.match(str(c.get("page", ""))) for c in all_citations),
        f"{label}：引用带 \"P.xx\" 页码",
        f"{len(all_citations)} 条引用",
    )
    checker.check(
        all(c.get("pdf_period") for c in all_citations),
        f"{label}：引用标注 PDF 报告期",
        str({c.get("pdf_period") for c in all_citations}),
    )
    unverified = [c for c in all_citations if not c.get("verified")]
    checker.check(
        all(
            item.get("verdict") == "inconclusive"
            for item in evidences
            if not all(c.get("verified") for c in item.get("citations") or [])
        ),
        f"{label}：含未核实页码的结论均已降级为 inconclusive",
        f"未核实引用 {len(unverified)} 条（已按要求标记并降级）",
    )
    checker.check(
        any("存货" in str(item.get("target_account", "")) for item in evidences),
        f"{label}：附注核查覆盖存货",
    )

    emit(f"  PDF：{meta.get('pdf_period')}｜{meta.get('page_count')} 页｜"
         f"{meta.get('chunks')} 切片（附注 {meta.get('note_chunks')}）｜解析 {meta.get('parse_elapsed_s')}s")
    for item in evidences:
        pages = "、".join(c.get("page", "") for c in item.get("citations") or [])
        emit(f"    · [{item.get('verdict')}] {item.get('target_account')}  [{pages}] "
             f"{str(item.get('finding_summary', ''))[:70]}…")

    verified_pages = {c["page"] for c in all_citations if c.get("verified")}
    missing = sorted(page for page in verified_pages if page not in result.markdown)
    checker.check(not missing, f"{label}：报告正文引用了已核实的附注页码",
                  f"缺失 {missing}" if missing else "、".join(sorted(verified_pages)))


# ---------------------------------------------------------------------------
# [6] 安全审计
# ---------------------------------------------------------------------------
def validate_security(checker: Checker, result: WorkflowResult) -> None:
    banner("[6] 安全审计（密钥绝不外泄）")
    config = LLMConfig.from_env()
    key = config.api_key
    if not key:
        checker.skip("密钥泄露扫描", "当前未配置密钥（离线模式）")
        return

    checker.check(len(key) > 10, "已读取到密钥（仅校验长度，不打印内容）", config.key_status)
    targets = [
        path
        for path in (
            result.markdown_path,
            result.trace_path,
            result.fact_pack_path,
            REPORT_OUTPUT_DIR / "agent_validation_transcript.txt",
        )
        if path is not None and Path(path).exists()
    ]
    leaked = []
    for path in targets:
        content = Path(path).read_text(encoding="utf-8", errors="ignore")
        if key in content or _SECRET_SCAN.search(content):
            leaked.append(str(path))
    checker.check(not leaked, "产物中未发现密钥或 sk- 字符串", ", ".join(leaked) or "全部干净")
    masked = mask_secret(f"http://x?key={key}")
    checker.check(key not in masked and "sk-****" in masked, "日志脱敏函数生效", masked[:40])


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Phase 2 多智能体工作流实盘验证")
    parser.add_argument("--code", default="600519", help="主验证标的")
    parser.add_argument("--peers", default="000858,000568", help="同业代码")
    parser.add_argument("--offline", action="store_true", help="强制离线确定性模式")
    parser.add_argument("--no-peer-run", action="store_true", help="跳过带同业的第二次工作流")
    parser.add_argument("--no-tools", action="store_true", help="跳过工具层单元断言（省时）")
    parser.add_argument("--excerpt-lines", type=int, default=60, help="打印报告摘录行数")
    parser.add_argument("--no-rag", action="store_true", help="关闭 Phase 3 年报附注穿透")
    parser.add_argument("--pdf", default=None, help="指定本地年报 PDF 路径")
    return parser


def _write_transcript() -> Path:
    ensure_directories()
    target = REPORT_OUTPUT_DIR / "agent_validation_transcript.txt"
    target.write_text("\n".join(TRANSCRIPT), encoding="utf-8")
    return target


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    ensure_directories()
    checker = Checker()
    started_total = time.perf_counter()

    banner("FinSight-Agent Phase 2 · 多智能体协同投研与排雷 · 实盘验证")
    config = LLMConfig.from_env()
    emit(f"  运行时间：{time.strftime('%Y-%m-%d %H:%M:%S')}")
    emit(f"  LLM 配置：mode={config.mode} model={config.model} base_url={config.base_url} "
         f"api_key={config.key_status}")
    mode_text = "离线确定性模式" if (args.offline or not config.api_key) else "真实 DeepSeek API"
    emit(f"  目标模式：{mode_text}")

    # ---- 准备：一次引擎调用，供格式化层断言复用（走缓存） ----
    banner("[0] 装载 Phase 1 结构化报告（唯一数值事实来源）")
    try:
        report = analyze_stock(args.code, lookback_years=4)
    except Exception as exc:  # noqa: BLE001
        emit(f"[FATAL] 无法装载 {args.code} 的财务报告：{type(exc).__name__}: {exc}")
        traceback.print_exc()
        _write_transcript()
        return 1
    emit(f"  {report.meta.stock_code} {report.meta.stock_name}｜{report.meta.latest_report_period}｜"
         f"覆盖 {len(report.meta.report_periods)} 年｜风险等级 {report.forensic.risk_level}")
    checker.check(report.meta.latest_report_period != "", "报告装载成功")

    # ---- [1] 格式化层 ----
    fact_pack_text = validate_formatter(checker, report)

    # ---- [2] 同业对标 ----
    if args.no_tools:
        checker.skip("同业对标工具断言", "--no-tools")
    else:
        try:
            validate_peer(checker)
        except Exception as exc:  # noqa: BLE001
            checker.check(False, "同业对标工具执行", f"{type(exc).__name__}: {exc}")

    # ---- [3] 质检反例 ----
    validate_fact_check(checker)

    # ---- [4] 工作流 A：单股 ----
    banner("[4] LangGraph 实盘 A：单股模式（--no-peers）")
    try:
        workflow = AgentWorkflow(offline=args.offline, verbose=True)
        result_single = workflow.run(
            args.code,
            peer_codes=[],
            lookback_years=4,
            rag_enabled=not args.no_rag,
            pdf_path=args.pdf,
        )
    except Exception as exc:  # noqa: BLE001
        emit(f"[FATAL] 单股工作流失败：{type(exc).__name__}: {exc}")
        traceback.print_exc()
        _write_transcript()
        return 1
    render_result(result_single, checker, "单股", expect_peers=False)
    validate_rag(result_single, checker, "单股")

    # ---- [5] 工作流 B：带同业 ----
    result_peer: Optional[WorkflowResult] = None
    if args.no_peer_run:
        checker.skip("带同业工作流", "--no-peer-run")
    else:
        banner("[5] LangGraph 实盘 B：带同业（默认池 000858,000568）")
        peers = [item.strip() for item in args.peers.split(",") if item.strip()]
        try:
            result_peer = workflow.run(
                args.code,
                peer_codes=peers,
                lookback_years=4,
                rag_enabled=not args.no_rag,
                pdf_path=args.pdf,
            )
            render_result(result_peer, checker, "带同业", expect_peers=True)
            validate_rag(result_peer, checker, "带同业")
        except Exception as exc:  # noqa: BLE001
            checker.check(False, "带同业工作流执行", f"{type(exc).__name__}: {exc}")

    # ---- [6] 安全 ----
    validate_security(checker, result_peer or result_single)

    # ---- 报告摘录 ----
    final_result = result_peer or result_single
    banner(f"[7] 生成报告摘录（{final_result.markdown_path}）")
    lines = final_result.markdown.splitlines()
    for line in lines[: args.excerpt_lines]:
        emit(line)
    if len(lines) > args.excerpt_lines:
        emit(f"……（其余 {len(lines) - args.excerpt_lines} 行见文件）")

    # ---- 汇总 ----
    banner("[8] 运行汇总")
    total_elapsed = time.perf_counter() - started_total
    emit(f"  总耗时：{total_elapsed:.1f}s（单股 {result_single.elapsed_s:.1f}s"
         + (f" / 带同业 {result_peer.elapsed_s:.1f}s" if result_peer else "") + "）")
    for label, res in (("单股", result_single), ("带同业", result_peer)):
        if res is None:
            continue
        emit(f"  {label}：mode={res.mode} tokens={res.total_tokens}"
             f"（prompt {res.prompt_tokens} / completion {res.completion_tokens}）"
             f" 调用 {len(res.usage)} 次 重写 {res.retry_count} 次")
    emit(f"  报告：{final_result.markdown_path}")
    emit(f"  轨迹：{final_result.trace_path}")
    emit(f"  事实包字符数：{len(fact_pack_text)}")

    exit_code = checker.summary()
    transcript = _write_transcript()
    emit(f"  文本副本：{transcript}")
    emit()
    emit("  >>> 全部断言通过：Phase 2 多智能体工作流验证成功。" if exit_code == 0
         else "  >>> 存在失败断言，请修复后重跑。")
    _write_transcript()
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
