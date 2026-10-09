"""Settings from environment variables and the account registry from YAML."""

from __future__ import annotations

import fnmatch
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import yaml

Mode = Literal["readonly", "readwrite"]
Security = Literal["tls", "starttls"]

_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_ALLOWED_KEYS = {
    "id",
    "label",
    "host",
    "port",
    "username",
    "password_env",
    "mode",
    "security",
    "tls_verify",
    "folders_allow",
    "folders_deny",
}


class ConfigError(Exception):
    """Raised when the configuration is invalid; the server refuses to start."""


@dataclass(frozen=True)
class Account:
    """One IMAP mailbox. Only `id`, `label` and `mode` are ever shown to the LLM."""

    id: str
    label: str
    host: str
    port: int
    username: str
    password: str = field(repr=False)
    mode: Mode = "readonly"
    security: Security = "tls"
    tls_verify: bool = True
    folders_allow: tuple[str, ...] = ()
    folders_deny: tuple[str, ...] = ()

    @property
    def writable(self) -> bool:
        return self.mode == "readwrite"

    def folder_allowed(self, folder: str) -> bool:
        """Deny always wins over allow; an empty allow list permits every folder."""
        if any(_folder_match(folder, pattern) for pattern in self.folders_deny):
            return False
        if not self.folders_allow:
            return True
        return any(_folder_match(folder, pattern) for pattern in self.folders_allow)

    def public(self) -> dict[str, str]:
        return {"id": self.id, "label": self.label, "mode": self.mode}


def _folder_match(folder: str, pattern: str) -> bool:
    # INBOX is case-insensitive per RFC 3501, everything else is matched as written.
    if folder.upper() == "INBOX" or pattern.upper() == "INBOX":
        return folder.upper() == pattern.upper()
    return fnmatch.fnmatchcase(folder, pattern)


@dataclass(frozen=True)
class Settings:
    host: str = "0.0.0.0"
    port: int = 8000
    api_keys: str = ""
    accounts_file: str = "/config/accounts.yaml"
    max_body_chars: int = 20_000
    max_attachment_mb: int = 10
    log_level: str = "INFO"
    allowed_hosts: tuple[str, ...] = ()

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> Settings:
        env = dict(os.environ if env is None else env)
        try:
            return cls(
                host=env.get("MCP_HOST", cls.host),
                port=int(env.get("MCP_PORT", cls.port)),
                api_keys=env.get("MCP_API_KEYS", ""),
                accounts_file=env.get("ACCOUNTS_FILE", cls.accounts_file),
                max_body_chars=_positive_int(env, "MAX_BODY_CHARS", cls.max_body_chars),
                max_attachment_mb=_positive_int(env, "MAX_ATTACHMENT_MB", cls.max_attachment_mb),
                log_level=env.get("LOG_LEVEL", cls.log_level).upper(),
                allowed_hosts=tuple(h.strip() for h in env.get("MCP_ALLOWED_HOSTS", "").split(",") if h.strip()),
            )
        except ValueError as exc:
            raise ConfigError(str(exc)) from exc


def _positive_int(env: dict[str, str], name: str, default: int) -> int:
    raw = env.get(name)
    if raw is None or raw == "":
        return default
    value = int(raw)
    if value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def load_accounts(path: str | Path, env: dict[str, str] | None = None) -> dict[str, Account]:
    """Load and validate the account file. Passwords are resolved from `env` via `password_env`."""
    env = dict(os.environ if env is None else env)
    try:
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"Account file not found: {path}") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"Account file is not valid YAML: {path}") from exc

    if not isinstance(raw, dict) or not isinstance(raw.get("accounts"), list) or not raw["accounts"]:
        raise ConfigError("Account file must contain a non-empty 'accounts' list")

    accounts: dict[str, Account] = {}
    for index, entry in enumerate(raw["accounts"]):
        account = _parse_account(entry, index, env)
        if account.id in accounts:
            raise ConfigError(f"Duplicate account id '{account.id}'")
        accounts[account.id] = account
    return accounts


def _parse_account(entry: object, index: int, env: dict[str, str]) -> Account:
    where = f"accounts[{index}]"
    if not isinstance(entry, dict):
        raise ConfigError(f"{where} must be a mapping")
    unknown = set(entry) - _ALLOWED_KEYS
    if unknown:
        raise ConfigError(f"{where}: unknown keys {sorted(unknown)}")
    for key in ("id", "host", "username", "password_env"):
        if not isinstance(entry.get(key), str) or not entry[key].strip():
            raise ConfigError(f"{where}: '{key}' is required")

    account_id = entry["id"]
    if not _ID_PATTERN.match(account_id):
        raise ConfigError(f"{where}: id '{account_id}' must match {_ID_PATTERN.pattern}")
    where = f"account '{account_id}'"

    password_env = entry["password_env"]
    password = _resolve_secret(env, password_env)
    if not password:
        raise ConfigError(f"{where}: environment variable {password_env} (or {password_env}_FILE) is not set")

    mode = entry.get("mode", "readonly")
    if mode not in ("readonly", "readwrite"):
        raise ConfigError(f"{where}: mode must be 'readonly' or 'readwrite'")

    security = entry.get("security", "tls")
    if security not in ("tls", "starttls"):
        raise ConfigError(f"{where}: security must be 'tls' or 'starttls' (plaintext is not supported)")

    port = entry.get("port", 993 if security == "tls" else 143)
    if not isinstance(port, int) or not 0 < port < 65536:
        raise ConfigError(f"{where}: port must be an integer between 1 and 65535")

    tls_verify = entry.get("tls_verify", True)
    if not isinstance(tls_verify, bool):
        raise ConfigError(f"{where}: tls_verify must be true or false")

    return Account(
        id=account_id,
        label=str(entry.get("label") or account_id),
        host=entry["host"],
        port=port,
        username=entry["username"],
        password=password,
        mode=mode,
        security=security,
        tls_verify=tls_verify,
        folders_allow=_patterns(entry, "folders_allow", where),
        folders_deny=_patterns(entry, "folders_deny", where),
    )


def _resolve_secret(env: dict[str, str], name: str) -> str | None:
    """Read `NAME` directly, or the file named by `NAME_FILE` (Docker secrets)."""
    if env.get(name):
        return env[name]
    secret_file = env.get(f"{name}_FILE")
    if not secret_file:
        return None
    try:
        return Path(secret_file).read_text(encoding="utf-8").rstrip("\r\n") or None
    except OSError as exc:
        raise ConfigError(f"Cannot read secret file referenced by {name}_FILE") from exc


def _patterns(entry: dict, key: str, where: str) -> tuple[str, ...]:
    value = entry.get(key) or []
    if not isinstance(value, list) or not all(isinstance(p, str) and p for p in value):
        raise ConfigError(f"{where}: {key} must be a list of non-empty strings")
    return tuple(value)
