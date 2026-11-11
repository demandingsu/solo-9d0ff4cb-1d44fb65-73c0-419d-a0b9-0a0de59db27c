"""结果公示核查编排。

流程：
1. 抓取原公告详情，解析分包表得到包号清单（核查基准，不增不减）；
2. 从结果目录页出发，沿同站分页链接 BFS 逐页抓取，收集结果详情链接；
3. 按项目编号过滤候选详情（列表行上下文 + 详情页编号双重判定），并发抓取解析；
4. 交给 :mod:`app.reconciler` 逐包关联、校验更正、产出结论与来源。
"""

from __future__ import annotations

import asyncio
import re
from datetime import datetime, timezone

import httpx
from fastapi import HTTPException

from app.audit_parser import (
    AuditParseError,
    _PACKAGE_NO_INLINE,
    extract_catalog_links,
    extract_packages,
    normalize_package_no,
    parse_result_detail,
)
from app.collector import _normalize_code
from app.models import (
    PackageStatus,
    ResultKind,
    SourceLink,
    VerifyRequest,
    VerifyResponse,
)
from app.reconciler import AuditInputs, AuditNotice, reconcile
from app.parser import _clean

_MAX_CONCURRENCY = 5
_MAX_CATALOG_PAGES = 20
_USER_AGENT = "TenderResultAuditor/1.0 (+local procurement result audit API)"


async def _fetch(client: httpx.AsyncClient, url: str) -> str:
    response = await client.get(url)
    response.raise_for_status()
    return response.text


def _code_matches(page_code: str | None, requested: str, title: str, row_context: str) -> bool:
    target = _normalize_code(requested)
    if page_code and _normalize_code(page_code) == target:
        return True
    return target in _normalize_code(f"{title} {row_context}")


def _row_packages(row_context: str, known_packages: set[str]) -> tuple[str, ...]:
    """从目录行上下文提取该行涉及的包号；无法识别时返回空元组（按全局失败处理）。"""
    found: list[str] = []
    for match in _PACKAGE_NO_INLINE.finditer(_clean(row_context)):
        token = next((g for g in match.groups() if g), "")
        pkg = normalize_package_no(token)
        if pkg and pkg in known_packages and pkg not in found:
            found.append(pkg)
    # 兜底：行文本中直接出现已知裸包号（如“包号 3”之类未被内联正则覆盖的写法）
    for pkg in known_packages:
        if re.search(rf"(?<![0-9A-Za-z]){re.escape(pkg)}(?![0-9A-Za-z])", row_context):
            if pkg not in found:
                found.append(pkg)
    return tuple(found)


def _aggregate_status(packages) -> PackageStatus:
    """项目级汇总，按严重程度 conflict > pending > missing > ok。"""
    order = [
        PackageStatus.CONFLICT,
        PackageStatus.PENDING,
        PackageStatus.MISSING,
        PackageStatus.OK,
    ]
    for status in order:
        if any(pkg.status == status for pkg in packages):
            return status
    return PackageStatus.PENDING


async def verify(req: VerifyRequest) -> VerifyResponse:
    original_url = str(req.original_url)
    catalog_url = str(req.catalog_url)
    timeout = httpx.Timeout(req.timeout)

    async with httpx.AsyncClient(
        timeout=timeout, headers={"User-Agent": _USER_AGENT}, follow_redirects=True
    ) as client:
        # 1) 原公告：包号清单是核查基准，抓不到直接 502
        try:
            original_html = await _fetch(client, original_url)
        except httpx.HTTPError as exc:
            raise HTTPException(
                status_code=502,
                detail=f"原公告详情抓取失败：{original_url}（{exc.__class__.__name__}）",
            ) from exc

        expected_packages = extract_packages(original_html)

        # 2) 目录沿同站分页 BFS
        candidates, catalog_failures, page_urls, limit_hit = await _crawl_catalog(
            client, catalog_url
        )

        parse_warnings: list[str] = []
        if not expected_packages:
            parse_warnings.append(
                f"原公告未解析到任何包号（需含“包号”列的分包表或“第N包/包号：…”表述）：{original_url}"
            )

        target_code = _normalize_code(req.project_code)
        known_packages = {pkg for pkg, _ in expected_packages}
        semaphore = asyncio.Semaphore(_MAX_CONCURRENCY)

        async def fetch_one(url: str, link_text: str, context: str):
            async with semaphore:
                try:
                    html = await _fetch(client, url)
                    return url, html, link_text, context
                except httpx.HTTPError:
                    return url, "", link_text, context

        # 3) 并发抓取候选详情
        fetched = await asyncio.gather(
            *(fetch_one(u, t, c) for u, t, c in candidates)
        )

        notices: list[AuditNotice] = []
        detail_failures: list[tuple[str, tuple[str, ...]]] = []
        matched_urls: set[str] = set()

        kind_map = {
            "award": ResultKind.AWARD,
            "reject": ResultKind.REJECT,
            "correction": ResultKind.CORRECTION,
            "unknown": ResultKind.UNKNOWN,
        }

        for url, html, link_text, context in fetched:
            row_mentions = target_code in _normalize_code(context)
            row_pkgs = _row_packages(context, known_packages)
            if not html:
                if row_mentions:
                    detail_failures.append((url, row_pkgs))
                continue
            try:
                data = parse_result_detail(html, url)
            except AuditParseError:
                if row_mentions:
                    detail_failures.append((url, row_pkgs))
                continue

            if not _code_matches(data.project_code, req.project_code, data.title, context):
                continue  # 同站但属于其他项目
            matched_urls.add(url)

            for warn in data.warnings:
                parse_warnings.append(f"{url}：{warn}")

            notices.append(
                AuditNotice(
                    url=url,
                    title=data.title,
                    kind=kind_map[data.kind_hint],
                    project_code=data.project_code,
                    notice_no=data.notice_no,
                    refers_to=data.refers_to,
                    published_at=data.published_at,
                    entries=data.entries,
                    warnings=data.warnings,
                )
            )

        # 列表行归属本项目、但详情解析后没有任何可用包条目的页面也要人工核实
        for url, html, _t, context in fetched:
            if not html or url in matched_urls or any(u == url for u, _ in detail_failures):
                continue
            if target_code in _normalize_code(context):
                detail_failures.append((url, _row_packages(context, known_packages)))
                parse_warnings.append(f"{url}：页面未解析出可采信的包级结果，标记待核实")

    # 4) 逐包核查
    packages, warnings = reconcile(
        AuditInputs(
            expected_packages=expected_packages,
            notices=notices,
            catalog_failures=catalog_failures,
            detail_failures=detail_failures,
            parse_warnings=parse_warnings,
            catalog_incomplete=limit_hit,
        )
    )

    # 5) 来源链接
    sources: list[SourceLink] = [
        SourceLink(role="original", url=original_url, title="原招标/采购公告（包号清单来源）"),
    ]
    for page in page_urls:
        sources.append(SourceLink(role="catalog", url=page, title="结果公示目录页"))
    role_by_url: dict[str, str] = {}
    title_by_url: dict[str, str] = {}
    for notice in notices:
        role_by_url[notice.url] = notice.kind.value
        title_by_url[notice.url] = notice.title
    for url in sorted(matched_urls):
        sources.append(
            SourceLink(
                role=role_by_url.get(url, "result"),
                url=url,
                title=title_by_url.get(url),
            )
        )
    failed_urls = {u for u, _ in detail_failures} | {
        u for u in catalog_failures if u.startswith(("http://", "https://"))
    }
    for url in sorted(failed_urls):
        sources.append(SourceLink(role="failed", url=url, title="读取失败-待核实"))

    if not expected_packages:
        overall = PackageStatus.PENDING
    else:
        overall = _aggregate_status(packages)

    return VerifyResponse(
        project_code=req.project_code,
        original_url=req.original_url,
        catalog_url=req.catalog_url,
        status=overall,
        packages=packages,
        warnings=warnings,
        sources=sources,
        fetched_at=datetime.now(timezone.utc),
    )


async def _crawl_catalog(
    client: httpx.AsyncClient, start_url: str
) -> tuple[
    list[tuple[str, str, str]],  # 详情链接
    list[str],                   # 目录/分页读取失败 URL
    list[str],                   # 成功读取的目录页 URL（来源留痕）
    bool,                        # 是否触达分页上限（目录不完整）
]:
    """沿同站分页链接 BFS 抓取目录，汇总所有详情链接。"""
    queue: list[str] = [start_url]
    visited: set[str] = set()
    all_details: list[tuple[str, str, str]] = []
    seen_detail: set[str] = set()
    failures: list[str] = []
    page_urls: list[str] = []
    limit_hit = False

    while queue:
        if len(visited) >= _MAX_CATALOG_PAGES:
            limit_hit = True
            break
        url = queue.pop(0)
        if url in visited:
            continue
        visited.add(url)
        try:
            html = await _fetch(client, url)
        except httpx.HTTPError:
            failures.append(url)
            continue
        page_urls.append(url)
        details, pages = extract_catalog_links(html, url)
        for link in details:
            if link[0] not in seen_detail:
                seen_detail.add(link[0])
                all_details.append(link)
        for page in pages:
            if page not in visited and page not in queue:
                queue.append(page)

    if limit_hit:
        failures.append(f"分页抓取达到上限 {_MAX_CATALOG_PAGES} 页，后续目录未读取")
    return all_details, failures, page_urls, limit_hit
