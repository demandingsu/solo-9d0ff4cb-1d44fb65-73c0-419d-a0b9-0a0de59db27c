"""逐包结果核查：把抓到的结果公告按包号/公告编号关联并产出结论。

规则（对应核查需求）：

* 原公告列出的每个包号都必须给出结论；**未见结果不等于漏发**——
  只有目录全部读取成功且该包确实没有任何结果时，才判定漏发（``missing``）；
* 更正公告只有在 **引用了同包已有结果公告编号、且发布时间更晚** 时，
  才替换该包结论；引用编号缺失/找不到对应公告/发布时间不晚 → 更正不生效，
  该包标记 ``pending`` 待核实（不静默采用，也不丢弃来源）；
* 同一包存在多条并列中标结果且中标单位不同 → ``conflict``；
* 同一包同时出现中标与废标结论且无有效更正解释 → ``conflict``；
* 目录分页或关联详情读取失败、详情解析不出可采信结论 → 相关包 ``pending``。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from app.models import (
    PackageResult,
    PackageStatus,
    ResultKind,
    ResultNoticeRecord,
)


@dataclass
class AuditNotice:
    """采集侧已解析的一条结果公告（可能覆盖多个包）。"""

    url: str
    title: str
    kind: ResultKind
    project_code: str | None
    notice_no: str | None
    refers_to: str | None
    published_at: datetime | None
    entries: list  # list[audit_parser.PackageEntry]
    warnings: list[str] = field(default_factory=list)


@dataclass
class AuditInputs:
    """核查器输入。"""

    expected_packages: list[tuple[str, str]]  # (包号, 包名称)，来自原公告
    notices: list[AuditNotice] = field(default_factory=list)
    catalog_failures: list[str] = field(default_factory=list)   # 目录页/分页抓取失败 URL
    # (失败 URL, 目录行可归属到的包号)；包号为空表示无法归属，视为全局性失败
    detail_failures: list[tuple[str, tuple[str, ...]]] = field(default_factory=list)
    parse_warnings: list[str] = field(default_factory=list)     # 页面级解析告警（含来源描述）
    catalog_incomplete: bool = False                            # 触达分页上限，目录未读全


def _sort_key(notice: AuditNotice) -> datetime:
    return notice.published_at or datetime.max  # 无发布时间的排最后，便于“更晚”判定从严


def reconcile(data: AuditInputs) -> tuple[list[PackageResult], list[str]]:
    """返回 ``(逐包结论, 全局告警)``。"""
    warnings: list[str] = []
    for url in data.catalog_failures:
        warnings.append(f"结果目录页读取失败：{url}")
    for url, _pkgs in data.detail_failures:
        warnings.append(f"关联结果详情读取失败：{url}")
    warnings.extend(data.parse_warnings)

    # 按包归集记录。notice_no -> 同包记录，用于更正引用校验
    by_package: dict[str, list[ResultNoticeRecord]] = {
        pkg: [] for pkg, _ in data.expected_packages
    }
    # 更正公告可能覆盖多个包；按 (包号, 公告编号) 建索引
    records_index: dict[str, dict[str, ResultNoticeRecord]] = {
        pkg: {} for pkg, _ in data.expected_packages
    }
    orphan_packages: set[str] = set()  # 结果公告里出现、原公告未列示的包号（仅告警，不臆造为漏发依据）

    def _ensure_package(pkg: str) -> None:
        if pkg not in by_package:
            orphan_packages.add(pkg)
            by_package[pkg] = []
            records_index[pkg] = {}

    ordered = sorted(data.notices, key=_sort_key)

    # ------------------------------------------------------------------ #
    # 第一轮：把 award / reject / unknown 记录登记到对应包
    # （更正公告留到第二轮，避免未生效更正被当作基础结论/引用目标）
    # ------------------------------------------------------------------ #
    for notice in ordered:
        if notice.kind == ResultKind.CORRECTION:
            continue
        for entry in notice.entries:
            pkg = entry.package_no
            if pkg is None:
                continue
            _ensure_package(pkg)
            record = ResultNoticeRecord(
                url=notice.url,
                title=notice.title,
                kind=notice.kind,
                package_no=pkg,
                notice_no=notice.notice_no,
                refers_to=notice.refers_to,
                published_at=notice.published_at,
                winners=list(entry.winners),
                reject_reason=entry.reject_reason,
                applied=True,
            )
            by_package[pkg].append(record)
            if notice.notice_no and notice.notice_no not in records_index[pkg]:
                records_index[pkg][notice.notice_no] = record

    # ------------------------------------------------------------------ #
    # 第二轮：校验并应用更正公告
    # ------------------------------------------------------------------ #
    for notice in ordered:
        if notice.kind != ResultKind.CORRECTION:
            continue
        for entry in notice.entries:
            pkg = entry.package_no
            if pkg is None:
                continue
            _ensure_package(pkg)

            record = ResultNoticeRecord(
                url=notice.url,
                title=notice.title,
                kind=ResultKind.CORRECTION,
                package_no=pkg,
                notice_no=notice.notice_no,
                refers_to=notice.refers_to,
                published_at=notice.published_at,
                winners=list(entry.winners),
                reject_reason=entry.reject_reason,
                applied=False,
            )
            reason = _validate_correction(record, records_index.get(pkg, {}))
            if reason is None:
                record.applied = True
                if notice.notice_no:
                    records_index[pkg][notice.notice_no] = record
            else:
                record.invalid_reason = reason
                warnings.append(
                    f"更正公告未生效（{pkg}）：{reason}，来源 {notice.url}"
                )
            by_package[pkg].append(record)

    for pkg in sorted(orphan_packages):
        warnings.append(
            f"结果公告中出现原公告未列示的包号 {pkg}，仅记录来源，不据此调整其他包结论"
        )

    # ------------------------------------------------------------------ #
    # 第三轮：逐包产出结论。只对原公告列示的包出 PackageResult
    # ------------------------------------------------------------------ #
    catalog_broken = bool(data.catalog_failures) or data.catalog_incomplete
    # 可归属到具体包的详情失败；无法归属（空元组）的失败视为全局性失败
    global_detail_failure = any(not pkgs for _u, pkgs in data.detail_failures)
    failure_packages: set[str] = {
        pkg for _u, pkgs in data.detail_failures for pkg in pkgs
    }
    results: list[PackageResult] = []
    for pkg, pkg_name in data.expected_packages:
        records = by_package.get(pkg, [])
        results.append(
            _evaluate_package(
                pkg,
                pkg_name,
                records,
                catalog_broken=catalog_broken,
                global_detail_failure=global_detail_failure,
                package_failed=pkg in failure_packages,
            )
        )

    return results, warnings


def _validate_correction(
    record: ResultNoticeRecord,
    package_index: dict[str, ResultNoticeRecord],
) -> str | None:
    """返回 None 表示更正生效；否则返回不生效原因。"""
    if not record.refers_to:
        return "更正公告未引用原结果公告编号"
    target = package_index.get(record.refers_to)
    if target is None:
        return f"引用的公告编号 {record.refers_to} 在同包结果中不存在"
    if record.published_at is None or target.published_at is None:
        return "更正或被引用公告缺少发布时间，无法校验先后"
    if record.published_at <= target.published_at:
        return (
            f"更正发布时间 {record.published_at:%Y-%m-%d %H:%M} 不晚于"
            f"原结果发布时间 {target.published_at:%Y-%m-%d %H:%M}"
        )
    return None


def _evaluate_package(
    pkg: str,
    pkg_name: str,
    records: list[ResultNoticeRecord],
    *,
    catalog_broken: bool,
    global_detail_failure: bool,
    package_failed: bool,
) -> PackageResult:
    reasons: list[str] = []

    # 1) 无任何记录
    if not records:
        # 目录不完整、全局失败或本包详情失败 → 待核实；不得把“未见”断言为漏发
        if catalog_broken or global_detail_failure or package_failed:
            reasons.append("目录或关联详情读取失败，未能取得该包结果公示，标记待核实（不臆断漏发）")
            return PackageResult(
                package_no=pkg, status=PackageStatus.PENDING,
                conclusion="pending", reasons=reasons, notices=[],
            )
        reasons.append("原公告列示该包，但结果目录全部读取成功且未见任何结果公示，疑似漏发")
        return PackageResult(
            package_no=pkg, status=PackageStatus.MISSING,
            conclusion="missing", reasons=reasons, notices=[],
        )

    # 2) 存在无法采信/不生效的记录 → 待核实优先（信息链不完整不能下确定结论）
    invalid = [r for r in records if r.kind == ResultKind.CORRECTION and not r.applied]
    unparsable = [r for r in records if r.kind == ResultKind.UNKNOWN]
    undated = [r for r in records if r.published_at is None]
    if invalid or unparsable or undated:
        for rec in invalid:
            reasons.append(f"更正公告未生效：{rec.invalid_reason}（{rec.url}）")
        for rec in unparsable:
            reasons.append(f"公告类型无法识别，结论无法采信（{rec.url}）")
        for rec in undated:
            reasons.append(f"公告缺少可解析的发布时间（{rec.url}）")
        return PackageResult(
            package_no=pkg, status=PackageStatus.PENDING,
            conclusion="pending", reasons=reasons,
            notices=sorted(records, key=lambda r: r.published_at or datetime.max),
        )

    ordered_records = sorted(records, key=lambda r: r.published_at or datetime.max)

    # 3) 只采信“生效中”的结论：有效更正替换其引用的旧结论
    effective = _effective_records(ordered_records)

    awards = [r for r in effective if r.kind == ResultKind.AWARD]
    rejects = [r for r in effective if r.kind == ResultKind.REJECT]

    # 4) 中标与废标互斥
    if awards and rejects:
        winners = sorted({w for r in awards for w in r.winners})
        reasons.append(
            "同包并存中标与废标结论且无有效更正衔接："
            + "；".join(
                f"[{r.kind.value}] {r.title}（{r.url}）"
                for r in (awards + rejects)
            )
        )
        return PackageResult(
            package_no=pkg, status=PackageStatus.CONFLICT,
            conclusion="conflict", winners=winners, reasons=reasons, notices=ordered_records,
        )

    # 5) 并列中标结果：中标单位不同 → 冲突
    if len(awards) > 1:
        winner_sets = {tuple(sorted(r.winners)) for r in awards}
        if len(winner_sets) > 1:
            reasons.append(
                "并列中标结果的中标单位不一致："
                + "；".join(
                    f"{r.title}→{'、'.join(r.winners) or '未载明单位'}（{r.url}）"
                    for r in awards
                )
            )
            return PackageResult(
                package_no=pkg, status=PackageStatus.CONFLICT,
                conclusion="conflict",
                winners=sorted({w for r in awards for w in r.winners}),
                reasons=reasons, notices=ordered_records,
            )

    # 6) 正常结论
    if awards:
        winners = sorted({w for r in awards for w in r.winners})
        if not winners:
            reasons.append("中标结果未载明中标单位，无法形成可核验结论")
            return PackageResult(
                package_no=pkg, status=PackageStatus.PENDING,
                conclusion="pending", reasons=reasons, notices=ordered_records,
            )
        return PackageResult(
            package_no=pkg, status=PackageStatus.OK,
            conclusion="award", winners=winners, notices=ordered_records,
        )

    if rejects:
        reason = next((r.reject_reason for r in rejects if r.reject_reason), None)
        reject_note = "废标/流标" + (f"：{reason}" if reason else "")
        return PackageResult(
            package_no=pkg, status=PackageStatus.OK,
            conclusion="reject", reasons=[reject_note], notices=ordered_records,
        )

    # 只剩生效更正却没有任何基础结论（理论上更正校验已拦住，双保险）
    reasons.append("仅有更正公告，缺少可替换的同包原始结果，待核实")
    return PackageResult(
        package_no=pkg, status=PackageStatus.PENDING,
        conclusion="pending", reasons=reasons, notices=ordered_records,
    )


def _effective_records(ordered: list[ResultNoticeRecord]) -> list[ResultNoticeRecord]:
    """按有效更正链剔除被替换的旧记录。

    生效更正引用某公告编号，则该编号记录被替换；更正自身携带的结论进入有效集。
    """
    replaced_notice_nos: set[str] = set()
    for rec in ordered:
        if rec.kind == ResultKind.CORRECTION and rec.applied and rec.refers_to:
            replaced_notice_nos.add(rec.refers_to)
    effective: list[ResultNoticeRecord] = []
    for rec in ordered:
        if rec.notice_no and rec.notice_no in replaced_notice_nos:
            continue
        if rec.kind == ResultKind.CORRECTION and rec.applied:
            # 更正承载的新结论按其内容归类为 award / reject（复制记录，不改写原始留痕）
            new_kind = ResultKind.REJECT if rec.reject_reason else ResultKind.AWARD
            effective.append(rec.model_copy(update={"kind": new_kind}))
            continue
        effective.append(rec)
    return effective
