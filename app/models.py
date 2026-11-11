"""Pydantic 数据模型：请求 / 响应 / 内部记录。"""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field, HttpUrl


class ProjectStatus(str, Enum):
    """投标状态。

    OPEN         可投标（存在有效截止时间，且未取消）
    CLOSED       不可投标（公告明确取消采购）
    PENDING      待核实（抓取/解析失败，或无法得到有效截止时间，不能把旧时间当最终期限）
    NOT_FOUND    列表页未发现与该项目编号相关的同站点公告
    """

    OPEN = "open"
    CLOSED = "closed"
    PENDING = "pending"
    NOT_FOUND = "not_found"


class NoticeKind(str, Enum):
    ORIGINAL = "original"          # 原公告
    CORRECTION = "correction"      # 更正公告（含澄清、变更、延期）
    CANCELLATION = "cancellation"  # 取消公告（更正的一种，且明确取消采购）


class FetchRequest(BaseModel):
    """采集请求。"""

    list_url: HttpUrl = Field(..., description="采购公告列表页 URL")
    project_code: str = Field(..., min_length=1, description="项目编号，如 GSB-2026-001")
    timeout: float = Field(10.0, ge=1.0, le=60.0, description="单次请求超时时间（秒）")


class NoticeRecord(BaseModel):
    """单条公告详情的解析结果（按发布时间排序）。"""

    url: HttpUrl
    kind: NoticeKind
    title: str
    published_at: datetime | None = None
    deadline: datetime | None = Field(
        None, description="该公告声明的投标截止时间；未提及则为 None，合并时沿用已生效时间"
    )
    cancelled: bool = False
    cancellation_reason: str | None = None


class SourceLink(BaseModel):
    """来源链接（列表页 + 参与合并的详情页）。"""

    role: str  # list / original / correction / cancellation
    url: HttpUrl
    title: str | None = None


class FetchResponse(BaseModel):
    """采集响应。"""

    project_code: str
    list_url: HttpUrl
    status: ProjectStatus
    can_bid: bool = Field(..., description="当前是否可投标")
    effective_deadline: datetime | None = Field(
        None, description="有效投标截止时间；待核实/未找到时为 None，绝不返回旧时间冒充最终期限"
    )
    cancelled: bool = False
    notices: list[NoticeRecord] = Field(default_factory=list)
    sources: list[SourceLink] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    fetched_at: datetime


# --------------------------------------------------------------------------- #
# 结果公示核查（中标/废标/结果更正，按包号关联）
# --------------------------------------------------------------------------- #
class ResultKind(str, Enum):
    """结果公告类型。

    AWARD       中标/成交结果公告
    REJECT      废标/流标/终止结果公告
    CORRECTION  结果更正公告（须引用同包已有结果且发布更晚才生效）
    UNKNOWN     无法识别结论类型，按待核实处理
    """

    AWARD = "award"
    REJECT = "reject"
    CORRECTION = "correction"
    UNKNOWN = "unknown"


class PackageStatus(str, Enum):
    """逐包核查结论。

    OK        结论一致且链路完整（中标或废标）
    CONFLICT  并列结果中标单位不同，或中标与废标互斥
    MISSING   原公告列示该包，且目录已完整读取却未见任何结果公示（漏发）
    PENDING   目录/关联详情读取失败、更正引用缺失等，无法断言漏发或给出结论
    """

    OK = "ok"
    CONFLICT = "conflict"
    MISSING = "missing"
    PENDING = "pending"


class VerifyRequest(BaseModel):
    """结果公示核查请求。"""

    original_url: HttpUrl = Field(..., description="原招标/采购公告详情 URL（从中读取包号清单）")
    catalog_url: HttpUrl = Field(..., description="结果公示目录 URL（沿同站分页链接逐页抓取）")
    project_code: str = Field(..., min_length=1, description="项目编号，如 GSB-2026-100")
    timeout: float = Field(10.0, ge=1.0, le=60.0, description="单次请求超时时间（秒）")


class ResultNoticeRecord(BaseModel):
    """单条结果公告在某个包上的解析/校验记录。"""

    url: HttpUrl
    title: str
    kind: ResultKind
    package_no: str = Field(..., description="归一化后的包号，如 3 / G1")
    notice_no: str | None = Field(None, description="本公告编号")
    refers_to: str | None = Field(None, description="更正公告引用的原结果公告编号")
    published_at: datetime | None = None
    winners: list[str] = Field(default_factory=list, description="中标/成交单位（可能并列多家）")
    reject_reason: str | None = None
    applied: bool = Field(True, description="更正是否通过校验并实际替换了结论；未生效更正为 False")
    invalid_reason: str | None = Field(None, description="更正未生效/记录无法采信的原因")


class PackageResult(BaseModel):
    """单个采购包的核查结论。"""

    package_no: str
    status: PackageStatus
    conclusion: str = Field(
        ...,
        description="award=中标/成交，reject=废标/流标，conflict=结论冲突，"
        "missing=疑似漏发，pending=待核实",
    )
    winners: list[str] = Field(default_factory=list, description="核查后采信的中标单位；废标/漏发/待核实为空")
    reasons: list[str] = Field(
        default_factory=list, description="漏发/冲突/待核实的具体原因，正常结论时为空"
    )
    notices: list[ResultNoticeRecord] = Field(default_factory=list, description="该包关联到的全部结果公告记录")


class VerifyResponse(BaseModel):
    """结果公示核查响应。"""

    project_code: str
    original_url: HttpUrl
    catalog_url: HttpUrl
    status: PackageStatus = Field(..., description="项目级汇总：按 conflict > pending > missing > ok 取最严重状态")
    packages: list[PackageResult] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    sources: list[SourceLink] = Field(default_factory=list)
    fetched_at: datetime
