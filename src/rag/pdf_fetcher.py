"""年报 PDF 双通道获取：本地目录优先 + 巨潮资讯（cninfo）自动下载。

为什么需要两个通道（铁律 3）
--------------------------
* **本地优先**：投研场景下多数人已经有年报 PDF，直接放 `data/pdfs/` 即可，零网络依赖；
* **巨潮兜底**：没有本地文件时，走 cninfo 官方公开公告查询接口自动下载，
  避免手工找链接。接口有两步：``topSearch/query`` 拿 ``orgId``，再 ``hisAnnouncement/query`` 列公告。

防御性约定
----------
1. 任何网络异常都**不抛出**，返回 ``None`` 并在 ``last_reason`` 中说明，
   工作流自动退化为 Phase 2 的"无 PDF 模式"（绝不阻塞报告生成）。
2. 目标年度未披露时，自动回退到**最近一期可用年报**，并在 ``period_mismatch_note``
   中显式标注期次差异（铁律 2：绝不张冠李戴）。
3. 过滤掉"摘要/英文版/更正前/已取消"等非完整正文，避免把 20 页摘要当成年报解析。
"""

from __future__ import annotations

import datetime as dt
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

#: 巨潮接口
CNINFO_TOP_SEARCH = "http://www.cninfo.com.cn/new/information/topSearch/query"
CNINFO_ANNOUNCEMENT = "http://www.cninfo.com.cn/new/hisAnnouncement/query"
CNINFO_STATIC = "http://static.cninfo.com.cn/"

#: 年报公告分类（巨潮固定分类码）
CATEGORY_ANNUAL_REPORT = "category_ndbg_szsh"

#: 请求头（巨潮对 UA / Referer 有校验）
_DEFAULT_HEADERS: dict[str, str] = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/122.0 Safari/537.36"
    ),
    "Referer": "http://www.cninfo.com.cn/new/commonUrl?url=disclosure/list/notice",
    "X-Requested-With": "XMLHttpRequest",
    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    "Accept": "application/json, text/javascript, */*; q=0.01",
}

#: 标题黑名单：这些不是"完整年度报告正文"
_TITLE_BLOCKLIST: tuple[str, ...] = (
    "摘要",
    "英文",
    "english",
    "English",
    "更正前",
    "已取消",
    "取消",
    "补充",
    "问询",
    "回复",
    "说明",
    "社会责任",
    "环境",
    "ESG",
    "内部控制",
    "审计报告",
)

#: 页面最小合理体积（低于此值多半是错误页/空文件）
_MIN_PDF_BYTES = 200_000


class PdfFetchError(RuntimeError):
    """PDF 获取过程中的可预期异常（通常由调用方降级处理）。"""


@dataclass
class PdfArtifact:
    """一份可用的年报 PDF。"""

    path: Path
    stock_code: str
    stock_name: str
    pdf_period: str
    period_year: int
    source: str = "local"  # local | cninfo
    announcement_title: str = ""
    announcement_url: str = ""
    size_bytes: int = 0
    downloaded_at: str = ""
    period_mismatch_note: Optional[str] = None
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def stem(self) -> str:
        return self.path.stem

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "stock_code": self.stock_code,
            "stock_name": self.stock_name,
            "pdf_period": self.pdf_period,
            "period_year": self.period_year,
            "source": self.source,
            "announcement_title": self.announcement_title,
            "announcement_url": self.announcement_url,
            "size_bytes": self.size_bytes,
            "downloaded_at": self.downloaded_at,
            "period_mismatch_note": self.period_mismatch_note,
        }


def _market_column(code: str) -> str:
    """巨潮接口的 ``column`` 参数（沪市 sse / 深市 szse / 北交所 bj / 新三板 third）。

    Patch 4.1：``920xxx`` 属北交所新号段，必须判 ``bj``（旧实现 ``9`` 开头一律 ``sse``，
    导致北交所标的在巨潮检索结果为 0 条）；``900xxx`` 才是沪市 B 股。
    ``43/83/87/88`` 为北交所旧号段或新三板挂牌，巨潮的 ``bj`` 频道仍收录其年报原文。
    """
    digits = "".join(ch for ch in str(code) if ch.isdigit())
    if digits.startswith("920"):
        return "bj"
    if digits.startswith("900"):
        return "sse"
    if digits.startswith("6"):
        return "sse"
    if digits.startswith(("43", "83", "87", "88", "4", "8")):
        return "bj"
    return "szse"


def _post_form(url: str, data: dict[str, str], *, timeout: float = 30.0) -> str:
    payload = urllib.parse.urlencode(data).encode("utf-8")
    request = urllib.request.Request(url, data=payload, headers=_DEFAULT_HEADERS)
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - 固定官方域名
        return response.read().decode("utf-8", errors="replace")


class AnnualReportFetcher:
    """年报 PDF 获取器（本地优先 + 巨潮下载）。"""

    def __init__(
        self,
        pdf_dir: Optional[Path] = None,
        *,
        timeout_s: float = 60.0,
        verbose: bool = True,
    ) -> None:
        from src.config import PDF_DIR  # 延迟导入避免循环

        self.pdf_dir = Path(pdf_dir) if pdf_dir is not None else PDF_DIR
        self.pdf_dir.mkdir(parents=True, exist_ok=True)
        self.timeout_s = timeout_s
        self.verbose = verbose
        self.last_reason: Optional[str] = None

    # -- 日志 ---------------------------------------------------------------
    def _log(self, message: str) -> None:
        if self.verbose:
            print(f"[pdf_fetcher] {message}", flush=True)

    # -- 对外入口 -----------------------------------------------------------
    def fetch(
        self,
        code: str,
        year: Optional[int] = None,
        *,
        allow_download: bool = True,
        local_path: Optional[Path | str] = None,
    ) -> Optional[PdfArtifact]:
        """获取年报 PDF。

        Parameters
        ----------
        code:
            6 位 A 股代码。
        year:
            目标年度（如 2025）。``None`` 表示取最新可得年度。
        allow_download:
            本地未命中时是否允许联网下载。
        local_path:
            显式指定的本地 PDF（跳过目录搜索，但仍会推断期次）。
        """
        digits = "".join(ch for ch in str(code) if ch.isdigit())
        if len(digits) != 6:
            self.last_reason = f"非法股票代码：{code!r}"
            return None

        self.last_reason = None

        # ---- 通道 1：显式路径 ----
        if local_path:
            artifact = self._artifact_from_path(Path(local_path), digits, hint_year=year)
            if artifact is not None:
                return artifact
            self._log(f"显式指定的 PDF 不可用，转为目录搜索：{local_path}")

        # ---- 通道 2：本地目录 ----
        artifact = self._find_local(digits, year)
        if artifact is not None:
            self._log(f"本地命中：{artifact.path.name}（{artifact.pdf_period}）")
            return artifact

        # ---- 通道 3：巨潮下载 ----
        if not allow_download:
            self.last_reason = "本地未找到年报 PDF，且已禁用联网下载"
            self._log(self.last_reason)
            return None

        try:
            artifact = self._download(digits, year)
        except Exception as exc:  # noqa: BLE001 - 网络问题一律降级
            self.last_reason = f"巨潮下载失败：{type(exc).__name__}: {exc}"
            self._log(self.last_reason)
            return None

        if artifact is None and self.last_reason is None:
            self.last_reason = f"巨潮未检索到 {digits} 的年度报告"
        return artifact

    # -- 本地 ---------------------------------------------------------------
    def _find_local(self, code: str, year: Optional[int]) -> Optional[PdfArtifact]:
        candidates = sorted(self.pdf_dir.glob(f"{code}*.pdf"))
        if not candidates and code[:6] != code:
            candidates = sorted(self.pdf_dir.glob("*.pdf"))

        parsed: list[tuple[int, Path]] = []
        for path in candidates:
            inferred = self._infer_year(path.name)
            if inferred is None:
                continue
            parsed.append((inferred, path))

        if not parsed:
            return None

        if year is not None:
            exact = [item for item in parsed if item[0] == year]
            if exact:
                return self._artifact_from_path(exact[0][1], code, hint_year=year)
            # 回退：最近一期（并标注期次差异）
            latest = max(parsed, key=lambda item: item[0])
            artifact = self._artifact_from_path(latest[1], code, hint_year=latest[0])
            if artifact is not None:
                artifact.period_mismatch_note = (
                    f"本地无 {year} 年报 PDF，已回退使用 {artifact.pdf_period}；"
                    f"引用附注数据时必须标明该期次"
                )
            return artifact

        latest = max(parsed, key=lambda item: item[0])
        return self._artifact_from_path(latest[1], code, hint_year=latest[0])

    @staticmethod
    def _infer_year(filename: str) -> Optional[int]:
        matches = re.findall(r"(20\d{2})", filename)
        if not matches:
            return None
        years = [int(value) for value in matches]
        return max(year for year in years if 2000 <= year <= dt.datetime.now().year + 1)

    def _artifact_from_path(
        self, path: Path, code: str, *, hint_year: Optional[int]
    ) -> Optional[PdfArtifact]:
        if not path.exists() or not path.is_file():
            return None
        size = path.stat().st_size
        if size < _MIN_PDF_BYTES:
            self._log(f"跳过疑似损坏/非完整 PDF：{path.name}（{size} B）")
            return None
        year = hint_year or self._infer_year(path.name)
        if year is None:
            year = dt.datetime.now().year - 1
        return PdfArtifact(
            path=path,
            stock_code=code,
            stock_name="",
            pdf_period=f"{year}年报",
            period_year=year,
            source="local",
            size_bytes=size,
        )

    # -- 巨潮 ---------------------------------------------------------------
    def _resolve_org(self, code: str) -> tuple[str, str]:
        """返回 (orgId, 股票简称)。"""
        text = _post_form(
            CNINFO_TOP_SEARCH, {"keyWord": code, "maxNum": "10"}, timeout=self.timeout_s
        )
        items = json.loads(text or "[]")
        for item in items:
            if str(item.get("code")) == code:
                return str(item.get("orgId", "")), str(item.get("zwjc", "") or code)
        if items:
            return str(items[0].get("orgId", "")), str(items[0].get("zwjc", "") or code)
        raise PdfFetchError(f"巨潮未收录该代码：{code}")

    @staticmethod
    def _extract_year(title: str) -> Optional[int]:
        matches = re.findall(r"(20\d{2})\s*年", title)
        if matches:
            return int(matches[-1])
        matches = re.findall(r"(20\d{2})", title)
        return int(matches[-1]) if matches else None

    @staticmethod
    def _is_full_report(title: str) -> bool:
        if "年度报告" not in title and "年报" not in title:
            return False
        return not any(token in title for token in _TITLE_BLOCKLIST)

    def list_annual_reports(self, code: str) -> list[dict[str, Any]]:
        """列出巨潮上可用的完整年度报告（按年度倒序）。"""
        org_id, name = self._resolve_org(code)
        now = dt.datetime.now()
        data = {
            "pageNum": "1",
            "pageSize": "60",
            "column": _market_column(code),
            "tabName": "fulltext",
            "plate": "",
            "stock": f"{code},{org_id}",
            "searchkey": "",
            "secid": "",
            "category": CATEGORY_ANNUAL_REPORT,
            "trade": "",
            "seDate": f"{now.year - 8}-01-01~{now.year + 1}-12-31",
            "sortName": "",
            "sortType": "",
            "isHLtitle": "true",
        }
        text = _post_form(CNINFO_ANNOUNCEMENT, data, timeout=self.timeout_s)
        payload = json.loads(text or "{}")
        reports: list[dict[str, Any]] = []
        for item in payload.get("announcements") or []:
            title = str(item.get("announcementTitle", "")).strip()
            if not self._is_full_report(title):
                continue
            year = self._extract_year(title)
            if year is None:
                continue
            reports.append(
                {
                    "year": year,
                    "title": title,
                    "url": CNINFO_STATIC + str(item.get("adjunctUrl", "")).lstrip("/"),
                    "name": name,
                    "published": dt.datetime.fromtimestamp(
                        int(item.get("announcementTime", 0)) / 1000
                    ).date().isoformat()
                    if item.get("announcementTime")
                    else "",
                }
            )
        reports.sort(key=lambda row: row["year"], reverse=True)
        return reports

    def _download(self, code: str, year: Optional[int]) -> Optional[PdfArtifact]:
        reports = self.list_annual_reports(code)
        if not reports:
            self.last_reason = f"巨潮未检索到 {code} 的完整年度报告"
            return None

        target = None
        mismatch_note = None
        if year is None:
            target = reports[0]
        else:
            target = next((row for row in reports if row["year"] == year), None)
            if target is None:
                target = reports[0]
                mismatch_note = (
                    f"巨潮未披露 {year} 年完整年报，已回退使用 {target['year']} 年报；"
                    f"引用附注数据时必须标明该期次"
                )
                self._log(mismatch_note)

        filename = f"{code}_{target['year']}年报.pdf"
        dest = self.pdf_dir / filename
        if dest.exists() and dest.stat().st_size >= _MIN_PDF_BYTES:
            self._log(f"下载目录已存在：{filename}（复用）")
        else:
            self._log(f"从巨潮下载：{target['title']} → {filename}")
            self._download_file(target["url"], dest)

        return PdfArtifact(
            path=dest,
            stock_code=code,
            stock_name=str(target.get("name", "")),
            pdf_period=f"{target['year']}年报",
            period_year=int(target["year"]),
            source="cninfo",
            announcement_title=str(target.get("title", "")),
            announcement_url=str(target.get("url", "")),
            size_bytes=dest.stat().st_size,
            downloaded_at=dt.datetime.now().isoformat(timespec="seconds"),
            period_mismatch_note=mismatch_note,
            extra={"published": target.get("published", "")},
        )

    def _download_file(self, url: str, dest: Path) -> None:
        request = urllib.request.Request(url, headers=_DEFAULT_HEADERS)
        tmp = dest.with_suffix(".pdf.part")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_s) as response:  # noqa: S310
                with tmp.open("wb") as handle:
                    while True:
                        block = response.read(1 << 16)
                        if not block:
                            break
                        handle.write(block)
            size = tmp.stat().st_size
            if size < _MIN_PDF_BYTES:
                raise PdfFetchError(f"下载文件过小（{size} B），疑似错误页")
            tmp.replace(dest)
        finally:
            if tmp.exists():
                tmp.unlink(missing_ok=True)


def fetch_annual_report_pdf(
    code: str,
    year: Optional[int] = None,
    *,
    pdf_dir: Optional[Path] = None,
    allow_download: bool = True,
    local_path: Optional[Path | str] = None,
    verbose: bool = True,
) -> Optional[PdfArtifact]:
    """便捷函数：一步获取年报 PDF（失败返回 ``None``，绝不抛错）。"""
    fetcher = AnnualReportFetcher(pdf_dir=pdf_dir, verbose=verbose)
    return fetcher.fetch(code, year, allow_download=allow_download, local_path=local_path)


def iter_annual_report_links(code: str) -> Iterable[dict[str, Any]]:
    """列出某公司全部可用年报链接（供调试与测试使用）。"""
    return AnnualReportFetcher(verbose=False).list_annual_reports(code)
