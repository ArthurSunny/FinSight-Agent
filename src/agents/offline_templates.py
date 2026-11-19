"""离线确定性模板（无密钥时的降级响应）。

存在意义
--------
当 ``FINSIGHT_LLM_MODE=offline`` 或未配置 ``DEEPSEEK_API_KEY`` 时，LangGraph 仍然要能
**完整跑一遍**：三个并行节点 → 条件边 → 重试回路 → 质检 → 落盘。

因此本模块用**确定性模板**替代 LLM 推理：
* 所有数字直接从事实包（Markdown 表格）**原样搬运**，不做任何重算，
  因而天然通过 fact_checker 的锚点校验；
* 输出显式标注"离线模板"，避免被误当作真实 LLM 分析成果。

它绝不参与生产路径：只要检测到密钥，``DeepSeekClient.available`` 为真，本模块就不会被调用。
"""

from __future__ import annotations

import re
from typing import Any

from src.agents.prompts import (
    FACT_PACK_END,
    FACT_PACK_START,
    REPORT_TITLE,
    REQUIRED_SECTIONS,
)

_SECTION_SPLIT = re.compile(r"^##\s+(\d+)\.\s*(.+)$", re.MULTILINE)


def extract_fact_pack(user_text: str) -> str:
    """从用户消息中截取事实包。"""
    start = user_text.find(FACT_PACK_START)
    end = user_text.find(FACT_PACK_END)
    if start < 0 or end < 0:
        return user_text.strip()
    return user_text[start + len(FACT_PACK_START) : end].strip()


def _numbered_sections(pack: str) -> dict[str, str]:
    """按 ``## N. 标题`` 切分事实包，返回 ``{"N. 标题": 正文}``。"""
    matches = list(_SECTION_SPLIT.finditer(pack))
    sections: dict[str, str] = {}
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(pack)
        key = f"{match.group(1)}. {match.group(2).strip()}"
        sections[key] = pack[match.end() : end].strip()
    return sections


def _pick(sections: dict[str, str], prefix: str) -> str:
    for title, body in sections.items():
        if title.startswith(prefix):
            return body
    return ""


def _first_data_row(table_body: str) -> dict[str, str]:
    """从 Markdown 表格中取第一行数据，返回 ``{表头: 单元格}``。"""
    lines = [line.strip() for line in table_body.splitlines() if line.strip().startswith("|")]
    if len(lines) < 3:
        return {}
    headers = [cell.strip() for cell in lines[0].strip("|").split("|")]
    cells = [cell.strip() for cell in lines[2].strip("|").split("|")]
    return dict(zip(headers, cells))


def _triggered_rules(pack: str) -> list[str]:
    return re.findall(r"^###\s*\[触发[^\]]*\]\s*(.+?)（[A-Z_]+）", pack, re.MULTILINE)


def _default_footnote_queries(pack: str) -> list[dict[str, str]]:
    """基于命中规则生成附注核查清单（离线推导，逻辑与提示词要求保持一致）。"""
    queries: list[dict[str, str]] = []
    triggered = _triggered_rules(pack)

    if any("存货" in name for name in triggered):
        queries.append(
            {
                "target_account": "存货",
                "risk_reason": "存货周转率连续两年下滑且存货占流动资产比重抬升，触发存货积压预警，"
                "需区分战略备货与被动积压。",
                "suggested_rag_query": "年报附注-存货分类明细：基酒/半成品与成品酒构成、"
                "存货跌价准备计提比例及本期变动、产销量与在手订单情况",
                "priority": "high",
            }
        )
    if any("存贷双高" in name for name in triggered):
        queries.append(
            {
                "target_account": "货币资金",
                "risk_reason": "存贷双高规则触发，需核实货币资金可动用性与受限情况。",
                "suggested_rag_query": "年报附注-货币资金明细：库存现金、银行存款、其他货币资金的构成，"
                "使用受限的款项金额及原因（保证金/质押/冻结）",
                "priority": "high",
            }
        )
    if any("非经常性损益" in name for name in triggered):
        queries.append(
            {
                "target_account": "非经常性损益",
                "risk_reason": "非经常性损益依赖度较高，需评估其可持续性。",
                "suggested_rag_query": "年报附注-非经常性损益明细表：政府补助、资产处置损益、"
                "公允价值变动损益的构成与同比变动说明",
                "priority": "high",
            }
        )

    queries.append(
        {
            "target_account": "货币资金",
            "risk_reason": "常规核查项：货币资金余额与利息收入的匹配性，以及是否存在资金占用。",
            "suggested_rag_query": "年报附注-货币资金：期末余额构成、受限资金金额、"
            "利息收入与平均存款余额的匹配说明",
            "priority": "medium",
        }
    )
    queries.append(
        {
            "target_account": "营业收入确认",
            "risk_reason": "常规核查项：收入确认政策与合同负债（预收款）变动的匹配性。",
            "suggested_rag_query": "年报附注-收入确认政策与合同负债：主要销售模式、"
            "收入确认时点、合同负债期初期末变动及期后结转情况",
            "priority": "medium",
        }
    )
    queries.append(
        {
            "target_account": "有息负债与利息费用",
            "risk_reason": "常规核查项：有息负债规模、期限结构与利息资本化情况。",
            "suggested_rag_query": "年报附注-借款与应付债券：短期借款/长期借款/应付债券的期限结构、"
            "利率区间及利息费用资本化金额",
            "priority": "low",
        }
    )
    return queries


# ---------------------------------------------------------------------------
# 各 Agent 的离线 payload
# ---------------------------------------------------------------------------
def _fundamental_payload(pack: str) -> dict[str, Any]:
    sections = _numbered_sections(pack)
    row = _first_data_row(_pick(sections, "1. 核心业绩"))
    period = row.get("报告期", "最新年报")
    revenue = row.get("营业总收入", "数据缺失")
    parent_np = row.get("归母净利润", "数据缺失")
    gross = row.get("毛利率", "数据缺失")
    net = row.get("净利率", "数据缺失")
    dupont_body = _pick(sections, "2. 杜邦拆解")
    driver_match = re.search(r"主导驱动因子：\**([^\n*]+)", dupont_body)
    driver = driver_match.group(1).strip() if driver_match else "数据不足"
    attribution = re.search(r"连环替代法归因原文[^：]*：(.+)", dupont_body)
    attribution_text = attribution.group(1).strip() if attribution else "数据不足"

    return {
        "growth_verdict": f"离线模板：{period} 营业总收入 {revenue}、归母净利润 {parent_np}。",
        "revenue_and_profit_trend": (
            f"离线模板生成（未调用 LLM）。{period} 营业总收入为 {revenue}，归母净利润为 {parent_np}。"
            f"具体趋势判断需在配置 DEEPSEEK_API_KEY 后由真实模型给出。"
        ),
        "margin_and_expense": (
            f"{period} 毛利率 {gross}、净利率 {net}。费用端明细请参见事实包原始科目快照。"
        ),
        "dupont_attribution": f"连环替代法归因：{attribution_text}",
        "dupont_dominant_factor": driver,
        "cash_earnings_quality": "离线模板未展开现金流评价，详见事实包第 3 节表格。",
        "key_observations": [
            f"{period} 营业总收入 {revenue}",
            f"{period} 归母净利润 {parent_np}",
            f"{period} 毛利率 {gross}、净利率 {net}",
            f"ROE 主导驱动因子：{driver}",
        ],
        "data_citations": [
            {"metric": "营业总收入", "period": period, "value": revenue},
            {"metric": "归母净利润", "period": period, "value": parent_np},
            {"metric": "毛利率", "period": period, "value": gross},
        ],
    }


def _forensic_payload(pack: str) -> dict[str, Any]:
    sections = _numbered_sections(pack)
    beneish_body = _pick(sections, "6. Beneish")
    m_score_match = re.search(r"M-Score\s*=\s*(-?\d+\.\d+)", beneish_body)
    m_score = m_score_match.group(1) if m_score_match else "数据缺失"
    triggered = _triggered_rules(pack)
    cash_line = ""
    for line in _pick(sections, "7. 本土化排雷规则").splitlines():
        if "财务公司" in line:
            cash_line = line.strip("- ").strip()
            break

    return {
        "m_score_verdict": f"离线模板：M-Score = {m_score}（阈值 -1.78）。",
        "risk_level": "medium" if triggered else "low",
        "beneish_variable_review": [],
        "rule_review": [
            {"rule": name, "triggered": True, "assessment": "离线模板：详见事实包第 7 节证据。"}
            for name in triggered
        ],
        "cash_and_cce_reconciliation": cash_line or "离线模板：未从事实包中提取到货币资金勾稽结论。",
        "materiality_and_nonrecurring": "离线模板：详见事实包第 7 节相关规则证据。",
        "industry_hypotheses": ["离线模式未启用行业推理，相关假设需真实模型补充。"],
        "footnote_queries": _default_footnote_queries(pack),
    }


def _peer_payload(pack: str) -> dict[str, Any]:
    sections = _numbered_sections(pack)
    peer_body = _pick(sections, "9. 同业")
    available = "未提供可比同业数据" not in peer_body and "|" in peer_body
    return {
        "peer_available": available,
        "competitive_positioning": (
            "离线模板：已提供同业对标表，详见事实包第 9 节。"
            if available
            else "离线模板：本次未提供同业数据，改为单公司历史序列评估。"
        ),
        "profitability_barrier": "离线模板未展开，详见同业对标表。",
        "efficiency_and_cashflow": "离线模板未展开，详见同业对标表。",
        "balance_sheet_strength": "离线模板未展开，详见同业对标表。",
        "peer_relative_verdict": "离线模板不给出相对结论。",
        "key_gaps": [],
    }


def _peer_fallback(growth_section: str) -> str:
    """无同业数据时的离线降级内容：改用单公司历史序列评估（仍有实质信息）。"""
    return (
        "本次未提供可比同业数据（`--no-peers` 或同业取数失败），按设计改为**单公司历史序列评估**，"
        "严禁编造行业均值。以下为可用于历史分位比较的序列（直接摘自事实包第 1 节，未做二次计算）：\n\n"
        f"{growth_section}\n\n"
        "- 评估方法：把最新年度指标与自身近 3~4 年区间对比——高于区间上沿视为历史高位，"
        "低于下沿视为历史低位；ROE 与净利率优先看趋势方向而非单点绝对值。\n"
        "- 如需真正的行业横向对标，请以 `--peers 000858,000568` 重新运行本流程。"
    )


def _editor_markdown(pack: str) -> str:
    """拼装一份数值与事实包完全一致的离线报告（天然通过锚点校验）。"""
    sections = _numbered_sections(pack)
    growth = _pick(sections, "1. 核心业绩")
    dupont = _pick(sections, "2. 杜邦拆解")
    cash = _pick(sections, "3. 现金流")
    efficiency = _pick(sections, "4. 营运效率")
    beneish = _pick(sections, "6. Beneish")
    rules = _pick(sections, "7. 本土化排雷规则")
    quality = _pick(sections, "8. 数据质量")
    peer = _pick(sections, "9. 同业")

    row = _first_data_row(growth)
    period = row.get("报告期", "最新年报")
    revenue = row.get("营业总收入", "数据缺失")
    parent_np = row.get("归母净利润", "数据缺失")
    gross = row.get("毛利率", "数据缺失")
    net = row.get("净利率", "数据缺失")

    triggered = _triggered_rules(pack)
    queries = _default_footnote_queries(pack)
    query_table = "\n".join(
        f"| {item['target_account']} | {item['risk_reason']} | {item['suggested_rag_query']} "
        f"| {item['priority']} |"
        for item in queries
    )
    trigger_text = "、".join(triggered) if triggered else "无"

    cash_highlight = ""
    for line in rules.splitlines():
        if "财务公司" in line or "受限货币资金" in line:
            cash_highlight = line.strip("- ").strip()
            break

    return f"""# {REPORT_TITLE} —— {row.get("报告期", "")}（离线模板）

> 本文件由 FinSight-Agent **离线确定性模板**生成（未调用 LLM），仅用于验证工作流与数值一致性。
> 配置 `DEEPSEEK_API_KEY` 后重跑即可获得真实模型撰写的投研纪要。

## 一、核心投研结论与财务健康度总览

- {period} 营业总收入 {revenue}，归母净利润 {parent_np}，毛利率 {gross}，净利率 {net}。
- 触发的排雷规则：{trigger_text}。
- 数据质量：{quality.splitlines()[1].strip("- ").strip() if quality else "见事实包"}。
- 结论：本模板不给出投资评级，仅陈列确定性计算结果与待核查事项。

## 二、成长性与杜邦 ROE 归因拆解

{growth}

{dupont}

## 三、现金流含金量与资产负债表稳健性

{cash}

{efficiency}

- 货币资金勾稽要点：{cash_highlight or "详见事实包第 7 节「存贷双高嫌疑」证据。"}

## 四、CPA 财务排雷与 M-Score 审计意见

{beneish}

{rules}

## 五、同业横向竞争财务对标

{peer if peer else _peer_fallback(growth)}

## 六、年报附注穿透核查与尽调追问清单

> 离线确定性模板不渲染 RAG 证据（无 LLM 判定），以下为待核查清单；配置
> `DEEPSEEK_API_KEY` 后会先给出带 `P.xx` 页码的穿透核查结论。

| 待核查科目 | 风险原因 | 建议检索语句 | 优先级 |
|---|---|---|---|
{query_table}

> 以上清单将作为 Phase 3 年报 PDF RAG 的标准输入（结构见 `footnote_queries`）。
"""


def build_offline_payload(tag: str, user_text: str) -> dict[str, Any]:
    """按 tag 生成离线响应。"""
    pack = extract_fact_pack(user_text)
    if tag == "fundamental":
        return _fundamental_payload(pack)
    if tag == "forensic":
        return _forensic_payload(pack)
    if tag == "peer":
        return _peer_payload(pack)
    if tag.startswith("chief_editor"):
        return {"markdown": _editor_markdown(pack)}
    # 兜底：返回一个合法的空对象，避免离线模式抛错
    return {"note": "offline", "sections": [title for _, title in REQUIRED_SECTIONS]}
