# imap-mcp-server

A small Docker container that exposes one or more IMAP mailboxes as an [MCP](https://modelcontextprotocol.io) server over Streamable HTTP. An LLM can list folders, search and read messages and fetch attachments. **Read-only by default**: reading never marks a message as seen, and nothing can be deleted or sent.

- One process, one endpoint `/mcp`, plus `/health`
- Several accounts, configured in a read-only mounted YAML file; passwords come from environment variables
- Optional API keys, optionally scoped to specific accounts
- Optional write mode per account (flags, move, drafts). Deleting and sending are not supported.

The design document (German) is in [docs/KONZEPT.md](docs/KONZEPT.md).

## Quick start

```bash
cp .env.example .env                                # set passwords and API keys
cp config/accounts.example.yaml config/accounts.yaml
docker compose up -d --build
curl http://127.0.0.1:8770/health
```

The port is bound to `127.0.0.1` only. For access from other machines, put a reverse proxy with TLS in front of it.

Connect an MCP client to `http://127.0.0.1:8770/mcp`, e.g. for Claude Code:

```bash
claude mcp add --transport http imap http://127.0.0.1:8770/mcp --header "Authorization: Bearer <key>"
```

## Accounts

```yaml
# config/accounts.yaml
accounts:
  - id: support                       # key the LLM uses as parameter
    label: Support mailbox
    host: imap.example.com
    port: 993
    username: support@example.com
    password_env: MAIL_SUPPORT_PASSWORD
    mode: readonly                    # readonly | readwrite
    folders_allow: ["INBOX", "Archive/*"]
    folders_deny: ["Junk", "Trash"]
```

| Key | Required | Default | Description |
|---|---|---|---|
| `id` | yes | – | Stable identifier (`a-z`, `0-9`, `_`, `-`) |
| `label` | no | `id` | Display name shown to the LLM |
| `host`, `username` | yes | – | IMAP server and login, never shown to the LLM |
| `password_env` | yes | – | Name of the env variable holding the password. `<NAME>_FILE` (Docker secrets) works too |
| `port` | no | 993 (143 with STARTTLS) | |
| `security` | no | `tls` | `tls` (implicit TLS) or `starttls`. Plaintext is not supported |
| `tls_verify` | no | `true` | Only disable for test servers with self-signed certificates |
| `mode` | no | `readonly` | `readwrite` enables the write tools for this account |
| `folders_allow` | no | all | Glob patterns; the folder delimiter depends on the server (`/` or `.`) |
| `folders_deny` | no | none | Glob patterns; always wins over `folders_allow`. Denied folders are neither listed nor readable |

The server refuses to start if the file is invalid or a referenced password variable is missing.

## Environment variables

| Variable | Default | Description |
|---|---|---|
| `MCP_HOST` | `0.0.0.0` | Bind address inside the container |
| `MCP_PORT` | `8000` | Port inside the container |
| `MCP_API_KEYS` | empty | Comma-separated keys. `key:acc1+acc2` restricts a key to those accounts |
| `MCP_ALLOWED_HOSTS` | empty | Optional: enables DNS-rebinding protection, e.g. `localhost:8770,mcp.example.com` |
| `ACCOUNTS_FILE` | `/config/accounts.yaml` | Path to the account file |
| `MAX_BODY_CHARS` | `20000` | Body length limit |
| `MAX_ATTACHMENT_MB` | `10` | Attachment size limit |
| `LOG_LEVEL` | `INFO` | `DEBUG` also enables SDK logs, which may contain request details |

When `MCP_API_KEYS` is set, every request to `/mcp` needs `Authorization: Bearer <key>` or `X-API-Key: <key>`, otherwise it gets `401`. `/health` stays open and only reports per account whether the last login worked.

Generate keys with `python -c "import secrets; print(secrets.token_urlsafe(32))"`.

## Tools

| Tool | Description |
|---|---|
| `list_accounts` | Accounts visible to the key: `id`, `label`, `mode` |
| `list_folders` | Accessible folders with message and unread counts |
| `search_messages` | Filter by `from`, `to`, `subject`, `text`, `since`, `before` (dates `YYYY-MM-DD`, arrival date), `unseen`, `flagged`; newest first, `limit` up to 50 |
| `get_message` | Headers, body as text (HTML converted), attachment list |
| `get_thread` | Conversation via Message-ID/References/In-Reply-To, within the same folder |
| `get_attachment` | Text, CSV, JSON, XML, PDF and attached e-mails as extracted text; other types as metadata |
| `set_flags` | *(readwrite)* Set or clear `seen` / `flagged` |
| `move_messages` | *(readwrite)* Move to another folder (requires the IMAP `MOVE` extension), returns new UIDs |
| `create_draft` | *(readwrite)* Store a plain-text draft in the Drafts folder. Nothing is sent |

The write tools are only registered if at least one account has `mode: readwrite`.

Messages are addressed by UID. Every result includes `uidvalidity` so that stale UIDs can be detected. Folders are opened with `EXAMINE` and messages fetched with `BODY.PEEK`, so reading never changes the `\Seen` flag.

## Security notes

- **Prompt injection:** e-mails are untrusted content. Message data is returned in fields named `content`, and tool descriptions tell the model to treat it as data. Clients should ask the user to confirm write actions.
- **Logging:** only tool name, account id, duration and outcome are logged. Subjects, senders, bodies, passwords and keys are never logged.
- **Container:** runs as a non-root user with a read-only root filesystem, all capabilities dropped and `no-new-privileges`.
- **Credentials:** use app passwords or dedicated IMAP users with minimal rights where possible.
- **Microsoft 365 / Exchange Online** disabled password-based IMAP; OAuth2 (XOAUTH2) is planned for a later phase.

## Development

```bash
python3.12 -m venv .venv && .venv/bin/pip install -e ".[dev]"
docker compose --profile test up -d greenmail   # IMAPS on 127.0.0.1:3993, API on 3980
.venv/bin/pytest
.venv/bin/ruff check . && .venv/bin/ruff format --check .
```

Integration tests run against [GreenMail](https://greenmail-mail-test.github.io/greenmail/). They reset the test server through its API and are skipped if it is not reachable.

## License

[MIT](LICENSE)
