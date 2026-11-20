"""unit 层：safety.url_guard——SSRF 防护 URL 校验器（B15 内容侧安全护栏，纯函数零网络）。

钉住的语义（nacos-config.md §3 B15 / plan-b15 §1）：
- **硬规则（代码常量，配置只能加强不能削弱）**：scheme ∈ {http,https}（大小写不敏感）；
  含 userinfo 的 URL 拒绝；超长拒（max_url_length）；host 归一化（percent-decode +
  NFKC 全角句点 + 小写 + 去尾点）后拒 localhost / *.localhost / *.local / *.internal /
  *.home.arpa / 无点短主机名（Docker 容器名）；
- **字面 IP**：先归一化混淆形态（十进制 2130706433 / 十六进制 0x7f000001 / 八进制
  0177.0.0.1 / 混合 0x7f.0.0.1 / 两段 127.1），再 ipaddress 判 loopback/private/
  link_local/reserved/multicast/unspecified + 显式元数据集 {169.254.169.254,
  100.100.100.200, fd00:ec2::254} + CGNAT 100.64.0.0/10（3.12 is_private 不含）；
  IPv6 v4-mapped（::ffff:0:0/96）回查 v4 规则；
- **DNS**：非字面 IP 主机经注入式 resolver 解析（unit 零网络），任一解析结果命中
  上述类别 → 拒（nip.io 形态 rebind 面）；NXDOMAIN（socket.gaierror）拒；resolver
  其他异常 fail-open 放行 + safety 域告警（host 只落日志、零完整 URL）；
- **结果契约**：UrlGuardResult(ok, reason)——reason ∈ URL_REJECT_REASONS 固定枚举、
  放行为 None；结果零 payload 回显（dataclass 仅 ok/reason 两字段，无 url/host）。

黑名单加强语义：blocked_hosts 按「等于或子域」匹配、blocked_host_patterns 按
search 匹配归一化 host——两者是运营加强面（deny 侧），代码硬底线不受其影响。
"""

import re
import socket
from dataclasses import fields
from typing import Any

import pytest

from app.agent.safety.url_guard import (
    URL_REJECT_REASONS,
    UrlGuardConfig,
    UrlGuardResult,
    validate_url,
)

#: 公网样例 IP（字面公网直判，不经 DNS）
_PUBLIC_IP = "93.184.216.34"


def _guard(**overrides: Any) -> UrlGuardConfig:
    values: dict[str, Any] = {
        "enabled": True,
        "blocked_hosts": (),
        "blocked_host_patterns": (),
        "max_url_length": 2048,
    }
    values.update(overrides)
    return UrlGuardConfig(**values)


def _resolver(*addresses: str):
    """注入式假 resolver：任意 host 返回给定地址表（unit 零网络）。"""

    def resolve(host: str) -> list[str]:
        return list(addresses)

    return resolve


def _nxdomain(host: str) -> list[str]:
    raise socket.gaierror(-2, "Name or service not known")


def _boom(host: str) -> list[str]:
    raise RuntimeError("resolver infra broken")


def _guard_warnings(log_records: list[Any]) -> list[str]:
    """safety 域 WARNING 消息（AgentLog extra.graph == "safety"）。"""
    return [
        r["message"]
        for r in log_records
        if r["level"].name == "WARNING" and r["extra"].get("graph") == "safety"
    ]


# ============================== scheme / 长 / userinfo ==============================


class TestSchemeRules:
    @pytest.mark.parametrize("scheme", ["http", "https", "HTTP", "HTTPS", "HtTpS"])
    def test_http_https_allowed_case_insensitive(self, scheme: str, log_records) -> None:
        """http/https（大小写不敏感）放行（公网字面 IP，零 DNS）。"""
        result = validate_url(f"{scheme}://{_PUBLIC_IP}/", _guard())
        assert result.ok is True
        assert result.reason is None
        assert _guard_warnings(log_records) == []

    @pytest.mark.parametrize(
        "url",
        [
            "ftp://example.com/",
            "file:///etc/passwd",
            "javascript:alert(1)",
            "data:text/html,x",
            "gopher://example.com/",
        ],
    )
    def test_non_http_scheme_rejected(self, url: str) -> None:
        assert validate_url(url, _guard()) == UrlGuardResult(False, "scheme_forbidden")

    def test_missing_scheme_rejected(self) -> None:
        """无 scheme（裸主机）→ scheme_forbidden（不是 malformed——scheme 门在前）。"""
        assert validate_url("example.com/x", _guard()).reason == "scheme_forbidden"


class TestLengthAndUserinfoRules:
    def test_url_over_max_length_rejected(self) -> None:
        url = f"http://{_PUBLIC_IP}/{'a' * 3000}"
        assert validate_url(url, _guard()) == UrlGuardResult(False, "url_too_long")

    def test_max_url_length_boundary_exact_allowed(self) -> None:
        """恰好等于上限放行、超一字拒（边界含端点）。"""
        path_a = "a" * 20
        url_ok = f"http://{_PUBLIC_IP}/{path_a}"
        guard = _guard(max_url_length=len(url_ok))
        assert validate_url(url_ok, guard).ok is True
        assert validate_url(url_ok + "b", guard).reason == "url_too_long"

    @pytest.mark.parametrize(
        "url",
        [
            f"http://user@{_PUBLIC_IP}/",
            f"http://user:pass@{_PUBLIC_IP}/",
            "http://user%2Fpass@example.com/",
        ],
    )
    def test_userinfo_rejected(self, url: str) -> None:
        assert validate_url(url, _guard()) == UrlGuardResult(False, "userinfo_forbidden")


# ============================== host 归一化与畸形面 ==============================


class TestHostNormalization:
    def test_fullwidth_period_host_rejected(self) -> None:
        """全角句点 NFKC 归一后命中回环（127。0。0。1 → 127.0.0.1）。"""
        assert validate_url("http://127。0。0。1/", _guard()) == UrlGuardResult(
            False, "ip_forbidden"
        )

    def test_fullwidth_digits_host_rejected(self) -> None:
        """全角数字 NFKC 归一后命中回环。"""
        assert validate_url("http://１２７。０。０。１/", _guard()).reason == "ip_forbidden"

    def test_percent_encoded_fullwidth_host_rejected(self) -> None:
        """percent-decode 先于 NFKC：%E3%80%82（。）解码后归一为句点。"""
        url = "http://127%E3%80%820%E3%80%820%E3%80%821/"
        assert validate_url(url, _guard()).reason == "ip_forbidden"

    def test_trailing_dot_stripped_then_literal_ip_judged(self) -> None:
        """尾点去除后按字面 IP 判定（公网字面 → 放行）。"""
        assert validate_url(f"http://{_PUBLIC_IP}./", _guard()).ok is True

    def test_uppercase_host_normalized_before_rules(self) -> None:
        """host 小写归一先于禁用规则（LOCALHOST 命中短主机名禁令）。"""
        assert validate_url("http://LOCALHOST/", _guard()).reason == "host_forbidden"

    def test_control_char_in_host_rejected(self) -> None:
        """%00 解码出控制字符 → malformed_url（不进 DNS 面）。"""
        assert validate_url("http://evil%00.example.com/", _guard()).reason == (
            "malformed_url"
        )

    def test_decoded_slash_or_at_in_host_rejected(self) -> None:
        """%2F/%40 解码出路径分隔或 @ → 拒（解析歧义面，不与客户端歧义共舞）。"""
        assert validate_url("http://a%2Fb.example.com/", _guard()).reason == (
            "malformed_url"
        )
        assert validate_url("http://a%40b.example.com/", _guard()).reason == (
            "malformed_url"
        )

    def test_empty_host_malformed(self) -> None:
        assert validate_url("http://", _guard()).reason == "malformed_url"

    def test_malformed_ipv6_bracket(self) -> None:
        """非法 IPv6 括号形态 → malformed_url（urlsplit ValueError 收口）。"""
        assert validate_url("http://[::1", _guard()).reason == "malformed_url"

    def test_non_ascii_host_after_nfkc_rejected(self) -> None:
        """NFKC 后仍含非 ASCII（IDN 裸形态）→ host_forbidden（客户端 punycode
        行为不确定，宁可拒收——B15 保守口径）。"""
        assert validate_url("http://münchen.de/", _guard()).reason == "host_forbidden"

    def test_empty_label_host_rejected(self) -> None:
        """评审 P2（双评审轮）：空 label 混淆形态（127..0.0.1）→ malformed_url——
        平台无关预检（macOS/BSD 下 getaddrinfo 会抛 UnicodeError 落入 fail-open
        放行、Linux glibc 抛 gaierror 落入拒收——行为不得随平台漂移，本测在 BSD
        机器上即回归护栏）。"""
        assert validate_url("http://127..0.0.1/", _guard()) == UrlGuardResult(
            False, "malformed_url"
        )
        assert validate_url("http://..example.com/", _guard()).reason == "malformed_url"
        assert validate_url("http://a..b/", _guard()).reason == "malformed_url"

    def test_overlong_label_host_rejected(self) -> None:
        """单 label 超 63 字符（DNS 协议上限）→ malformed_url（与空段同判据——
        IDNA 编码面 UnicodeError 的平台无关预检）。"""
        url = "http://" + "a" * 64 + ".example.com/"
        assert validate_url(url, _guard()) == UrlGuardResult(False, "malformed_url")


# ============================== 禁用主机名与黑名单加强 ==============================


class TestForbiddenHosts:
    @pytest.mark.parametrize(
        "host",
        [
            "localhost",
            "sub.localhost",
            "a.local",
            "deep.b.local",
            "svc.internal",
            "x.home.arpa",
            "mycontainer",  # 无点短主机名（Docker 容器名面）
        ],
    )
    def test_forbidden_hostnames_rejected(self, host: str) -> None:
        assert validate_url(f"http://{host}/", _guard()) == UrlGuardResult(
            False, "host_forbidden"
        )

    def test_blocked_hosts_exact_and_subdomain(self) -> None:
        """blocked_hosts：精确等于或任意级子域命中；前缀同串非子域不误伤。"""
        guard = _guard(blocked_hosts=("evil.example",))
        assert validate_url("http://evil.example/", guard).reason == "host_forbidden"
        assert validate_url("http://sub.evil.example/", guard).reason == "host_forbidden"
        assert validate_url("http://deep.sub.evil.example/", guard).reason == (
            "host_forbidden"
        )
        # "notevil.example" 不是 evil.example 的子域（endswith(".evil.example") 不命中）
        result = validate_url(
            "http://notevil.example/", guard, resolver=_resolver(_PUBLIC_IP)
        )
        assert result.ok is True

    def test_blocked_host_pattern_search_match(self) -> None:
        """blocked_host_patterns 对归一化 host 做 search 匹配（deny 侧加强）。"""
        guard = _guard(blocked_host_patterns=(re.compile(r"\.internal\.corp$"),))
        assert validate_url("http://x.internal.corp/", guard).reason == "host_forbidden"
        result = validate_url(
            "http://x.example.com/", guard, resolver=_resolver(_PUBLIC_IP)
        )
        assert result.ok is True


# ============================== 字面 IP（含混淆形态） ==============================


class TestLiteralIpRules:
    @pytest.mark.parametrize(
        "host",
        [
            "127.0.0.1",
            "127.1",  # 两段形态 → 127.0.0.1
            "0.0.0.0",  # unspecified
            "[::1]",
            "[::ffff:127.0.0.1]",  # v4-mapped 回查 v4
        ],
    )
    def test_loopback_forms_rejected(self, host: str) -> None:
        assert validate_url(f"http://{host}/", _guard()).reason == "ip_forbidden"

    def test_private_boundary_precise(self) -> None:
        """私网边界精确：172.15 放行 / 172.16 拒（is_private 不含 172.15/12 前段）。"""
        assert validate_url(f"http://172.15.255.255/", _guard()).ok is True
        assert validate_url("http://172.16.0.1/", _guard()).reason == "ip_forbidden"
        assert validate_url("http://10.0.0.1/", _guard()).reason == "ip_forbidden"
        assert validate_url("http://192.168.1.1/", _guard()).reason == "ip_forbidden"

    @pytest.mark.parametrize(
        "host",
        [
            "169.254.169.254",  # 云元数据（is_link_local 之外显式集合兜 stdlib 演进）
            "100.100.100.200",  # 阿里云元数据（CGNAT 段内）
            "[fd00:ec2::254]",  # IMDSv6
        ],
    )
    def test_metadata_endpoints_rejected(self, host: str) -> None:
        assert validate_url(f"http://{host}/", _guard()).reason == "ip_forbidden"

    def test_cgnat_range_rejected_with_boundary(self) -> None:
        """CGNAT 100.64.0.0/10 拒（3.12 is_private 不含——显式检查）；100.128/12 外放行。"""
        assert validate_url("http://100.64.0.1/", _guard()).reason == "ip_forbidden"
        assert validate_url("http://100.127.255.255/", _guard()).reason == "ip_forbidden"
        assert validate_url("http://100.128.0.1/", _guard()).ok is True

    @pytest.mark.parametrize(
        "host",
        [
            "2130706433",  # 十进制 → 127.0.0.1
            "0x7f000001",  # 十六进制
            "0177.0.0.1",  # 八进制段
            "0x7f.0.0.1",  # 混合十六进制段
            "127.0x0.0.1",  # 混合（部分段十六进制）
        ],
    )
    def test_obfuscated_ipv4_forms_rejected(self, host: str) -> None:
        assert validate_url(f"http://{host}/", _guard()).reason == "ip_forbidden"

    @pytest.mark.parametrize("host", ["[fd00::1]", "[fe80::1]", "[ff02::1]", "[::]"])
    def test_ipv6_ula_linklocal_multicast_unspecified_rejected(self, host: str) -> None:
        assert validate_url(f"http://{host}/", _guard()).reason == "ip_forbidden"


# ============================== DNS 解析面（注入式 resolver） ==============================


class TestDnsRules:
    def test_resolved_private_ip_rejected(self) -> None:
        """解析结果任一命中私网即拒（多地址一私全拒）。"""
        result = validate_url(
            "http://internal.example.com/", _guard(), resolver=_resolver("10.0.0.5")
        )
        assert result == UrlGuardResult(False, "ip_forbidden")

    def test_nip_io_style_rebind_rejected(self) -> None:
        """nip.io 形态 rebind：公网样域名解析到回环 → 拒。"""
        result = validate_url(
            "http://metadata.nip.io/", _guard(), resolver=_resolver("127.0.0.1")
        )
        assert result.reason == "ip_forbidden"

    def test_public_resolution_passes(self) -> None:
        result = validate_url(
            "http://example.com/", _guard(), resolver=_resolver(_PUBLIC_IP)
        )
        assert result == UrlGuardResult(True, None)

    def test_multi_address_one_private_rejected(self) -> None:
        result = validate_url(
            "http://mixed.example.com/",
            _guard(),
            resolver=_resolver(_PUBLIC_IP, "192.168.0.9"),
        )
        assert result.reason == "ip_forbidden"

    def test_nxdomain_rejected(self) -> None:
        """NXDOMAIN（socket.gaierror）→ dns_unresolved 拒。"""
        result = validate_url("http://no-such.example/", _guard(), resolver=_nxdomain)
        assert result == UrlGuardResult(False, "dns_unresolved")

    def test_empty_resolution_rejected(self) -> None:
        """解析返回空表 = 无地址可判 → 按 NXDOMAIN 同语义拒。"""
        assert validate_url("http://void.example/", _guard(), resolver=_resolver()).reason == (
            "dns_unresolved"
        )

    def test_resolver_crash_fails_open_with_warning(
        self, log_records: list[Any]
    ) -> None:
        """resolver 基础设施异常（非 gaierror、非超时类）→ fail-open 放行 + safety
        域告警一次（基础设施故障不挡检索）。"""
        result = validate_url("http://example.com/", _guard(), resolver=_boom)
        assert result.ok is True
        warnings = _guard_warnings(log_records)
        assert len(warnings) == 1
        assert "fail-open" in warnings[0]

    def test_resolver_timeout_fail_closed(self, log_records: list[Any]) -> None:
        """评审 P2（双评审轮）：超时类可被攻击者廉价诱导（护栏侧首解 >3s、抓取侧
        快解回恶意 IP）——超时**不配 fail-open**，fail-closed 拒（dns_unresolved）。"""
        def slow_timeout(host: str) -> list[str]:
            raise TimeoutError("resolver timed out")

        result = validate_url("http://evil.example/", _guard(), resolver=slow_timeout)
        assert result == UrlGuardResult(False, "dns_unresolved")
        # 零 fail-open 告警（拒收不是基础设施故障——不刷 WARNING）
        assert _guard_warnings(log_records) == []

    def test_all_non_ip_resolution_contract_note(self) -> None:
        """解析结果全为非 IP 串 → 逐条跳过=放行（getaddrinfo 形态下不可达——契约
        面注记用例：假 resolver 语义下钉死当前行为防漂移）。"""
        result = validate_url(
            "http://weird.example.com/", _guard(), resolver=_resolver("hostname-txt")
        )
        assert result.ok is True

    def test_unparseable_resolved_address_skipped(self) -> None:
        """解析结果非 IP 形态（无法判定）→ 跳过该条（抓取面自行失败），不整体拒。"""
        result = validate_url(
            "http://weird.example.com/", _guard(), resolver=_resolver("not-an-ip")
        )
        assert result.ok is True


# ============================== 结果契约 ==============================


class TestResultContract:
    def test_reason_enum_stable(self) -> None:
        """reason 枚举固定（api.md errors[].reason 语义面——枚举漂移即契约破坏）。"""
        assert URL_REJECT_REASONS == (
            "malformed_url",
            "url_too_long",
            "scheme_forbidden",
            "userinfo_forbidden",
            "host_forbidden",
            "ip_forbidden",
            "dns_unresolved",
        )

    def test_result_carries_no_payload_echo(self) -> None:
        """结果零 payload 回显：dataclass 仅 ok/reason 两字段（零 url/host）。"""
        assert {f.name for f in fields(UrlGuardResult)} == {"ok", "reason"}
        bad = validate_url("http://169.254.169.254/secret-path?q=1", _guard())
        assert bad == UrlGuardResult(False, "ip_forbidden")

    def test_all_reject_reasons_within_enum(self) -> None:
        """各拒绝路径 reason 均落在固定枚举内（逐路抽一条）。"""
        reasons = {
            validate_url("ftp://x/", _guard()).reason,
            validate_url("http://user@x/", _guard()).reason,
            validate_url("http://x" + "a" * 3000, _guard()).reason,
            validate_url("http://localhost/", _guard()).reason,
            validate_url("http://127.0.0.1/", _guard()).reason,
            validate_url("http://no-such.example/", _guard(), resolver=_nxdomain).reason,
            validate_url("http://", _guard()).reason,
        }
        assert reasons <= set(URL_REJECT_REASONS)
        assert None not in reasons
