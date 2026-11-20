"""safety.url_guard：SSRF 防护 URL 校验器（B15 内容侧安全护栏——同步纯函数，DNS 经注入式 resolver）。

背景与定位（plan-b15 §1）：deep 模式读网页 URL 此前只查 scheme（deep_research.py
scheme-only 判定），`http://169.254.169.254`（云元数据）与 Docker 网内短主机名均可
通过——备位抓取器是 compose 网内容器，可被注入诱导的 URL 打到内网端点、正文回灌
上下文造成信息渗出。本模块是**代码硬底线**：scheme / 私网 / 元数据 / 短主机名判定
为代码常量，Nacos 黑名单（blocked_hosts / blocked_host_patterns）只能加强不能削弱；
关闸走 safety.url_guard.enabled 单键（loader 层）。

判定顺序（validate_url，任一步命中即返回）：
1. 超长（max_url_length）→ scheme（∈ {http,https}，大小写不敏感）→ userinfo；
2. host 归一化：percent-decode → NFKC（全角句点/数字→ASCII）→ 小写 → 去尾点；
3. 归一化后含控制字符 / `%2F`、`%40` 解码出 `/`、`@` → malformed（与客户端权威
   解析歧义面不共舞，宁可拒收）；非 ASCII（IDN 裸形态，客户端 punycode 行为不确定）→ 拒；
4. 黑名单加强：blocked_hosts（等于或任意级子域）与 blocked_host_patterns（search
   匹配）→ host_forbidden；
5. 字面 IP：先归一化混淆形态（十进制 2130706433 / 十六进制 0x7f000001 / 八进制
   0177.0.0.1 / 混合 0x7f.0.0.1 / 两段 127.1——与 inet_aton 接受面一致），再按
   类别规则判（loopback/private/link_local/reserved/multicast/unspecified + 显式
   元数据集 + CGNAT 100.64.0.0/10——3.12 ipaddress.is_private 不含 CGNAT，显式补）；
   IPv6 v4-mapped（::ffff:0:0/96）回查 v4 规则；含 `:` 但非合法 IPv6 字面量 → malformed；
6. 保留主机名：localhost / *.localhost / *.local / *.internal / *.home.arpa /
   无点短主机名（Docker 容器名）→ host_forbidden；
7. DNS：非字面 IP 主机经注入式 resolver 解析，任一解析结果命中类别 5 → 拒
   （nip.io 形态 rebind 面）；解析空表 / socket.gaierror（NXDOMAIN）/ **UnicodeError
   （host 形态非法——空段/超长 label，平台无关预检外的防线）→ 拒**；**超时类
   （TimeoutError）→ 拒（fail-closed——可被攻击者廉价诱导，评审 P2）**；resolver
   其他基础设施异常 **fail-open 放行** + safety 域告警（故障不挡检索；契约面注记：
   解析结果全为非 IP 串时逐条跳过=放行——getaddrinfo 形态不可达）。

结果契约：UrlGuardResult(ok, reason)——reason ∈ URL_REJECT_REASONS 固定枚举、
放行为 None；**零 payload 回显**（dataclass 仅 ok/reason 两字段，不携带 url/host，
errors[].reason 语义面（api.md）只收枚举词）。告警纪律：只记 host/异常类型名，
零完整 URL、零 payload 原文。

调用点包络：本函数是同步纯函数（DNS 为唯一 IO，unit 注入假 resolver 零网络）；
async 节点接线统一 `asyncio.to_thread + asyncio.wait_for` 包络（沿 safety_check.py
扫描线程化先例——DNS/正则耗时不得阻塞事件循环）。已知残余（R21）：本模块不覆盖
抓取方重定向至私网与 DNS rebinding/TOCTOU——第二道防线是抓取容器出网策略（运维面）。
"""

import ipaddress
import socket
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import unquote, urlsplit

from app.agent.safety.config import UrlGuardConfig
from app.core.logger import AgentLog

__all__ = [
    "URL_REJECT_REASONS",
    "Resolver",
    "UrlGuardConfig",
    "UrlGuardResult",
    "validate_url",
]

#: 注入式 resolver 契约：host → 解析地址表（socket.getaddrinfo 形态的字符串 IP 列表）
Resolver = Callable[[str], list[str]]

#: 拒绝原因固定枚举（reason 语义面稳定——api.md errors[].reason 收枚举词，漂移即契约破坏）
URL_REJECT_REASONS: tuple[str, ...] = (
    "malformed_url",
    "url_too_long",
    "scheme_forbidden",
    "userinfo_forbidden",
    "host_forbidden",
    "ip_forbidden",
    "dns_unresolved",
)

#: 允许的 scheme 白名单（大小写不敏感——urlsplit 已将 scheme 小写化）
_ALLOWED_SCHEMES: tuple[str, ...] = ("http", "https")

#: 保留主机名后缀（mDNS / 内部 DNS 后缀 / home.arpa RFC 8375——归一化后判定）
_FORBIDDEN_HOST_SUFFIXES: tuple[str, ...] = (".localhost", ".local", ".internal", ".home.arpa")

#: 显式元数据端点集（is_link_local/is_private 已覆盖大部分——显式列出兜 stdlib
#: 特殊段演进面；100.100.100.200 在 CGNAT 段内由 _CGNAT_NETWORK 兜）
_METADATA_IPS = frozenset(
    {
        ipaddress.ip_address("169.254.169.254"),  # AWS/GCP/Aliyun IMDSv4
        ipaddress.ip_address("100.100.100.200"),  # Aliyun IMDSv4（100.64/10 段内）
        ipaddress.ip_address("fd00:ec2::254"),  # AWS IMDSv6
    }
)

#: CGNAT 运营商级 NAT 段（RFC 6598）——Python 3.12 ipaddress.is_private **不含**该段
#: （实测 100.64.0.1 is_private=False），SSRF 判定必须显式补
_CGNAT_NETWORK = ipaddress.ip_network("100.64.0.0/10")


@dataclass(frozen=True)
class UrlGuardResult:
    """URL 校验结果：ok=放行；reason ∈ URL_REJECT_REASONS（放行为 None）。零 payload
    回显——不携带 url/host 片段，可安全并入 state errors[]。"""

    ok: bool
    reason: str | None


def _warn(message: str) -> None:
    """safety 域告警（graph="safety"，node 标 url_guard；消息只含 host/类型名，零完整 URL）。"""
    AgentLog(message=message, graph="safety", node="url_guard").warning()


def _socket_resolver(host: str) -> list[str]:
    """默认 resolver：socket.getaddrinfo 全地址族（IPv4/IPv6）去重保序。"""
    infos = socket.getaddrinfo(host, None)
    seen: list[str] = []
    for info in infos:
        address = str(info[4][0])
        if address not in seen:
            seen.append(address)
    return seen


def _numeric_part(part: str) -> int | None:
    """单个点分段值解析（十六进制 0x 前缀 / 前导零八进制 / 十进制）；非数值 → None。

    与 inet_aton 接受面一致：前导零按八进制（0177=127）、0x 前缀按十六进制——
    混淆形态归一的原子操作。
    """
    if part.startswith(("0x", "0X")):
        try:
            return int(part, 16)
        except ValueError:
            return None
    if len(part) > 1 and part[0] == "0" and part.isdigit():
        try:
            return int(part, 8)
        except ValueError:
            return None
    if part.isdigit():
        return int(part)
    return None


def _parse_dotted_ipv4(host: str) -> ipaddress.IPv4Address | None:
    """点分混淆形态 → IPv4Address；非字面量形态（段越界等）→ None（交 DNS 面）。

    inet_aton 语义：四段每段 ≤255；三段末段 ≤0xFFFF；两段末段 ≤0xFFFFFF——越界
    形态真实客户端不做字面量解释（走 DNS），本函数同样返回 None 保持一致。
    """
    parts = host.split(".")
    if any(not part for part in parts):
        return None
    values: list[int] = []
    for part in parts:
        number = _numeric_part(part)
        if number is None:
            return None
        values.append(number)
    count = len(values)
    if count == 4:
        if any(value > 255 for value in values):
            return None
        a, b, c, d = values
        return _ipv4((a << 24) | (b << 16) | (c << 8) | d)
    if count == 3:
        a, b, c = values
        if a > 255 or b > 255 or c > 0xFFFF:
            return None
        return _ipv4((a << 24) | (b << 16) | c)
    if count == 2:
        a, b = values
        if a > 255 or b > 0xFFFFFF:
            return None
        return _ipv4((a << 24) | b)
    return _ipv4(values[0])


def _ipv4(value: int) -> ipaddress.IPv4Address | None:
    try:
        return ipaddress.IPv4Address(value)
    except ValueError:
        return None


def _parse_numeric_host(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """无点全数值主机（十进制 2130706433 / 十六进制 0x7f000001 / 八进制 0177）→
    地址对象；超 32 位回 IPv6 解释；不可解析 → None（交短主机名禁令）。"""
    value = _numeric_part(host)
    if value is None or value < 0:
        return None
    v4 = _ipv4(value)
    if v4 is not None:
        return v4
    try:
        return ipaddress.IPv6Address(value)
    except ValueError:
        return None


def _parse_literal_ip(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """字面 IP 识别（含混淆形态）：含 `:` 按 IPv6 字面量（urlsplit 已去方括号）；
    点分按 inet_aton 形态；无点全数值按整数形态。非字面量 → None。"""
    if ":" in host:
        try:
            return ipaddress.ip_address(host)
        except ValueError:
            return None
    if "." in host:
        return _parse_dotted_ipv4(host)
    return _parse_numeric_host(host)


def _ip_forbidden(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """类别判定：元数据显式集 + CGNAT + loopback/private/link_local/reserved/
    multicast/unspecified；IPv6 v4-mapped（::ffff:0:0/96）先映射回 v4 再判
    （`[::ffff:127.0.0.1]` 等价于回环）。"""
    if isinstance(ip, ipaddress.IPv6Address):
        mapped = ip.ipv4_mapped
        if mapped is not None:
            ip = mapped
    if ip in _METADATA_IPS:
        return True
    if isinstance(ip, ipaddress.IPv4Address) and ip in _CGNAT_NETWORK:
        return True
    return bool(
        ip.is_loopback
        or ip.is_private
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


def _normalize_host(raw_host: str) -> str:
    """host 归一化：percent-decode → NFKC（全角字母数字/FF0E 句点→ASCII）→ 表意
    句点归一（U+3002 与 NFKC 后的 U+FF61——NFKC 对两者**不**映射到 `.`，WHATWG
    主机解析做了该映射，客户端会当句点解释，防线必须对齐）→ 小写 → 去尾点。

    顺序即防线：先解码后归一，`127%E3%80%820...`（percent 编码全角句点）与裸
    全角形态（`127。0。0。1`）同样收敛为字面 IP。
    """
    host = unicodedata.normalize("NFKC", unquote(raw_host)).replace("\u3002", ".")
    return host.lower().rstrip(".")


def _host_blocked(host: str, config: UrlGuardConfig) -> bool:
    """黑名单加强面：blocked_hosts 等于或任意级子域命中；blocked_host_patterns 对
    归一化 host 做 search（deny 侧宁可多拦——运营正则自带锚定自由度）。"""
    for entry in config.blocked_hosts:
        entry = entry.lower().rstrip(".")
        if host == entry or host.endswith("." + entry):
            return True
    return any(pattern.search(host) for pattern in config.blocked_host_patterns)


def _host_reserved(host: str) -> bool:
    """保留主机名：localhost / 保留后缀 / 无点短主机名（Docker 容器名——compose
    网内容器可被 `http://litellm:4000` 形态直达，短主机名一律拒）。"""
    return (
        host == "localhost"
        or host.endswith(_FORBIDDEN_HOST_SUFFIXES)
        or "." not in host
    )


def _check_dns(host: str, resolver: Resolver) -> UrlGuardResult:
    """DNS 面：任一解析结果命中类别 → 拒；空表/gaierror（NXDOMAIN）/**UnicodeError**
    （host 形态非法信号——空段/超长 label 的 IDNA 编码失败，平台相关形态，预检外的
    防线）/ **超时类（TimeoutError）→ 拒（fail-closed——超时可被攻击者廉价诱导：
    护栏侧首解拖时、抓取侧快解回恶意 IP，比 R21 rebinding 更廉价不配 fail-open）**；
    其余基础设施异常（网络不可达等 OSError 族）fail-open 放行 + 告警（安全检查不挡
    检索，同 safety_check 直通门语义）。契约面注记：解析结果全为非 IP 串时逐条跳过
    =放行（getaddrinfo 返回形态下不可达——假 resolver 语义，测试已钉死防漂移）。"""
    try:
        addresses = resolver(host)
    except socket.gaierror:
        return UrlGuardResult(ok=False, reason="dns_unresolved")
    except UnicodeError as exc:
        # host 形态非法信号（空 label/超长 label 的 IDNA 编码失败）而非基础设施故障
        # ——与 _parse_dotted_ipv4 空段检测同语义拒收（评审 P2：平台行为不漂移）
        _warn(
            f"url_guard DNS 解析 UnicodeError（host 形态非法，拒收）：{type(exc).__name__}"
            f"（host={host}）"
        )
        return UrlGuardResult(ok=False, reason="malformed_url")
    except TimeoutError:
        # 超时类 fail-closed（评审 P2：可被廉价诱导，不配 fail-open）
        return UrlGuardResult(ok=False, reason="dns_unresolved")
    except Exception as exc:  # noqa: BLE001 —— DNS 基础设施故障不挡检索（fail-open）
        _warn(
            f"url_guard DNS 解析异常（fail-open 放行）：{type(exc).__name__}（host={host}）"
        )
        return UrlGuardResult(ok=True, reason=None)
    if not addresses:
        return UrlGuardResult(ok=False, reason="dns_unresolved")
    for address in addresses:
        try:
            ip = ipaddress.ip_address(address)
        except ValueError:
            continue  # 非 IP 形态解析结果无法判定 → 跳过该条（抓取面自行失败）
        if _ip_forbidden(ip):
            return UrlGuardResult(ok=False, reason="ip_forbidden")
    return UrlGuardResult(ok=True, reason=None)


def validate_url(
    url: str,
    config: UrlGuardConfig,
    *,
    resolver: Resolver | None = None,
) -> UrlGuardResult:
    """SSRF 防护 URL 校验（同步纯函数；DNS 经注入式 resolver——unit 零网络）。

    config 为 UrlGuardConfig（loader 层产物；黑名单只加强不削弱代码硬底线）。
    返回 UrlGuardResult(ok, reason)：reason ∈ URL_REJECT_REASONS 固定枚举、
    零 payload 回显。async 调用点须 `asyncio.to_thread + wait_for` 包络
    （DNS/正则不阻塞事件循环，超预算 fail-open 由调用点裁定）。
    """
    url = "" if url is None else str(url)
    if len(url) > config.max_url_length:
        return UrlGuardResult(ok=False, reason="url_too_long")
    try:
        parts = urlsplit(url)
    except ValueError:
        return UrlGuardResult(ok=False, reason="malformed_url")
    scheme = parts.scheme.lower()
    if scheme not in _ALLOWED_SCHEMES:
        return UrlGuardResult(ok=False, reason="scheme_forbidden")
    if parts.username is not None or parts.password is not None:
        return UrlGuardResult(ok=False, reason="userinfo_forbidden")
    raw_host = parts.hostname
    if not raw_host:
        return UrlGuardResult(ok=False, reason="malformed_url")
    host = _normalize_host(raw_host)
    if not host:
        return UrlGuardResult(ok=False, reason="malformed_url")
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in host) or "/" in host or "@" in host:
        # 控制字符 / 解码出路径分隔或 userinfo 分隔——归一化后的 host 与客户端权威
        # 解析必歧义（%2F/%40 走私面），宁可拒收
        return UrlGuardResult(ok=False, reason="malformed_url")
    labels = host.split(".")
    if any(label == "" for label in labels) or any(len(label) > 63 for label in labels):
        # 空 label（127..0.0.1 形态）/ 超 63 字符 label（DNS 协议上限）——host 形态
        # 非法（评审 P2：此类 host 在 macOS/BSD 下 getaddrinfo 抛 UnicodeError、
        # Linux glibc 抛 gaierror，fail-open 路径会造成平台行为漂移；解析歧义面
        # 宁可拒收——平台无关预检，不进 DNS 面）
        return UrlGuardResult(ok=False, reason="malformed_url")
    if not host.isascii():
        # NFKC 后仍非 ASCII（IDN 裸形态）——客户端 punycode 行为不确定，保守拒收
        return UrlGuardResult(ok=False, reason="host_forbidden")
    if _host_blocked(host, config):
        return UrlGuardResult(ok=False, reason="host_forbidden")
    literal = _parse_literal_ip(host)
    if literal is not None:
        if _ip_forbidden(literal):
            return UrlGuardResult(ok=False, reason="ip_forbidden")
        return UrlGuardResult(ok=True, reason=None)  # 公网字面 IP 直判放行（零 DNS）
    if ":" in host:
        # 含冒号又非合法 IPv6 字面量（主机名不允许冒号）→ malformed
        return UrlGuardResult(ok=False, reason="malformed_url")
    if _host_reserved(host):
        return UrlGuardResult(ok=False, reason="host_forbidden")
    return _check_dns(host, resolver if resolver is not None else _socket_resolver)
