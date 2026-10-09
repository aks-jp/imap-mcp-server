from pathlib import Path

import pytest

from imap_mcp.config import ConfigError, Settings, load_accounts

VALID = """
accounts:
  - id: support
    label: Support
    host: imap.example.com
    username: support@example.com
    password_env: MAIL_SUPPORT_PASSWORD
    folders_allow: ["INBOX", "Archiv/*"]
    folders_deny: ["Archiv/Geheim", "Junk"]
  - id: info
    host: imap.example.org
    username: info@example.org
    password_env: MAIL_INFO_PASSWORD
    mode: readwrite
    security: starttls
"""
ENV = {"MAIL_SUPPORT_PASSWORD": "pw1", "MAIL_INFO_PASSWORD": "pw2"}


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "accounts.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_loads_valid_file(tmp_path):
    accounts = load_accounts(write(tmp_path, VALID), ENV)
    assert list(accounts) == ["support", "info"]
    support, info = accounts["support"], accounts["info"]
    assert support.port == 993 and support.security == "tls" and support.mode == "readonly"
    assert info.port == 143 and info.writable and info.label == "info"
    assert support.password == "pw1"


def test_password_is_not_in_repr(tmp_path):
    accounts = load_accounts(write(tmp_path, VALID), ENV)
    assert "pw1" not in repr(accounts["support"])


def test_public_view_hides_connection_data(tmp_path):
    public = load_accounts(write(tmp_path, VALID), ENV)["support"].public()
    assert public == {"id": "support", "label": "Support", "mode": "readonly"}


def test_missing_password_env_fails(tmp_path):
    with pytest.raises(ConfigError, match="MAIL_INFO_PASSWORD"):
        load_accounts(write(tmp_path, VALID), {"MAIL_SUPPORT_PASSWORD": "pw1"})


def test_password_from_secret_file(tmp_path):
    secret = tmp_path / "secret"
    secret.write_text("from-file\n", encoding="utf-8")
    env = {"MAIL_SUPPORT_PASSWORD_FILE": str(secret), "MAIL_INFO_PASSWORD": "pw2"}
    assert load_accounts(write(tmp_path, VALID), env)["support"].password == "from-file"


@pytest.mark.parametrize(
    ("snippet", "message"),
    [
        ("security: plain", "security"),
        ("mode: admin", "mode"),
        ("port: 70000", "port"),
        ("unknown_key: 1", "unknown keys"),
        ("folders_deny: Junk", "folders_deny"),
    ],
)
def test_invalid_values_fail(tmp_path, snippet, message):
    text = f"""
accounts:
  - id: a
    host: h
    username: u
    password_env: P
    {snippet}
"""
    with pytest.raises(ConfigError, match=message):
        load_accounts(write(tmp_path, text), {"P": "x"})


def test_duplicate_and_invalid_ids_fail(tmp_path):
    entry = "  - {id: a, host: h, username: u, password_env: P}\n"
    with pytest.raises(ConfigError, match="Duplicate"):
        load_accounts(write(tmp_path, "accounts:\n" + entry + entry), {"P": "x"})
    with pytest.raises(ConfigError, match="must match"):
        load_accounts(
            write(tmp_path, "accounts:\n  - {id: 'A B', host: h, username: u, password_env: P}\n"), {"P": "x"}
        )


def test_empty_or_missing_file_fails(tmp_path):
    with pytest.raises(ConfigError):
        load_accounts(write(tmp_path, "accounts: []"), {})
    with pytest.raises(ConfigError, match="not found"):
        load_accounts(tmp_path / "missing.yaml", {})


def test_folder_rules_deny_wins(tmp_path):
    support = load_accounts(write(tmp_path, VALID), ENV)["support"]
    assert support.folder_allowed("INBOX")
    assert support.folder_allowed("inbox")
    assert support.folder_allowed("Archiv/2025")
    assert not support.folder_allowed("Archiv/Geheim")
    assert not support.folder_allowed("Junk")
    assert not support.folder_allowed("Sent")


def test_no_allow_list_permits_everything_but_denied(tmp_path):
    text = VALID.replace('    folders_allow: ["INBOX", "Archiv/*"]\n', "")
    support = load_accounts(write(tmp_path, text), ENV)["support"]
    assert support.folder_allowed("Sent")
    assert not support.folder_allowed("Junk")


def test_settings_from_env():
    settings = Settings.from_env({"MCP_PORT": "9000", "MAX_BODY_CHARS": "500", "LOG_LEVEL": "debug"})
    assert settings.port == 9000 and settings.max_body_chars == 500 and settings.log_level == "DEBUG"
    assert settings.host == "0.0.0.0" and settings.accounts_file == "/config/accounts.yaml"
    with pytest.raises(ConfigError):
        Settings.from_env({"MAX_ATTACHMENT_MB": "0"})
