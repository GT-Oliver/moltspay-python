"""Validated origins and network policy for untrusted MoltsPay providers.

Provider URLs are attacker-controlled input.  This module intentionally keeps
the policy independent from the payment protocol so discovery, challenges,
and paid execution all use the same boundary.
"""

from __future__ import annotations

import ipaddress
import os
import socket
import threading
from dataclasses import dataclass
from typing import Callable, Iterable, Optional, Sequence
from urllib.parse import SplitResult, urljoin, urlsplit, urlunsplit

import httpx


class ProviderURLPolicyError(ValueError):
    """Raised when a provider URL or resolved destination violates policy."""


Resolver = Callable[[str, int], Iterable[str]]


def _default_resolver(host: str, port: int) -> Iterable[str]:
    """Resolve a host without consulting proxy configuration."""
    results = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return {str(item[4][0]) for item in results if item[4]}


def _is_blocked_address(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value)
    except ValueError as exc:
        raise ProviderURLPolicyError(f"Provider DNS returned an invalid address: {value!r}") from exc
    return any(
        (
            address.is_loopback,
            address.is_private,
            address.is_link_local,
            address.is_multicast,
            address.is_reserved,
            address.is_unspecified,
            getattr(address, "is_site_local", False),
        )
    )


def _host_is_loopback(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host == "localhost" or host.endswith(".localhost")


@dataclass(frozen=True)
class ProviderOrigin:
    """A normalized provider base URL and its immutable URL identity.

    ``resolver`` is deliberately injectable for deterministic tests.  In
    production it is the system resolver, and it is invoked for every request
    rather than only when the origin is first created.
    """

    scheme: str
    hostname: str
    port: int
    base_path: str = ""
    resolver: Resolver = _default_resolver
    allow_test_loopback: bool = False

    @classmethod
    def from_url(
        cls,
        value: str,
        *,
        resolver: Optional[Resolver] = None,
        allow_test_loopback: bool = False,
    ) -> "ProviderOrigin":
        parsed = _parse_provider_url(value)
        host = _normalize_host(parsed)
        scheme = parsed.scheme.lower()
        port = parsed.port or (443 if scheme == "https" else 80)
        origin = cls(
            scheme=scheme,
            hostname=host,
            port=port,
            base_path=parsed.path.rstrip("/"),
            resolver=resolver or _default_resolver,
            allow_test_loopback=allow_test_loopback,
        )
        origin._validate_scheme_and_host()
        origin.resolve_and_validate()
        return origin

    @property
    def base_url(self) -> str:
        host = f"[{self.hostname}]" if ":" in self.hostname else self.hostname
        default_port = self.port == (443 if self.scheme == "https" else 80)
        netloc = host if default_port else f"{host}:{self.port}"
        return urlunsplit((self.scheme, netloc, self.base_path, "", ""))

    @property
    def origin_key(self) -> tuple[str, str, int]:
        return self.scheme, self.hostname, self.port

    def _validate_scheme_and_host(self) -> None:
        if self.scheme not in {"http", "https"}:
            raise ProviderURLPolicyError("Provider URLs must use HTTP or HTTPS")
        if self.scheme == "http":
            return
        if self.hostname == "localhost" or self.hostname.endswith(".localhost"):
            if not self.allow_test_loopback:
                raise ProviderURLPolicyError("localhost provider URLs are not allowed")
        try:
            address = ipaddress.ip_address(self.hostname)
        except ValueError:
            return
        if _is_blocked_address(self.hostname) and not self.allow_test_loopback:
            raise ProviderURLPolicyError("Provider URL resolves to a prohibited IP address")
        if self.allow_test_loopback and not address.is_loopback:
            raise ProviderURLPolicyError("The test-only exception is limited to loopback")

    def resolve_and_validate(self) -> str:
        """Resolve now and reject every unsafe literal/DNS result."""
        self._validate_scheme_and_host()
        addresses = [str(value) for value in self.resolver(self.hostname, self.port)]
        if not addresses:
            raise ProviderURLPolicyError("Provider hostname did not resolve")
        if self.scheme == "http":
            return addresses[0]
        unsafe = [value for value in addresses if _is_blocked_address(value)]
        if unsafe:
            if not (self.allow_test_loopback and all(_host_is_loopback(value) for value in addresses)):
                raise ProviderURLPolicyError("Provider DNS resolves to a prohibited IP address")
        if self.allow_test_loopback and not all(_host_is_loopback(value) for value in addresses):
            raise ProviderURLPolicyError("The test-only exception is limited to loopback")
        return addresses[0]

    def validate_request_url(self, value: str, *, resolve: bool = True) -> str:
        """Validate a request URL is same-origin and optionally resolve it."""
        parsed = _parse_provider_url(value, allow_query=True)
        candidate = ProviderOrigin(
            scheme=parsed.scheme.lower(),
            hostname=_normalize_host(parsed),
            port=parsed.port or (443 if parsed.scheme.lower() == "https" else 80),
            base_path=parsed.path.rstrip("/"),
            resolver=self.resolver,
            allow_test_loopback=self.allow_test_loopback,
        )
        candidate._validate_scheme_and_host()
        if candidate.origin_key != self.origin_key:
            raise ProviderURLPolicyError("Provider request changed origin")
        if resolve:
            candidate.resolve_and_validate()
        return value

    def validate_redirect(self, request_url: str, location: str) -> str:
        """Reject redirects unless they are same-origin HTTP(S) URLs.

        The current client does not follow redirects.  Validating the target
        before reporting it gives callers a precise policy error and prevents a
        future caller from accidentally following a cross-origin redirect or
        HTTPS downgrade.
        """
        target = urljoin(request_url, location)
        self.validate_request_url(target, resolve=True)
        if self.scheme == "https" and urlsplit(target).scheme.lower() != "https":
            raise ProviderURLPolicyError("Provider redirects may not downgrade from HTTPS")
        raise ProviderURLPolicyError("Provider redirects are not followed")

    def validate_response(self, request_url: str, response: httpx.Response) -> None:
        if 300 <= response.status_code < 400:
            location = response.headers.get("location")
            if not location:
                raise ProviderURLPolicyError("Provider returned a redirect without a location")
            self.validate_redirect(request_url, location)


def _parse_provider_url(value: str, *, allow_query: bool = False) -> SplitResult:
    if not isinstance(value, str) or not value or len(value) > 2048 or any(char.isspace() for char in value):
        raise ProviderURLPolicyError("Provider URL is invalid")
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise ProviderURLPolicyError("Provider URL is invalid") from exc
    if not parsed.scheme or not hostname or parsed.username is not None or parsed.password is not None:
        raise ProviderURLPolicyError("Provider URL must contain only a scheme, host, port, and path")
    if parsed.fragment or (parsed.query and not allow_query):
        raise ProviderURLPolicyError("Provider base URLs may not contain a query or fragment")
    if parsed.scheme.lower() not in {"http", "https"} or port is None and ":" in parsed.netloc and not parsed.netloc.endswith("]"):
        raise ProviderURLPolicyError("Provider URL has an unsupported scheme or port")
    return parsed


def _normalize_host(parsed: SplitResult) -> str:
    try:
        return (parsed.hostname or "").rstrip(".").lower().encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise ProviderURLPolicyError("Provider hostname is invalid") from exc


class _PinnedSyncBackend:
    """httpcore backend that connects to the address validated for this call."""

    def __init__(self) -> None:
        import httpcore

        self._backend = httpcore.SyncBackend()
        self._local = threading.local()

    def pin(self, host: str, port: int, address: str) -> None:
        self._local.endpoint = (host, port, address)

    def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        endpoint = getattr(self._local, "endpoint", None)
        target = endpoint[2] if endpoint and endpoint[:2] == (host, port) else host
        return self._backend.connect_tcp(target, port, timeout, local_address, socket_options)

    def connect_unix_socket(self, *args, **kwargs):
        return self._backend.connect_unix_socket(*args, **kwargs)

    def sleep(self, seconds):
        return self._backend.sleep(seconds)


class _PinnedAsyncBackend:
    """Async equivalent of :class:`_PinnedSyncBackend`."""

    def __init__(self) -> None:
        from httpcore._backends.auto import AutoBackend

        self._backend = AutoBackend()
        self._endpoint = None

    def pin(self, host: str, port: int, address: str) -> None:
        self._endpoint = (host, port, address)

    async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        target = self._endpoint[2] if self._endpoint and self._endpoint[:2] == (host, port) else host
        return await self._backend.connect_tcp(target, port, timeout, local_address, socket_options)

    async def connect_unix_socket(self, *args, **kwargs):
        return await self._backend.connect_unix_socket(*args, **kwargs)

    async def sleep(self, seconds):
        return await self._backend.sleep(seconds)


class ProviderHTTPTransport(httpx.HTTPTransport):
    """HTTP transport with no env proxy and DNS-pinned connections."""

    def __init__(self, policy: Optional[ProviderOrigin] = None) -> None:
        super().__init__(trust_env=False, proxy=None)
        self._policy = policy
        self._backend = _PinnedSyncBackend()
        self._pool._network_backend = self._backend

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        if self._policy is not None:
            address = self._policy.resolve_and_validate()
        else:
            origin = ProviderOrigin.from_url(str(request.url), resolver=_default_resolver)
            address = origin.resolve_and_validate()
        self._backend.pin(request.url.host, request.url.port, address)
        request.extensions["sni_hostname"] = request.url.host
        return super().handle_request(request)


class ProviderAsyncHTTPTransport(httpx.AsyncHTTPTransport):
    """Async HTTP transport with no env proxy and DNS-pinned connections."""

    def __init__(self, policy: Optional[ProviderOrigin] = None) -> None:
        super().__init__(trust_env=False, proxy=None)
        self._policy = policy
        self._backend = _PinnedAsyncBackend()
        self._pool._network_backend = self._backend

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if self._policy is not None:
            address = self._policy.resolve_and_validate()
        else:
            origin = ProviderOrigin.from_url(str(request.url), resolver=_default_resolver)
            address = origin.resolve_and_validate()
        self._backend.pin(request.url.host, request.url.port, address)
        request.extensions["sni_hostname"] = request.url.host
        return await super().handle_async_request(request)


__all__ = [
    "ProviderOrigin",
    "ProviderURLPolicyError",
    "ProviderHTTPTransport",
    "ProviderAsyncHTTPTransport",
]
