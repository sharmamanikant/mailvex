from __future__ import annotations

import ipaddress
import socket
from collections.abc import Callable, Sequence

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address

# Well-known service ports that are never legitimate SMTP destinations. Blocking
# these mitigates SSRF against internal/cloud metadata and datastore endpoints.
PORT_DENYLIST = {
    0,
    22, 23,
    135, 137, 138, 139,
    445,
    1433, 1521,
    3306, 3389, 5432,
    6379, 9200, 11211, 27017,
}

# Low (privileged) ports that are accepted as SMTP destinations by default.
ALLOWED_PRIVILEGED_SMTP_PORTS = {25, 465, 587}

TLS_MODES = {"TLS", "STARTTLS", "SSL"}


class SMTPPolicyViolation(ValueError):
    """Raised when an SMTP destination violates the sender security policy."""


Resolver = Callable[[str], Sequence[IPAddress]]


def _default_resolver(host: str) -> Sequence[IPAddress]:
    """Resolve every A/AAAA record for a host, rejecting DNS failures.

    Resolving all records (rather than the first) lets callers reject a
    destination if *any* answer points at an internal/private address, which
    mitigates DNS-rebinding and round-robin SSRF.
    """
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise SMTPPolicyViolation("SMTP hostname could not be resolved") from exc
    unique: list[IPAddress] = []
    for info in infos:
        try:
            ip = ipaddress.ip_address(str(info[4][0]))
        except ValueError:
            continue
        if ip not in unique:
            unique.append(ip)
    if not unique:
        raise SMTPPolicyViolation("SMTP hostname resolved to no usable addresses")
    return unique


def _is_private(ip: IPAddress) -> bool:
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


def resolve_and_validate(
    host: str,
    *,
    port: int | None,
    allow_private_hosts: bool = False,
    resolver: Resolver | None = None,
) -> None:
    """Resolve `host` and reject destinations that point at internal addresses.

    Always blocks private/loopback/link-local/reserved addresses unless
    `allow_private_hosts` is set (for a controlled internal environment).
    """
    resolve = resolver or _default_resolver
    addresses = resolve(str(host))
    if not allow_private_hosts:
        offending = [str(ip) for ip in addresses if _is_private(ip)]
        if offending:
            raise SMTPPolicyViolation("SMTP hostname resolves to a private or internal network address")
    if port is not None and not _port_allowed(int(port)):
        raise SMTPPolicyViolation("SMTP port is not permitted by the security policy")


def _port_allowed(port: int, *, allow_insecure_ports: bool = False) -> bool:
    if allow_insecure_ports:
        return port > 0
    if port in PORT_DENYLIST:
        return False
    if port < 1024 and port not in ALLOWED_PRIVILEGED_SMTP_PORTS:
        return False
    return True


def validate_destination(
    host: str,
    *,
    port: int | None,
    tls_mode: str | None,
    username: str | None,
    password: str | None,
    allow_private_hosts: bool = False,
    allow_plaintext_auth: bool = False,
    allow_insecure_ports: bool = False,
    resolver: Resolver | None = None,
) -> None:
    """Validate an SMTP destination against the sender security policy.

    Raises SMTPPolicyViolation if the destination is unsafe or authentication
    would be transmitted without TLS protection. Never accepts or returns the
    password.
    """
    resolve = resolver or _default_resolver
    addresses = resolve(str(host))
    if not allow_private_hosts:
        offending = [str(ip) for ip in addresses if _is_private(ip)]
        if offending:
            raise SMTPPolicyViolation("SMTP hostname resolves to a private or internal network address")

    if port is not None and not _port_allowed(int(port), allow_insecure_ports=allow_insecure_ports):
        raise SMTPPolicyViolation("SMTP port is not permitted by the security policy")

    authenticating = bool(username) and bool(password)
    if authenticating and tls_mode not in TLS_MODES and not allow_plaintext_auth:
        raise SMTPPolicyViolation("SMTP authentication requires a TLS-secured connection")
