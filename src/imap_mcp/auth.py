"""API-key parsing, constant-time key lookup and the ASGI middleware guarding /mcp."""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from starlette.types import ASGIApp, Receive, Scope, Send

from .config import ConfigError


@dataclass(frozen=True)
class ApiKey:
    digest: bytes
    # None means the key may see every account.
    accounts: frozenset[str] | None


class KeyRing:
    """Holds the configured keys. Syntax: `key1:support+info,key2` (scope accounts joined by '+')."""

    def __init__(self, keys: Iterable[ApiKey] = ()) -> None:
        self._keys = tuple(keys)

    @property
    def enabled(self) -> bool:
        return bool(self._keys)

    @classmethod
    def parse(cls, raw: str, known_accounts: Iterable[str]) -> KeyRing:
        known = set(known_accounts)
        keys: list[ApiKey] = []
        for item in (part.strip() for part in raw.split(",")):
            if not item:
                continue
            secret, sep, scope = item.partition(":")
            if not secret:
                raise ConfigError("MCP_API_KEYS contains an empty key")
            accounts: frozenset[str] | None = None
            if sep:
                accounts = frozenset(a.strip() for a in scope.split("+") if a.strip())
                if not accounts:
                    raise ConfigError("MCP_API_KEYS contains a key with an empty scope")
                unknown = accounts - known
                if unknown:
                    raise ConfigError(f"MCP_API_KEYS references unknown accounts {sorted(unknown)}")
            keys.append(ApiKey(digest=_digest(secret), accounts=accounts))
        return cls(keys)

    def lookup(self, token: str | None) -> ApiKey | None:
        """Compare against every key so the timing does not depend on which key matched."""
        if not token:
            return None
        candidate = _digest(token)
        found: ApiKey | None = None
        for key in self._keys:
            if hmac.compare_digest(candidate, key.digest):
                found = key
        return found

    def allowed_accounts(self, headers: Mapping[str, str] | None) -> frozenset[str] | None:
        """Account scope for a request: None = unrestricted, empty set = nothing."""
        if not self.enabled:
            return None
        key = self.lookup(extract_token(headers or {}))
        if key is None:
            return frozenset()
        return key.accounts


def _digest(value: str) -> bytes:
    return hashlib.sha256(value.encode("utf-8")).digest()


def extract_token(headers: Mapping[str, str]) -> str | None:
    lowered = {k.lower(): v for k, v in headers.items()}
    auth = lowered.get("authorization", "")
    scheme, _, value = auth.partition(" ")
    if scheme.lower() == "bearer" and value.strip():
        return value.strip()
    api_key = lowered.get("x-api-key", "").strip()
    return api_key or None


class ApiKeyMiddleware:
    """Rejects requests to protected paths without a valid key; other paths pass through."""

    def __init__(self, app: ASGIApp, keyring: KeyRing, protected_prefix: str = "/mcp") -> None:
        self.app = app
        self.keyring = keyring
        self.protected_prefix = protected_prefix

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and self.keyring.enabled and self._protected(scope.get("path", "")):
            headers = {k.decode("latin-1"): v.decode("latin-1") for k, v in scope.get("headers", [])}
            if self.keyring.lookup(extract_token(headers)) is None:
                await _unauthorized(send)
                return
        await self.app(scope, receive, send)

    def _protected(self, path: str) -> bool:
        return path == self.protected_prefix or path.startswith(self.protected_prefix + "/")


async def _unauthorized(send: Send) -> None:
    body = json.dumps({"error": "unauthorized"}).encode()
    await send(
        {
            "type": "http.response.start",
            "status": 401,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
                (b"www-authenticate", b'Bearer realm="imap-mcp"'),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})
