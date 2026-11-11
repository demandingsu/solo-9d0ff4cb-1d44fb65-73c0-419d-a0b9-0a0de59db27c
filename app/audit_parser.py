"""结果公示核查的 HTML 解析。

三类输入：
1. 原招标/采购公告详情：读取分包表，得到 ``包号清单``（不漏包、不臆造包）；
2. 结果目录页：发现同站结果详情链接与分页链接，沿分页逐页抓取；
3. 中标/废标/结果更正详情：提取包号、公告编号、更正引用编号、发布时间、
   中标单位与废标原因。

解析沿用 :mod:`app.parser` 的三层回退思路（结构化标记 → 标签匹配 → 正文正则），
本模块只补充结果公示场景所需的结构。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse

from app.parser import _clean, _data_field, _labeled_value, _soup, parse_datetime, same_site

# --------------------------------------------------------------------------- #
# 包号
# --------------------------------------------------------------------------- #
# 第3包 / 第 G1 标段 / 包号：A-2 / 包 5
_PACKAGE_NO_INLINE = re.compile(
    r"第\s*([0-9]{1,3}|[A-Za-z]{1,3}[-_]?[0-9]{1,3})\s*(?:包组|分包|标段|包|标)"
    r"|(?:包组编号|分包编号|采购包号|标段编号|包编号|分包号|标段号|包组号|包号)"
    r"\s*[:：]?\s*([A-Za-z0-9\-_]{1,12})"
    r"|包\s*([0-9]{1,3})(?![0-9])"
)
# 表头明确为“包号”列时，单元格可直接是裸编号 3 / G1 / A-2
_BARE_PACKAGE_CODE = re.compile(r"[A-Za-z]{0,3}[-_]?[0-9]{1,4}")
_PACKAGE_NO_LABELS = (
    "包号", "分包号", "采购包号", "包组编号", "包编号",
    "标段编号", "标段号", "分包编号", "包组号",
)
_PACKAGE_NAME_LABELS = (
    "包名称", "分包名称", "采购包名称", "标段名称", "包组名称", "采购内容", "标的名称",
)


class AuditParseError(ValueError):
    """核查所需页面结构无法解析。"""


def normalize_package_no(raw: str | None) -> str | None:
    """归一化包号：去“第/包/标段”等修饰与空白，拉丁前缀统一大写。

    输入不含任何包号线索（“第N包/包号：…”等）时返回 None，避免把普通文本误判为包号。
    """
    if not raw:
        return None
    text = _clean(raw)
    match = _PACKAGE_NO_INLINE.search(text)
    if not match:
        return None
    token = next((g for g in match.groups() if g), "")
    token = token.strip(" ：:、.。-_\t")
    if not token:
        return None
    token = re.sub(r"(包组|分包|标段|包|标)$", "", token).strip()
    if re.fullmatch(r"[a-z]{1,3}[-_]?[0-9]{1,4}", token):
        token = token.upper()
    return token or None


def _coerce_bare_package(raw: str | None) -> str | None:
    """包号列/结构化字段中的裸编号（如 1、G3、A-2）直接归一化。"""
    if not raw:
        return None
    text = _clean(raw).strip(" ：:、.。-_")
    if not _BARE_PACKAGE_CODE.fullmatch(text):
        return normalize_package_no(raw)
    if re.fullmatch(r"[a-z]{1,3}[-_]?[0-9]{1,4}", text):
        text = text.upper()
    return text


def _header_index(headers: list[str], labels: tuple[str, ...]) -> int | None:
    for idx, header in enumerate(headers):
        head = header.strip(" ：:")
        if head in labels or any(label in head for label in labels):
            return idx
    return None


def extract_packages(html: str) -> list[tuple[str, str]]:
    """从原公告提取 ``(包号, 包名称)`` 清单，保持表格顺序并去重。

    优先读取含“包号”表头的表格；无结构化表格时回退到标签对与正文正则。
    只返回能解析出明确包号的条目，绝不凭空生成包号。
    """
    soup = _soup(html)
    found: list[tuple[str, str]] = []
    seen: set[str] = set()

    def _add(no: str | None, name: str = "", *, normalized: bool = False) -> None:
        # normalized=True 时 no 已经过 _coerce_bare_package/normalize 处理，直接采信
        pkg = no if normalized else normalize_package_no(no)
        if pkg and pkg not in seen:
            seen.add(pkg)
            found.append((pkg, _clean(name)))

    # 1) 含包号列的表格
    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if not rows:
            continue
        header_row = rows[0]
        headers = [_clean(c.get_text()) for c in header_row.find_all(["th", "td"])]
        no_idx = _header_index(headers, _PACKAGE_NO_LABELS)
        if no_idx is None:
            continue
        name_idx = _header_index(headers, _PACKAGE_NAME_LABELS)
        for tr in rows[1:]:
            cells = tr.find_all(["td", "th"])
            if len(cells) <= no_idx:
                continue
            no_text = _clean(cells[no_idx].get_text())
            pkg = _coerce_bare_package(no_text)
            if pkg is None:
                continue
            name = ""
            if name_idx is not None and len(cells) > name_idx:
                name = _clean(cells[name_idx].get_text())
            _add(pkg, name, normalized=True)

    # 2) 标签对回退（th/td、dt/dd、li、p）
    if not found:
        value = _data_field(soup, "package-no") or _labeled_value(soup, _PACKAGE_NO_LABELS)
        _add(value)

    # 3) 正文正则兜底：“第3包”“包号：G1”等（正则已抽出包号 token，直接采信）
    if not found:
        body = _clean(soup.get_text(" "))
        for match in _PACKAGE_NO_INLINE.finditer(body):
            token = next((g for g in match.groups() if g), None)
            if token and re.fullmatch(r"[a-z]{1,3}[-_]?[0-9]{1,4}", token):
                token = token.upper()
            _add(token, normalized=True)

    return found


# --------------------------------------------------------------------------- #
# 结果目录页：详情链接 + 分页链接
# --------------------------------------------------------------------------- #
_SKIP_PATH = re.compile(
    r"(login|logout|register|index|home|about|contact|search|#|javascript)", re.I
)
_PAGE_TEXT_KEYWORDS = (
    "下一页", "下页", "尾页", "末页", "上一页", "上页", "首页", "更多", "下一頁",
)
_PAGE_HINT = re.compile(
    r"(?:^|[/?&])(?:page(?:no|num|index|size)?|p|pn)=?\d+"
    r"|page[-_/]?\d+|list[-_]?\d+|index[-_]?\d+",
    re.I,
)


def _is_pagination(anchor, absolute: str) -> bool:
    rel = anchor.get("rel") or []
    if any(r in ("next", "prev", "first", "last") for r in rel):
        return True
    text = _clean(anchor.get_text())
    if text in _PAGE_TEXT_KEYWORDS or re.fullmatch(r"\d{1,4}", text):
        return True
    parsed = urlparse(absolute)
    return bool(_PAGE_HINT.search(parsed.path + "?" + parsed.query))


def extract_catalog_links(
    html: str, catalog_url: str
) -> tuple[list[tuple[str, str, str]], list[str]]:
    """解析目录页，返回 ``(详情链接[(url,文本,行上下文)], 分页链接[url])``。

    仅保留同站链接；分页链接不会再被当作详情链接；导航/登录类链接排除。
    """
    soup = _soup(html)
    seen_detail: set[str] = set()
    seen_page: set[str] = set()
    details: list[tuple[str, str, str]] = []
    pages: list[str] = []
    list_path = urlparse(catalog_url).path.rstrip("/")

    for anchor in soup.find_all("a", href=True):
        absolute = urljoin(catalog_url, anchor["href"].strip())
        if not absolute.startswith(("http://", "https://")):
            continue
        if not same_site(absolute, catalog_url):
            continue
        parsed = urlparse(absolute)
        if _SKIP_PATH.search(parsed.path):
            continue

        if _is_pagination(anchor, absolute):
            if absolute not in seen_page and parsed.path.rstrip("/") != list_path:
                seen_page.add(absolute)
                pages.append(absolute)
            continue

        if parsed.path.rstrip("/") == list_path and not parsed.query:
            continue  # 目录自身
        if absolute in seen_detail:
            continue
        seen_detail.add(absolute)
        row = anchor.find_parent(["tr", "li", "dd", "article"]) or anchor.parent
        context = _clean(row.get_text(" ")) if row is not None else ""
        details.append((absolute, _clean(anchor.get_text()), context))

    return details, pages


# --------------------------------------------------------------------------- #
# 结果详情页
# --------------------------------------------------------------------------- #
_CORRECTION_KEYWORDS = ("更正", "修正", "变更", "补充公示", "补充公告")
_REJECT_KEYWORDS = ("废标", "流标", "终止", "取消")
_AWARD_KEYWORDS = ("中标", "成交", "授标", "入围", "结果公示", "结果公告")

_NOTICE_NO_LABELS = (
    "公告编号", "公告文号", "公示编号", "中标公告编号",
    "成交公告编号", "结果公告编号", "文号",
)
_REF_NOTICE_LABELS = (
    "原公告编号", "原结果公告编号", "原中标公告编号", "原成交公告编号",
    "原公示编号", "被更正公告编号", "引用公告编号", "关联公告编号", "原公告文号",
)
_WINNER_LABELS = (
    "中标单位", "中标供应商", "中标（成交）单位", "中标(成交)单位",
    "成交单位", "成交供应商", "授标单位", "中标人", "成交人",
    "中标供应商名称", "成交供应商名称", "中标单位名称",
)
_REJECT_REASON_LABELS = ("废标原因", "流标原因", "终止原因", "取消原因", "废标情形")
_PROJECT_LABELS = ("项目编号", "项目编码", "采购编号", "招标编号")

_NOTICE_NO_REGEX = re.compile(
    r"(?:公告编号|公告文号|公示编号|文号)\s*[:：]?\s*"
    r"([A-Za-z0-9\-_〔〕\[\]（）().]{2,40}号?)"
)
_REF_REGEX = re.compile(
    r"(?:原|被更正|引用)[^。；;\n]{0,12}?(?:公告编号|公告文号|公示编号|文号)\s*[:：]?\s*"
    r"([A-Za-z0-9\-_〔〕\[\]（）().]{2,40}号?)"
)
# 供应商名称切分：顿号/逗号/分号/斜杠/换行
_PARTY_SPLIT = re.compile(r"[、,，；;/\n]+")


@dataclass
class PackageEntry:
    """一条结果公告在单个包上的结论。"""

    package_no: str | None
    winners: list[str] = field(default_factory=list)
    reject_reason: str | None = None


@dataclass
class ResultDetailData:
    """结果详情页解析结果。"""

    title: str
    project_code: str | None
    notice_no: str | None
    refers_to: str | None
    published_at: "object | None"
    kind_hint: str  # award / reject / correction / unknown
    entries: list[PackageEntry] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _detect_result_kind(title: str) -> str:
    # 更正优先：标题常同时含“中标结果”与“更正”
    if any(k in title for k in _CORRECTION_KEYWORDS):
        return "correction"
    if any(k in title for k in _REJECT_KEYWORDS):
        return "reject"
    if any(k in title for k in _AWARD_KEYWORDS):
        return "award"
    return "unknown"


def _split_parties(text: str | None) -> list[str]:
    if not text:
        return []
    parties = []
    for part in _PARTY_SPLIT.split(text):
        name = _clean(part.strip(" ：:、.。()（）"))
        # 过滤纯括号说明与明显非名称片段
        if name and not re.fullmatch(r"[（(].*?[)）]", name) and len(name) >= 2:
            parties.append(name)
    return parties


def _first_regex(pattern: re.Pattern[str], text: str) -> str | None:
    match = pattern.search(text)
    return _clean(match.group(1)) if match else None


def _extract_notice_no(soup, body_text: str) -> str | None:
    return (
        _data_field(soup, "notice-no")
        or _labeled_value(soup, _NOTICE_NO_LABELS)
        or _first_regex(_NOTICE_NO_REGEX, body_text)
    )


def _extract_ref(soup, body_text: str) -> str | None:
    return (
        _data_field(soup, "ref-notice-no")
        or _labeled_value(soup, _REF_NOTICE_LABELS)
        or _first_regex(_REF_REGEX, body_text)
    )


def _extract_result_tables(soup) -> list[PackageEntry]:
    """读取“包号 + 中标单位/废标原因”结构的多包结果表。"""
    entries: list[PackageEntry] = []
    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if not rows:
            continue
        headers = [_clean(c.get_text()) for c in rows[0].find_all(["th", "td"])]
        no_idx = _header_index(headers, _PACKAGE_NO_LABELS)
        winner_idx = _header_index(headers, _WINNER_LABELS)
        reason_idx = _header_index(headers, _REJECT_REASON_LABELS)
        if no_idx is None or (winner_idx is None and reason_idx is None):
            continue
        for tr in rows[1:]:
            cells = tr.find_all(["td", "th"])
            if len(cells) <= no_idx:
                continue
            pkg = _coerce_bare_package(_clean(cells[no_idx].get_text()))
            if pkg is None:
                continue
            winners = (
                _split_parties(_clean(cells[winner_idx].get_text()))
                if winner_idx is not None and len(cells) > winner_idx
                else []
            )
            reason = (
                _clean(cells[reason_idx].get_text())
                if reason_idx is not None and len(cells) > reason_idx
                else None
            ) or None
            entries.append(PackageEntry(package_no=pkg, winners=winners, reject_reason=reason))
    return entries


def parse_result_detail(html: str, url: str) -> ResultDetailData:
    """解析中标/废标/结果更正详情页。"""
    soup = _soup(html)
    title_node = soup.find("h1") or soup.find("title")
    title = _clean(title_node.get_text()) if title_node else ""
    if not title:
        raise AuditParseError(f"结果详情页缺少标题：{url}")

    body_text = _clean(soup.get_text(" "))
    project_code = (
        _data_field(soup, "project-code") or _labeled_value(soup, _PROJECT_LABELS)
    )
    project_code = _clean(project_code) or None
    notice_no = _extract_notice_no(soup, body_text)
    refers_to = _extract_ref(soup, body_text)
    published_raw = (
        _data_field(soup, "published-at")
        or _labeled_value(soup, ("发布时间", "发布日期", "公示时间", "公告时间"))
    )
    published_at = parse_datetime(published_raw)
    kind_hint = _detect_result_kind(title)

    entries = _extract_result_tables(soup)
    if not entries:
        # 单包布局：包号 + 中标单位/废标原因
        pkg = _coerce_bare_package(
            _data_field(soup, "package-no") or _labeled_value(soup, _PACKAGE_NO_LABELS)
        )
        winners = _split_parties(
            _data_field(soup, "winners")
            or _data_field(soup, "winner")
            or _labeled_value(soup, _WINNER_LABELS)
        )
        reason = (
            _data_field(soup, "reject-reason")
            or _labeled_value(soup, _REJECT_REASON_LABELS)
        )
        reason = _clean(reason) or None
        if pkg or winners or reason:
            entries.append(PackageEntry(package_no=pkg, winners=winners, reject_reason=reason))

    warnings: list[str] = []
    if published_at is None:
        warnings.append("未能解析发布时间，无法校验更正先后顺序")
    if kind_hint == "correction" and not refers_to:
        warnings.append("更正公告未引用原结果公告编号")
    if not entries:
        warnings.append("未解析到任何包号/中标单位/废标原因，结论无法采信")

    return ResultDetailData(
        title=title,
        project_code=project_code,
        notice_no=notice_no,
        refers_to=refers_to,
        published_at=published_at,
        kind_hint=kind_hint,
        entries=entries,
        warnings=warnings,
    )
