"""safety：输入安全检测件（B13）+ 内容侧护栏（B15）——纯函数优先，单测全 mock 零网络。

六件职责（B13 输入侧接线在 safety_check 节点；B15 内容侧接线在后续工单批——
fast 搜索摘要 / deep 读页 / 子代理报告等调用点）：
- `config`：Agent Spec `safety` 节 → `SafetyConfig`（B13 输入安全，fail-open 直通）
  与 `load_content_guard_config` → `ContentGuardConfig`（B15 内容侧护栏两子节——
  与 safety.enabled 解耦、缺子节回落 defaults 种子=部署即启用）；
- `sanitizer`：PII 检测与脱敏（sanitize 通道，纯函数）；
- `lexicon`：词表硬命中 + grey 规则扫描（Layer 1 代码层直拦判据，纯函数）；
- `reply`：类别化拒答话术生成（reject 通道，light 模型可注入、模板段文本兜底）；
- `url_guard`：SSRF 防护 URL 校验器（B15——scheme/私网判定为代码硬底线，DNS 经
  注入式 resolver，同步纯函数、unit 零网络）；
- `injection`：网页内容注入特征过滤（B15——内置种子 ∪ Nacos 正则并集、归一化
  匹配、Scan 零 payload 回显，flag/drop 处置由接线层消费）。

分层约束：本包只被 agent 层节点/services 消费，不依赖 nodes/（唯一例外 reply 对
`build_light_model` 的函数内延迟 import）与 api/。
"""

from app.agent.safety.config import (
    ContentGuardConfig,
    InjectionFilterConfig,
    SafetyConfig,
    UrlGuardConfig,
    load_content_guard_config,
    load_safety_config,
)
from app.agent.safety.injection import InjectionScan, scan_injection
from app.agent.safety.lexicon import LexHit, LexScan, scan_lexicon
from app.agent.safety.reply import generate_rejection_reply
from app.agent.safety.sanitizer import PiiFinding, detect_pii, sanitize
from app.agent.safety.url_guard import (
    URL_REJECT_REASONS,
    Resolver,
    UrlGuardResult,
    validate_url,
)

__all__ = [
    "ContentGuardConfig",
    "InjectionFilterConfig",
    "InjectionScan",
    "LexHit",
    "LexScan",
    "PiiFinding",
    "Resolver",
    "URL_REJECT_REASONS",
    "SafetyConfig",
    "UrlGuardConfig",
    "UrlGuardResult",
    "detect_pii",
    "generate_rejection_reply",
    "load_content_guard_config",
    "load_safety_config",
    "sanitize",
    "scan_injection",
    "scan_lexicon",
    "validate_url",
]
