"""Pydantic 请求/响应模型。"""
from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field

Direction = Literal["ltr", "rtl", "ttb", "auto"]
FeatureValue = Literal["on", "off"]
Scenario = Literal["web", "desktop", "print"]


class FeatureToggle(BaseModel):
    """单个 OpenType 特性开关，如 liga / calt / kern / smcp。"""

    tag: str = Field(..., min_length=4, max_length=4, pattern=r"[ -~]{4}")
    value: FeatureValue = "on"


class AnalyzeRequest(BaseModel):
    text: str = Field(..., min_length=1)
    # 字体栈按优先级排列，每项为字体摘要 (sha256)
    font_stack: list[str] = Field(..., min_length=1)
    language: str = Field(
        "dflt",
        description="BCP-47 / hb_language，例如 en、zh-Hans、ar、he",
    )
    script: Optional[str] = Field(
        None, description="ISO-15924 书写系统标签（latn/arab/hebr/hani…），留空自动推断"
    )
    direction: Direction = "auto"
    features: list[FeatureToggle] = Field(default_factory=list)
    # 不进行 Unicode 规范化，严格按原始输入整形
    normalize: Literal["none", "nfc", "nfkc"] = "none"


class ReanalyzeRequest(BaseModel):
    """以保存记录的固定参数重新执行（参数内嵌在记录中，无需客户端重传）。"""

    force_refresh: bool = False


# ------------------------------------------------- 许可与嵌入合规门禁模型

class PolicyCreateRequest(BaseModel):
    """登记不可变策略版本；版本号由服务端线性分配。"""

    scenario: Scenario
    # CAS 基准：当前生效版本；尚无生效版本时必须传 0
    expected_active_version: int = Field(..., ge=0)
    policy: dict = Field(..., description="策略规则体")
    activate: bool = Field(True, description="登记成功后是否原子切换为生效版本")
    registered_by: str = Field("admin", min_length=1, max_length=120)


class PolicyActivateRequest(BaseModel):
    expected_active_version: int = Field(..., ge=0)


class ExemptionCreateRequest(BaseModel):
    scenario: Scenario
    policy_version: int = Field(..., ge=1)
    font_digest: str = Field(..., min_length=64, max_length=64)
    rule_codes: list[str] = Field(..., min_length=1,
                                  description="豁免检查代码，可用 * 表示全代码")
    justification: str = Field(..., min_length=1, max_length=2000)
    requested_by: str = Field("admin", min_length=1, max_length=120)
    expires_at: Optional[datetime] = Field(
        None, description="ISO-8601 到期时间（UTC）；省略=长期")
    applies_to_stack_conflict: bool = False


class GateEvaluateRequest(BaseModel):
    scenario: Scenario
    font_stack: list[str] = Field(..., min_length=1)
    policy_version: Optional[int] = Field(
        None, description="省略或 null=当前生效版本")


class GatePublishRequest(GateEvaluateRequest):
    request_id: Optional[str] = Field(
        None, max_length=200,
        description="客户端幂等键；同键重复发布返回同一冻结决定")
