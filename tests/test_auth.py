import pytest

from imap_mcp.auth import KeyRing, extract_token
from imap_mcp.config import ConfigError


def test_parse_keys_with_and_without_scope():
    ring = KeyRing.parse("alpha, beta:support+info ,gamma:info", ["support", "info"])
    assert ring.enabled
    assert ring.lookup("alpha").accounts is None
    assert ring.lookup("beta").accounts == {"support", "info"}
    assert ring.lookup("gamma").accounts == {"info"}
    assert ring.lookup("delta") is None
    assert ring.lookup("") is None


def test_empty_config_disables_auth():
    ring = KeyRing.parse("", ["support"])
    assert not ring.enabled
    assert ring.allowed_accounts({}) is None


def test_unknown_scope_account_fails():
    with pytest.raises(ConfigError, match="unknown accounts"):
        KeyRing.parse("k:nope", ["support"])
    with pytest.raises(ConfigError, match="empty scope"):
        KeyRing.parse("k:", ["support"])


def test_allowed_accounts_by_header():
    ring = KeyRing.parse("alpha,beta:info", ["support", "info"])
    assert ring.allowed_accounts({"Authorization": "Bearer alpha"}) is None
    assert ring.allowed_accounts({"x-api-key": "beta"}) == {"info"}
    assert ring.allowed_accounts({"Authorization": "Bearer wrong"}) == frozenset()
    assert ring.allowed_accounts(None) == frozenset()


def test_extract_token():
    assert extract_token({"authorization": "bearer abc"}) == "abc"
    assert extract_token({"X-API-Key": " k "}) == "k"
    assert extract_token({"authorization": "Basic abc"}) is None
    assert extract_token({}) is None
