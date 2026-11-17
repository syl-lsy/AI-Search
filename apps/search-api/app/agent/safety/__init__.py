"""safety：B13 输入安全检测件（纯函数优先，单测全 mock 零网络）。

四件职责（接入面在后续工单——主图壳 pre_route / rewrite / planner 节点接线不属本包）：
- `config`：Agent Spec `safety` 节 → `SafetyConfig`（fail-open：不可用返回 None 直通）；
- `sanitizer`：PII 检测与脱敏（sanitize 通道，纯函数）；
- `lexicon`：词表硬命中 + grey 规则扫描（Layer 1 代码层直拦判据，纯函数）；
- `reply`：类别化拒答话术生成（reject 通道，light 模型可注入、模板段文本兜底）。

分层约束：本包只被 agent 层节点/services 消费，不依赖 nodes/（唯一例外 reply 对
`build_light_model` 的函数内延迟 import）与 api/。
"""

from app.agent.safety.config import SafetyConfig, load_safety_config
from app.agent.safety.lexicon import LexHit, LexScan, scan_lexicon
from app.agent.safety.reply import generate_rejection_reply
from app.agent.safety.sanitizer import PiiFinding, detect_pii, sanitize

__all__ = [
    "LexHit",
    "LexScan",
    "PiiFinding",
    "SafetyConfig",
    "detect_pii",
    "generate_rejection_reply",
    "load_safety_config",
    "sanitize",
    "scan_lexicon",
]
