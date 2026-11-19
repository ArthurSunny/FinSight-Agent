"""财务数据获取与本地缓存层（数据层，**不含任何财务比率计算**）。

职责边界（单一职责）
--------------------
1. 把 6 位 A 股代码转换为东方财富年度报表接口所需的带市场前缀代码（``SH600519``）；
2. 调用 akshare 的**年度**三表接口获取【资产负债表 / 利润表 / 现金流量表】；
3. 落地为本地 CSV 缓存 + JSON 元数据，避免频繁请求被限流（默认 TTL 12 小时）；
4. 统一列名数值化、按报告年度去重排序、剔除"尚未披露"的报告期占位行。

本模块刻意**不做**任何财务比率计算 —— 计算全部在 :mod:`src.tools.financial_engine` 中完成，
以保证"数据层 / 计算层"分离，任何数字都能独立复算与回溯（AGENTS.md 铁律 1、2）。

接口与字段的实测依据见 AGENTS.md 第 2 节，**不得凭记忆猜测列名**。
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import pandas as pd

from src.config import DEFAULT_CACHE_TTL_HOURS, STATEMENT_CACHE_DIR, ensure_directories
from src.tools.diagnostics import WarningCollector


class DataUnavailableError(RuntimeError):
    """无法获取到编制报告所必需的数据（通常为代码不存在或接口异常）。"""


# ---------------------------------------------------------------------------
# 报表规格
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class TableSpec:
    """单张报表的数据源接口定义。"""

    key: str
    interface: str
    label: str


#: 三张报表的接口映射（已实测，见 AGENTS.md 2.1）
REPORT_TABLES: tuple[TableSpec, ...] = (
    TableSpec("balance_sheet", "stock_balance_sheet_by_yearly_em", "资产负债表"),
    TableSpec("profit_sheet", "stock_profit_sheet_by_yearly_em", "利润表"),
    TableSpec("cash_flow_sheet", "stock_cash_flow_sheet_by_yearly_em", "现金流量表"),
)

#: 必须成功获取的报表（缺失则整份报告无意义，直接抛错而不是输出空壳）
MANDATORY_TABLES: frozenset[str] = frozenset({"balance_sheet", "profit_sheet"})

#: 表 key → 中文名（错误提示用）
REPORT_TABLE_LABELS: dict[str, str] = {spec.key: spec.label for spec in REPORT_TABLES}

#: 元数据文本列：不做数值化
_META_TEXT_COLUMNS: frozenset[str] = frozenset(
    {
        "SECUCODE",
        "SECURITY_CODE",
        "SECURITY_NAME_ABBR",
        "ORG_CODE",
        "ORG_TYPE",
        "REPORT_TYPE",
        "REPORT_DATE_NAME",
        "SECURITY_TYPE_CODE",
        "CURRENCY",
        "OPINION_TYPE",
        "OSOPINION_TYPE",
        "LISTING_STATE",
    }
)

#: 日期列：单独解析
_DATE_COLUMNS: tuple[str, ...] = ("REPORT_DATE", "NOTICE_DATE", "UPDATE_DATE")


# ---------------------------------------------------------------------------
# 代码解析（Patch 4.1：北交所 920 新号段 / 新三板旧号段）
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class MarketInfo:
    """代码所属市场与板块（Patch 4.1）。

    ``em_yearly_supported`` 是**实测结论**而非猜测（见 AGENTS.md §2.7）：
    东方财富年度三表接口对 ``920xxx``（北交所新号段）可用，
    对 ``43x/83x/87x/88x``（北交所旧号段 / 新三板）一律返回空数据。
    """

    prefix: str
    board: str
    em_yearly_supported: bool
    hint: str = ""


#: 北交所新号段（2023 年起启用，920xxx）——实测三表接口可用
_BJ_NEW_PREFIXES: tuple[str, ...] = ("920",)
#: 北交所旧号段 / 新三板挂牌（43/83/87/88）——实测三表接口不可用，但巨潮多收录其年报 PDF
_BJ_OLD_PREFIXES: tuple[str, ...] = ("43", "83", "87", "88")
#: 沪市 B 股（900xxx）必须判为 SH，**不能**被 9 开头的北交所规则吞掉
_SH_B_PREFIXES: tuple[str, ...] = ("900",)
_SH_PREFIXES: tuple[str, ...] = ("6",)
_SZ_PREFIXES: tuple[str, ...] = ("0", "3", "2")

_BJ_OLD_HINT = (
    "该代码属于北交所旧号段 / 新三板挂牌企业，其年报三大表未被东方财富免费接口标准化收录。"
    "可选方案：① 改用沪深/北交所新号段可比公司做横向对标；"
    "② 在投研工作台上传该公司年报 PDF，使用「年报附注穿透取证」与「附注即时检索」。"
)


def classify_market(code: str) -> MarketInfo:
    """把 6 位代码解析为 :class:`MarketInfo`（市场前缀 + 板块 + 接口覆盖结论）。

    Examples
    --------
    >>> classify_market("920012").prefix
    'BJ'
    >>> classify_market("900901").prefix
    'SH'
    >>> classify_market("874886").em_yearly_supported
    False
    """
    digits = "".join(ch for ch in str(code) if ch.isdigit())
    if len(digits) != 6:
        raise ValueError(f"股票代码必须为 6 位数字，收到: {code!r}")

    # 顺序敏感：920 必须先于 900/9，否则北交所新号段会被判成沪市
    if digits.startswith(_BJ_NEW_PREFIXES):
        return MarketInfo("BJ", "北交所 920 新号段", True)
    if digits.startswith(_SH_B_PREFIXES):
        return MarketInfo("SH", "沪市 B 股", True)
    if digits.startswith(_BJ_OLD_PREFIXES):
        return MarketInfo("BJ", "北交所旧号段 / 新三板挂牌", False, _BJ_OLD_HINT)
    if digits.startswith("4"):
        return MarketInfo("BJ", "新三板 / 老三板", False, _BJ_OLD_HINT)
    if digits.startswith(_SH_PREFIXES):
        return MarketInfo("SH", "沪市主板 / 科创板", True)
    if digits.startswith(("3",)):
        return MarketInfo("SZ", "创业板", True)
    if digits.startswith(("0",)):
        return MarketInfo("SZ", "深市主板（含原中小板）", True)
    if digits.startswith(("2",)):
        return MarketInfo("SZ", "深市 B 股", True)
    raise ValueError(f"无法识别的 A 股代码前缀: {code!r}（支持 6/9/0/3/2/4/8 开头）")


def split_symbol(code: str) -> tuple[str, str]:
    """把 6 位 A 股代码转换为 (市场前缀, 数据源代码)。

    Examples
    --------
    >>> split_symbol("600519")
    ('SH', 'SH600519')
    >>> split_symbol("920012")   # 北交所新号段
    ('BJ', 'BJ920012')
    >>> split_symbol("900901")   # 沪市 B 股
    ('SH', 'SH900901')
    """
    digits = "".join(ch for ch in str(code) if ch.isdigit())
    market = classify_market(digits)
    return market.prefix, f"{market.prefix}{digits}"


# ---------------------------------------------------------------------------
# 取数失败的语义翻译（Patch 4.1）
# ---------------------------------------------------------------------------
#: 网络层异常类名（遇到时给出网络/代理提示，而不是"代码不存在"）
_NETWORK_ERROR_NAMES: frozenset[str] = frozenset(
    {
        "ProxyError",
        "ConnectionError",
        "ConnectionResetError",
        "ConnectTimeout",
        "ReadTimeout",
        "Timeout",
        "URLError",
        "SSLError",
        "JSONDecodeError",
    }
)


def _looks_like_missing_code(exc: BaseException) -> bool:
    """判定异常是否等价于"该代码在上游查不到"。

    akshare 的年度报表接口在 ``result`` 为 ``None`` 时直接做下标访问，
    因此"查无此码"表现为 ``TypeError: 'NoneType' object is not subscriptable``；
    部分接口则表现为 ``KeyError`` / ``IndexError``。三者都按"未收录"处理。
    """
    if isinstance(exc, TypeError) and "not subscriptable" in str(exc):
        return True
    if isinstance(exc, TypeError) and "'NoneType'" in str(exc):
        return True
    return isinstance(exc, (KeyError, IndexError))


def describe_unavailable(
    code: str,
    spec: TableSpec,
    market: Optional[MarketInfo] = None,
    *,
    exc: Optional[BaseException] = None,
    empty: bool = False,
) -> DataUnavailableError:
    """把底层异常翻译成**面向业务的中文提示**（绝不泄露 ``NoneType``）。

    Parameters
    ----------
    code:
        6 位股票代码（缺失时用 em_symbol 兜底）。
    spec:
        当前失败的报表规格，用于在提示里点名是哪张表。
    market:
        :func:`classify_market` 的结果；``None`` 时按"未知板块"措辞。
    exc:
        原始异常；为 ``None`` 且 ``empty=True`` 表示接口正常返回但数据为空。
    """
    digits = "".join(ch for ch in str(code) if ch.isdigit()) or str(code)
    board = market.board if market is not None else "未知板块"

    # ① 网络 / 代理问题：与代码无关，必须区分开，否则会误导用户去改代码
    if exc is not None and type(exc).__name__ in _NETWORK_ERROR_NAMES:
        return DataUnavailableError(
            f"获取 {digits} 的{spec.label}时网络请求失败（{type(exc).__name__}）。"
            f"请检查网络/代理设置后重试；若该标的此前已成功取数，"
            f"可临时断网重跑以命中本地缓存（{spec.interface}）。"
        )

    # ② 北交所旧号段 / 新三板：免费接口确实无标准化三表 —— 给出可执行的两条出路
    if market is not None and not market.em_yearly_supported:
        return DataUnavailableError(
            f"{digits}（{board}）不在沪深京 A 股标准化三表覆盖范围内："
            f"东方财富年度报表接口 {spec.interface} 未返回该代码的{spec.label}。"
            f"建议：① 改用沪深主板或北交所 920 号段的可比公司做横向对标；"
            f"② 在投研工作台上传该公司的年报 PDF，使用「年报附注穿透取证」与「附注即时检索」功能"
            f"（巨潮对多数北交所旧号段标的仍收录年报原文）。"
        )

    # ③ 其他（920 新号段偶发、代码写错、上游临时故障）
    reason = "接口返回空数据" if empty else "接口调用失败"
    return DataUnavailableError(
        f"{digits}（{board}）的{spec.label}暂不可用：{spec.interface} {reason}。"
        f"请确认代码是否正确（沪深 6/0/3 开头、北交所 920 开头、B 股 900/200 开头），"
        f"或稍后重试。"
    )


def _is_business_error(exc: BaseException) -> bool:
    return isinstance(exc, DataUnavailableError)


# ---------------------------------------------------------------------------
# 数据包
# ---------------------------------------------------------------------------
@dataclass
class StatementBundle:
    """三表原始数据 + 溯源信息。"""

    code: str
    em_symbol: str
    market_prefix: str
    stock_name: str
    org_type: str
    frames: dict[str, pd.DataFrame]
    provenance: dict[str, dict[str, Any]] = field(default_factory=dict)
    audit_opinions: dict[str, Optional[str]] = field(default_factory=dict)
    as_of: dt.datetime = field(default_factory=dt.datetime.now)
    #: Patch 4.2：数据来源标识（在线接口 vs 本地年报 PDF 还原）
    provider: str = "akshare"
    upstream: str = "东方财富 Choice 数据接口"
    source_note: Optional[str] = None

    def frame(self, key: str) -> pd.DataFrame:
        return self.frames.get(key, pd.DataFrame())

    @property
    def report_years(self) -> list[int]:
        """可用于编制报告的年度（资产负债表与利润表的交集，倒序）。"""
        year_sets: list[set[int]] = []
        for key in ("balance_sheet", "profit_sheet"):
            df = self.frame(key)
            if "_report_year" in df.columns:
                year_sets.append(set(int(y) for y in df["_report_year"].dropna()))
        if not year_sets:
            return []
        common = set.intersection(*year_sets)
        return sorted(common, reverse=True)


# ---------------------------------------------------------------------------
# 数据提供者
# ---------------------------------------------------------------------------
class FinancialDataProvider:
    """akshare 年度报表数据提供者（带 CSV/JSON 本地缓存）。

    Parameters
    ----------
    cache_dir:
        缓存目录，默认 ``data/cache/statements``。
    ttl_hours:
        缓存有效期（小时），超期后重新拉取。
    """

    def __init__(
        self,
        cache_dir: Optional[Path] = None,
        ttl_hours: float = DEFAULT_CACHE_TTL_HOURS,
    ) -> None:
        self.cache_dir = Path(cache_dir) if cache_dir is not None else STATEMENT_CACHE_DIR
        self.ttl_hours = float(ttl_hours)

    # -- 对外入口 -----------------------------------------------------------
    def load(
        self,
        code: str,
        *,
        force_refresh: bool = False,
        as_of: Optional[dt.datetime] = None,
        collector: Optional[WarningCollector] = None,
    ) -> StatementBundle:
        """加载三表数据（优先缓存）。

        Parameters
        ----------
        code:
            6 位 A 股代码，如 ``"600519"``。
        force_refresh:
            为 True 时忽略缓存强制请求接口。
        as_of:
            数据可见性截止时间，用于剔除报告期已到但年报尚未披露的占位行。
        collector:
            警告收集器，数据层产生的警告会合并进最终报告。
        """
        ensure_directories()
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        as_of = as_of or dt.datetime.now()
        collector = collector or WarningCollector(str(code))

        digits = "".join(ch for ch in str(code) if ch.isdigit())
        market = classify_market(digits)
        prefix, em_symbol = split_symbol(digits)

        frames: dict[str, pd.DataFrame] = {}
        provenance: dict[str, dict[str, Any]] = {}
        provider = "akshare"
        upstream = "东方财富 Choice 数据接口"
        source_note: Optional[str] = None
        failure: Optional[DataUnavailableError] = None

        for spec in REPORT_TABLES:
            try:
                df, meta = self._load_table(
                    spec, digits, em_symbol, force_refresh, as_of, collector, market
                )
            except DataUnavailableError as exc:
                # 已经是业务级中文提示（Patch 4.1），不得再包一层类名
                if spec.key in MANDATORY_TABLES:
                    failure = exc
                    break
                collector.add(
                    "table_unavailable",
                    f"{spec.label}不可用，依赖该报表的指标将全部返回 null。",
                    field=spec.key,
                )
                df, meta = pd.DataFrame(), {
                    "interface": spec.interface,
                    "fetched_at": dt.datetime.now().isoformat(timespec="seconds"),
                    "cache_hit": False,
                    "cache_path": None,
                    "error": "数据源未收录",
                }
            except Exception as exc:  # noqa: BLE001 - 网络/接口异常需降级而非崩溃
                message = describe_unavailable(digits, spec, market, exc=exc)
                if spec.key in MANDATORY_TABLES:
                    failure = message
                    break
                collector.add(
                    "table_unavailable",
                    f"{message}依赖该报表的指标将全部返回 null。",
                    field=spec.key,
                )
                df, meta = pd.DataFrame(), {
                    "interface": spec.interface,
                    "fetched_at": dt.datetime.now().isoformat(timespec="seconds"),
                    "cache_hit": False,
                    "cache_path": None,
                    "error": "数据源未收录",
                }
            frames[spec.key] = df
            provenance[spec.key] = meta

        # ---- Patch 4.2：在线取数失败 → 尝试用本地年报 PDF 还原三大表 ----
        if failure is not None:
            fallback = self._load_from_local_pdf(digits, market, as_of, collector)
            if fallback is None:
                raise DataUnavailableError(
                    f"{failure}\n（补充：本地 data/pdfs/ 中也没有该代码的年报 PDF，"
                    f"可在投研工作台上传 PDF，系统会自动从年报原文还原三大表。）"
                )
            frames = fallback["frames"]
            provenance = fallback["provenance"]
            provider = "local_pdf_tables"
            upstream = f"本地年报 PDF · {fallback['pdf_name']}"
            source_note = fallback["note"]

        balance = frames.get("balance_sheet", pd.DataFrame())
        profit = frames.get("profit_sheet", pd.DataFrame())

        stock_name = self._first_text(balance, "SECURITY_NAME_ABBR") or self._first_text(
            profit, "SECURITY_NAME_ABBR"
        )
        org_type = self._first_text(balance, "ORG_TYPE") or self._first_text(profit, "ORG_TYPE")

        return StatementBundle(
            code=digits,
            em_symbol=em_symbol,
            market_prefix=prefix,
            stock_name=stock_name or digits,
            org_type=org_type or "未知",
            frames=frames,
            provenance=provenance,
            audit_opinions=self._extract_audit_opinions(balance),
            as_of=as_of,
            provider=provider,
            upstream=upstream,
            source_note=source_note,
        )

    # -- Patch 4.2：本地 PDF 兜底 ------------------------------------------
    def _load_from_local_pdf(
        self,
        code: str,
        market: MarketInfo,
        as_of: dt.datetime,
        collector: WarningCollector,
    ) -> Optional[dict[str, Any]]:
        """用本地年报 PDF 还原三大表（找不到 / 解析失败则返回 None）。"""
        from src.tools.pdf_financial_extractor import (
            build_em_frames,
            extract_statements,
            find_local_pdf,
        )

        pdf_path = find_local_pdf(code)
        if pdf_path is None:
            return None
        try:
            extraction = extract_statements(pdf_path, code=code)
        except Exception as exc:  # noqa: BLE001 - PDF 解析失败仍应给出可读提示
            collector.add(
                "pdf_tables_failed",
                f"本地年报 PDF《{pdf_path.name}》解析失败（{type(exc).__name__}: {exc}），"
                f"已放弃该兜底路径。",
                field="local_pdf",
            )
            return None

        if extraction.code and extraction.code != code:
            collector.add(
                "pdf_code_mismatch",
                f"本地 PDF《{pdf_path.name}》识别出的证券代码为 {extraction.code}，"
                f"与请求的 {code} 不一致，已忽略该文件以免张冠李戴。",
                field="local_pdf",
            )
            return None

        raw_frames = build_em_frames(extraction)
        frames: dict[str, pd.DataFrame] = {}
        provenance: dict[str, dict[str, Any]] = {}
        fetched_at = dt.datetime.now().isoformat(timespec="seconds")
        for spec in REPORT_TABLES:
            raw = raw_frames.get(spec.key)
            if raw is None or raw.empty:
                continue
            try:
                normalized = self._normalize(raw, spec, as_of, collector)
            except Exception as exc:  # noqa: BLE001
                collector.add(
                    "pdf_table_normalize_failed",
                    f"从 PDF 还原的{spec.label}归一化失败（{type(exc).__name__}: {exc}）。",
                    field=spec.key,
                )
                continue
            if normalized.empty:
                continue
            frames[spec.key] = normalized
            provenance[spec.key] = {
                "code": code,
                "table": spec.key,
                "label": spec.label,
                "interface": f"local_pdf:{pdf_path.name}",
                "provider": "local_pdf_tables",
                "fetched_at": fetched_at,
                "rows": int(len(normalized)),
                "columns": int(len(normalized.columns)),
                "cache_hit": False,
                "cache_path": None,
                "as_of": as_of.isoformat(timespec="seconds"),
                "pdf_path": str(pdf_path),
                "pdf_period": f"{extraction.report_year}年报",
                "pdf_pages": extraction.statement_pages,
                "extract_warnings": extraction.warnings,
            }

        missing = [key for key in MANDATORY_TABLES if key not in frames]
        if missing:
            collector.add(
                "pdf_tables_incomplete",
                f"本地年报 PDF《{pdf_path.name}》未能还原出"
                f"{'、'.join(REPORT_TABLE_LABELS.get(key, key) for key in missing)}，"
                f"已放弃该兜底路径。",
                field="local_pdf",
            )
            return None

        years = "、".join(period.period_name for period in extraction.periods)
        collector.add(
            "local_pdf_tables_used",
            f"在线接口无 {code} 的标准化三大表，已从本地年报 PDF《{pdf_path.name}》"
            f"（{extraction.stock_name} · {extraction.report_year}年报）确定性还原"
            f"资产负债表 / 利润表 / 现金流量表，覆盖 {years}；"
            f"金额单位 {extraction.unit} 已统一换算为元。",
            level="info",
            field="local_pdf",
        )
        for item in extraction.warnings:
            collector.add("pdf_extract_warning", item, level="info", field="local_pdf")

        return {
            "frames": frames,
            "provenance": provenance,
            "pdf_name": pdf_path.name,
            "pdf_path": pdf_path,
            "note": (
                f"三大表由本地年报 PDF《{pdf_path.name}》确定性还原"
                f"（{extraction.stock_name} {extraction.report_year}年报，"
                f"共 {len(extraction.periods)} 期），未使用任何在线接口。"
            ),
        }

    # -- 单表加载 -----------------------------------------------------------
    def _load_table(
        self,
        spec: TableSpec,
        code: str,
        em_symbol: str,
        force_refresh: bool,
        as_of: dt.datetime,
        collector: WarningCollector,
        market: Optional[MarketInfo] = None,
    ) -> tuple[pd.DataFrame, dict[str, Any]]:
        csv_path, meta_path = self._cache_paths(code, spec.key)

        if not force_refresh and self._cache_is_fresh(csv_path, meta_path):
            try:
                raw = pd.read_csv(csv_path, encoding="utf-8-sig", low_memory=False)
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                meta["cache_hit"] = True
                normalized = self._normalize(raw, spec, as_of, collector)
                return normalized, meta
            except Exception as exc:  # noqa: BLE001 - 缓存损坏则回退到线上请求
                collector.add(
                    "cache_corrupted",
                    f"{spec.label}本地缓存读取失败（{type(exc).__name__}: {exc}），已回退为实时请求。",
                    field=spec.key,
                )

        raw = self._fetch_remote(spec, em_symbol, code, market)
        normalized = self._normalize(raw, spec, as_of, collector)

        fetched_at = dt.datetime.now().isoformat(timespec="seconds")
        meta: dict[str, Any] = {
            "code": code,
            "table": spec.key,
            "label": spec.label,
            "interface": spec.interface,
            "fetched_at": fetched_at,
            "rows": int(len(normalized)),
            "columns": int(len(normalized.columns)),
            "cache_path": str(csv_path),
            "cache_hit": False,
            "as_of": as_of.isoformat(timespec="seconds"),
        }
        try:
            normalized.to_csv(csv_path, index=False, encoding="utf-8-sig")
            meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError as exc:
            collector.add(
                "cache_write_failed",
                f"{spec.label}本地缓存写入失败（{type(exc).__name__}: {exc}），本次结果不受影响。",
                field=spec.key,
            )
        return normalized, meta

    def _fetch_remote(
        self,
        spec: TableSpec,
        em_symbol: str,
        code: str = "",
        market: Optional[MarketInfo] = None,
    ) -> pd.DataFrame:
        """调用 akshare 接口（延迟导入，避免仅需模型定义时也强制依赖）。

        Patch 4.1：akshare 对"查无此码"的响应会直接下标 ``None``，抛出
        ``TypeError: 'NoneType' object is not subscriptable`` —— 这是**上游实现缺陷**，
        绝不能原样暴露给前端。这里统一翻译成中文业务提示（见 :func:`describe_unavailable`）。
        """
        import akshare as ak  # 延迟导入：单元测试只校验模型时无需联网依赖

        func = getattr(ak, spec.interface, None)
        if func is None:
            raise DataUnavailableError(f"当前 akshare 版本不存在接口 {spec.interface}")
        try:
            df = func(symbol=em_symbol)
        except Exception as exc:  # noqa: BLE001 - 逐个翻译成业务语义
            raise describe_unavailable(code or em_symbol, spec, market, exc=exc) from exc
        if df is None or not isinstance(df, pd.DataFrame) or df.empty:
            raise describe_unavailable(code or em_symbol, spec, market, empty=True)
        return df

    # -- 归一化 -------------------------------------------------------------
    def _normalize(
        self,
        df: pd.DataFrame,
        spec: TableSpec,
        as_of: dt.datetime,
        collector: WarningCollector,
    ) -> pd.DataFrame:
        """统一类型、只保留年报、剔除未披露报告期、按年度去重排序。"""
        out = df.copy()

        if "REPORT_DATE" not in out.columns:
            raise DataUnavailableError(f"{spec.label}返回结果缺少 REPORT_DATE 列，接口结构可能已变更")

        for column in _DATE_COLUMNS:
            if column in out.columns:
                out[column] = pd.to_datetime(out[column], errors="coerce")

        for column in out.columns:
            if column in _META_TEXT_COLUMNS or column in _DATE_COLUMNS:
                continue
            out[column] = pd.to_numeric(out[column], errors="coerce")

        out = out[out["REPORT_DATE"].notna()].copy()
        out["_report_year"] = out["REPORT_DATE"].dt.year.astype("int64")

        # 仅保留年报（年度接口本应全部为年报，此处为防御性校验）
        if "REPORT_TYPE" in out.columns:
            annual = out[out["REPORT_TYPE"].astype(str).str.strip() == "年报"]
            if annual.empty:
                collector.add(
                    "no_annual_rows",
                    f"{spec.label}未筛出 REPORT_TYPE='年报' 的记录，已退回全量数据，请复核接口口径。",
                    field=spec.key,
                )
            else:
                out = annual

        # 剔除报告期已到但年报尚未披露的占位行（NOTICE_DATE 晚于截止日）
        if "NOTICE_DATE" in out.columns:
            before = len(out)
            undisclosed = out["NOTICE_DATE"].notna() & (out["NOTICE_DATE"] > as_of)
            out = out[~undisclosed]
            if len(out) < before:
                collector.add(
                    "undisclosed_period_excluded",
                    f"{spec.label}有 {before - len(out)} 条记录的年报公告日晚于截止日 "
                    f"{as_of.date().isoformat()}，判定为尚未披露，已剔除。",
                    level="info",
                    field=spec.key,
                )

        # 同一会计年度可能因追溯调整出现多行：保留 UPDATE_DATE 最新的一条
        sort_columns = ["_report_year"]
        if "UPDATE_DATE" in out.columns:
            sort_columns.append("UPDATE_DATE")
        out = (
            out.sort_values(sort_columns, na_position="first")
            .drop_duplicates(subset=["_report_year"], keep="last")
            .sort_values("_report_year", ascending=False)
            .reset_index(drop=True)
        )

        if "REPORT_DATE_NAME" not in out.columns:
            out["REPORT_DATE_NAME"] = out["_report_year"].map(lambda y: f"{int(y)}年报")

        return out

    # -- 工具方法 -----------------------------------------------------------
    def _cache_paths(self, code: str, table_key: str) -> tuple[Path, Path]:
        stem = self.cache_dir / f"{code}_{table_key}"
        return stem.with_suffix(".csv"), stem.with_name(stem.name + ".meta.json")

    def _cache_is_fresh(self, csv_path: Path, meta_path: Path) -> bool:
        if not csv_path.exists() or not meta_path.exists():
            return False
        if self.ttl_hours <= 0:
            return False
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            fetched_at = dt.datetime.fromisoformat(meta["fetched_at"])
        except (OSError, ValueError, KeyError, TypeError):
            return csv_path.stat().st_mtime > (dt.datetime.now() - dt.timedelta(hours=self.ttl_hours)).timestamp()
        return dt.datetime.now() - fetched_at < dt.timedelta(hours=self.ttl_hours)

    @staticmethod
    def _first_text(df: pd.DataFrame, column: str) -> str:
        if column not in df.columns or df.empty:
            return ""
        values = df[column].dropna()
        return str(values.iloc[0]).strip() if not values.empty else ""

    @staticmethod
    def _extract_audit_opinions(df: pd.DataFrame) -> dict[str, Optional[str]]:
        """提取各年度审计意见类型（用于"非标意见"排雷规则）。"""
        if df.empty or "OPINION_TYPE" not in df.columns or "_report_year" not in df.columns:
            return {}
        opinions: dict[str, Optional[str]] = {}
        for _, row in df.iterrows():
            year = int(row["_report_year"])
            value = row["OPINION_TYPE"]
            opinions[f"{year}年报"] = None if pd.isna(value) else str(value).strip()
        return opinions
