"""safety.lexicon：词表硬命中 + grey 规则扫描（B13 Layer 1 代码层直拦通道）。

语义（nacos-config.md §1 v1.30）：词表硬命中由代码层直拦、不经 grey LLM 复核；
仅 grey_rules（掩码/分隔变形正则）命中的查询送 `common.safety_check` LLM 复核——
故 **grey = 规则任一命中且词表未硬命中**（硬命中压 grey）。

匹配口径：
- 词表为**精确（子串）匹配**——中文无词边界，条目原文 containment；latin 大小写
  不敏感（casefold 双侧归一；Hit 只携带 term 原文，无 span 需求故无索引漂移问题）；
- grey 正则按 config.grey_rules（config.py 已编译，原样编译不加 flags——种子规则
  自带 `[Bb]` 等大小写处理）对原文 `search`。

纯函数零 IO；处置优先级序 danger → profanity → privacy → politics（词表配置序，
输出确定性；safety_check 按类别优先级取首个拒答类别）。
"""

from dataclasses import dataclass, field

from app.agent.safety.config import SafetyConfig

__all__ = [
    "LexHit",
    "LexScan",
    "scan_lexicon",
]


@dataclass(frozen=True)
class LexHit:
    """一处词表硬命中（kind ∈ danger|profanity|privacy|politics，term 为词表条目原文）。"""

    kind: str
    term: str


@dataclass(frozen=True)
class LexScan:
    """词表扫描结果：hard_hits（含任一即代码层直拦）+ grey（送 LLM 复核标记）。"""

    hard_hits: list[LexHit] = field(default_factory=list)
    grey: bool = False


def scan_lexicon(text: str, config: SafetyConfig) -> LexScan:
    """对 text 做词表精确匹配 + grey 正则扫描（纯函数，零 IO）。

    词表大小写不敏感仅对 latin 生效（casefold 对中文恒等）；grey 正则作用于原文
    （编译时未加 flags，变形掩码规则自带大小写处理）。mode 门控：lexicon（仅词表
    语义）下跳过 grey 正则扫描——grey 结果在 lexicon 模式恒被弃置，扫描是白付的
    CPU 成本（ReDoS 残余面同理随门控消除）。
    """
    if not isinstance(text, str):
        text = "" if text is None else str(text)
    normalized = text.casefold()
    hits: list[LexHit] = []
    for term in config.danger_terms:
        if term.casefold() in normalized:
            hits.append(LexHit(kind="danger", term=term))
    for term in config.profanity_terms:
        if term.casefold() in normalized:
            hits.append(LexHit(kind="profanity", term=term))
    # B13 增量一（nacos-config.md §3 v1.31）：隐私侵害意图词表——命中即代码层直拦
    # reject（privacy 一律 reject 不得 sanitize，处置优先级 danger > profanity >
    # privacy > pii > grey）
    for term in config.privacy_terms:
        if term.casefold() in normalized:
            hits.append(LexHit(kind="privacy", term=term))
    # B14 增量 B（nacos-config.md §3 v1.32）：涉政敏感话题词表——命中即代码层直拦
    # reject（全程不搜索不综合；初值空表由用户自填，空表即本通道零命中不误拦）
    for term in config.politics_terms:
        if term.casefold() in normalized:
            hits.append(LexHit(kind="politics", term=term))
    grey = (
        config.mode == "hybrid"
        and not hits
        and any(rule.search(text) for rule in config.grey_rules)
    )
    return LexScan(hard_hits=hits, grey=grey)
