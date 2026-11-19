"""Phase 4 UI 主题：机构级配色、字体与全局 CSS。

设计原则
--------
* 配色克制：券商深蓝（主）+ 石板灰（辅）+ 琥珀金（强调）+ 警示红（风险），
  不用高饱和色块与 Emoji，避免"玩具感"；
* 字体优先 Windows/macOS 自带中文字体，保证开箱不依赖外部字体文件；
* 所有样式集中在 :func:`inject_css`，页面本身只调用语义化的 CSS class。
"""

from __future__ import annotations

from typing import Final

# ---------------------------------------------------------------------------
# 配色（同时供 Plotly 与 CSS 使用，保证图表与页面同色系）
# ---------------------------------------------------------------------------
NAVY: Final = "#1F3A5F"      # 券商深蓝：主色
NAVY_LIGHT: Final = "#33587F"
SLATE: Final = "#5B6B7B"     # 石板灰：次要信息
GOLD: Final = "#C9A227"      # 琥珀金：强调 / 基准线
RED: Final = "#A93226"       # 警示红：风险 / 负向
GREEN: Final = "#1E6F5C"     # 稳健绿：正向
INK: Final = "#1B2430"       # 文字主色
MUTED: Final = "#7A8794"     # 弱化文字
GRID: Final = "#DCE3EA"      # 网格线
PANEL: Final = "#F6F8FA"     # 面板底色
BORDER: Final = "#D8E0E8"

#: 多公司对比的序列色（深蓝 → 石板 → 金 → 绿 → 红）
SERIES_COLORS: Final[tuple[str, ...]] = (NAVY, SLATE, GOLD, GREEN, RED)

#: 图表统一字体
FONT_FAMILY: Final = '"Microsoft YaHei", "PingFang SC", "Hiragino Sans GB", "Source Han Sans SC", sans-serif'

#: 风险等级 → (中文标签, 颜色)
RISK_STYLES: Final[dict[str, tuple[str, str]]] = {
    "high": ("高风险", RED),
    "medium": ("中风险", GOLD),
    "low": ("低风险", GREEN),
    "unknown": ("待判定", MUTED),
}

#: verdict → (中文标签, 颜色)
VERDICT_STYLES: Final[dict[str, tuple[str, str]]] = {
    "verified_benign": ("良性证实", GREEN),
    "verified_risk": ("风险证实", RED),
    "inconclusive": ("待人工核查", GOLD),
}


def risk_badge(level: str | None) -> tuple[str, str]:
    """风险等级 → (标签, 颜色)。"""
    return RISK_STYLES.get((level or "unknown").lower(), RISK_STYLES["unknown"])


def verdict_badge(verdict: str | None) -> tuple[str, str]:
    """RAG 结论 → (标签, 颜色)。"""
    return VERDICT_STYLES.get((verdict or "inconclusive").lower(), VERDICT_STYLES["inconclusive"])


# ---------------------------------------------------------------------------
# 全局 CSS
# ---------------------------------------------------------------------------
GLOBAL_CSS: Final[str] = f"""
<style>
:root {{
  --fs-navy: {NAVY};
  --fs-slate: {SLATE};
  --fs-gold: {GOLD};
  --fs-red: {RED};
  --fs-green: {GREEN};
  --fs-ink: {INK};
  --fs-muted: {MUTED};
  --fs-border: {BORDER};
  --fs-panel: {PANEL};
}}

/* ---------- 页面骨架 ---------- */
.block-container {{ padding-top: 2.2rem; padding-bottom: 3rem; max-width: 1500px; }}
header[data-testid="stHeader"] {{ background: transparent; }}

/* ---------- 品牌行 ---------- */
.fs-brand {{
  display: flex; align-items: baseline; gap: .75rem;
  border-bottom: 3px solid var(--fs-navy); padding-bottom: .55rem; margin-bottom: .35rem;
}}
.fs-brand-title {{
  font-size: 1.42rem; font-weight: 700; color: var(--fs-navy); letter-spacing: .01em;
}}
.fs-brand-sub {{ font-size: .8rem; color: var(--fs-muted); }}

/* ---------- KPI 条 ---------- */
.fs-kpi-grid {{
  display: grid; grid-template-columns: repeat(auto-fit, minmax(158px, 1fr));
  gap: .6rem; margin: .8rem 0 1.1rem 0;
}}
.fs-kpi {{
  background: #fff; border: 1px solid var(--fs-border); border-top: 3px solid var(--fs-navy);
  border-radius: 4px; padding: .62rem .75rem .58rem .75rem;
}}
.fs-kpi.risk-high {{ border-top-color: var(--fs-red); }}
.fs-kpi.risk-medium {{ border-top-color: var(--fs-gold); }}
.fs-kpi.risk-low {{ border-top-color: var(--fs-green); }}
.fs-kpi-label {{ font-size: .72rem; color: var(--fs-muted); letter-spacing: .04em; margin-bottom: .18rem; }}
.fs-kpi-value {{ font-size: 1.24rem; font-weight: 700; color: var(--fs-ink); line-height: 1.25; }}
.fs-kpi-sub {{ font-size: .73rem; margin-top: .12rem; }}
.fs-up {{ color: var(--fs-green); }}
.fs-down {{ color: var(--fs-red); }}
.fs-flat {{ color: var(--fs-muted); }}

/* ---------- 徽章 ---------- */
.fs-badge {{
  display: inline-block; padding: .12rem .5rem; border-radius: 3px; font-size: .72rem;
  font-weight: 600; letter-spacing: .02em;
}}
.fs-badge-solid {{ color: #fff; }}
.fs-badge-outline {{ background: #fff; border: 1px solid currentColor; }}

/* ---------- 面板 / 卡片 ---------- */
.fs-panel {{
  background: var(--fs-panel); border: 1px solid var(--fs-border);
  border-left: 3px solid var(--fs-navy); border-radius: 4px;
  padding: .7rem .9rem; margin: .5rem 0 .9rem 0; font-size: .86rem; color: var(--fs-ink);
}}
.fs-panel.warn {{ border-left-color: var(--fs-gold); background: #FDF8EC; }}
.fs-panel.danger {{ border-left-color: var(--fs-red); background: #FBF1F0; }}
.fs-panel.ok {{ border-left-color: var(--fs-green); background: #F1F7F4; }}
.fs-panel-title {{ font-weight: 700; color: var(--fs-navy); margin-bottom: .22rem; font-size: .88rem; }}

.fs-evidence {{
  border: 1px solid var(--fs-border); border-radius: 4px; padding: .65rem .8rem;
  margin-bottom: .55rem; background: #fff;
}}
.fs-evidence-head {{ display: flex; align-items: center; gap: .5rem; flex-wrap: wrap; }}
.fs-evidence-account {{ font-weight: 700; color: var(--fs-ink); font-size: .95rem; }}
.fs-evidence-body {{ font-size: .84rem; color: #33414F; margin-top: .35rem; line-height: 1.62; }}

/* ---------- 页签 ---------- */
.stTabs [data-baseweb="tab-list"] {{ gap: .25rem; border-bottom: 1px solid var(--fs-border); }}
.stTabs [data-baseweb="tab"] {{
  height: 2.5rem; padding: 0 1rem; font-size: .88rem; color: var(--fs-slate); border-radius: 0;
}}
.stTabs [aria-selected="true"] {{
  color: var(--fs-navy); font-weight: 700; border-bottom: 2px solid var(--fs-navy);
}}

/* ---------- 侧边栏 ---------- */
section[data-testid="stSidebar"] {{ background: #FBFCFD; border-right: 1px solid var(--fs-border); }}
section[data-testid="stSidebar"] .fs-panel {{ background: #fff; }}

/* ---------- 表格 ---------- */
.fs-table {{ width: 100%; border-collapse: collapse; font-size: .82rem; }}
.fs-table th, .fs-table td {{ border: 1px solid var(--fs-border); padding: .34rem .5rem; }}
.fs-table th {{ background: #EDF1F5; color: var(--fs-navy); font-weight: 600; text-align: left; }}
.fs-table td.num {{ text-align: right; font-variant-numeric: tabular-nums; }}
.fs-table tr:nth-child(even) td {{ background: #FAFBFC; }}

/* ---------- Markdown 报告渲染 ---------- */
.fs-report h1 {{ font-size: 1.5rem; color: var(--fs-navy); border-bottom: 3px solid var(--fs-navy);
  padding-bottom: .4rem; margin: .2rem 0 1rem 0; }}
.fs-report h2 {{ font-size: 1.14rem; color: var(--fs-navy); margin: 1.5rem 0 .6rem 0;
  border-left: 4px solid var(--fs-gold); padding-left: .55rem; }}
.fs-report h3 {{ font-size: 1rem; color: var(--fs-slate); margin: 1.1rem 0 .45rem 0; }}
.fs-report table {{ border-collapse: collapse; width: 100%; margin: .7rem 0 1rem 0; font-size: .82rem; }}
.fs-report th, .fs-report td {{ border: 1px solid var(--fs-border); padding: .34rem .52rem; }}
.fs-report th {{ background: #EDF1F5; color: var(--fs-navy); }}
.fs-report tr:nth-child(even) td {{ background: #FAFBFC; }}
.fs-report blockquote {{ border-left: 3px solid var(--fs-gold); background: #FDF8EC;
  margin: .6rem 0; padding: .5rem .8rem; color: #4A5765; font-size: .85rem; }}
.fs-report code {{ background: #EFF3F7; padding: .05rem .3rem; border-radius: 3px; font-size: .84em; }}
.fs-report li {{ margin: .18rem 0; }}

/* ---------- 隐藏 Streamlit 默认装饰 ---------- */
#MainMenu, footer, .stDeployButton {{ visibility: hidden; }}
</style>
"""


def inject_css() -> None:
    """把全局样式注入 Streamlit 页面（在 app 顶部调用一次）。"""
    import streamlit as st

    st.markdown(GLOBAL_CSS, unsafe_allow_html=True)
