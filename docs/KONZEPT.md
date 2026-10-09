# Konzept: IMAP-MCP-Server

## Zielbild & Scope

Ein schlanker Docker-Container stellt ein oder mehrere IMAP-Postfächer als MCP-Server per Streamable HTTP bereit. Ein LLM kann damit Ordner auflisten, Mails suchen, lesen und Anhänge abrufen; in Phase 1 ausschließlich lesend.

Aufbau: ein Prozess, ein Endpunkt `/mcp`, ein `/health`-Endpunkt, Konfiguration nur über Env-Variablen und eine gemountete Datei, optionale API-Key-Absicherung.

| Im Scope (Phase 1) | Nicht im Scope |
|---|---|
| Mehrere IMAP-Konten über TLS (Port 993) | Mails versenden (SMTP) |
| Ordner auflisten, Mails suchen und lesen | Mails löschen oder Papierkorb leeren |
| Anhänge auflisten und abrufen (mit Größenlimit) | Eigene Speicherung oder Indexierung von Mails |
| Lesen ohne Statuswechsel (Mails bleiben ungelesen) | Push-Benachrichtigungen (IMAP IDLE) |
| Pro Konto schaltbarer Schreibmodus (Phase 2) | Weboberfläche |

## Architektur

Der MCP-Client spricht per Streamable HTTP mit einem Container, der die IMAP-Verbindungen hält und dem LLM nur aufbereitete Ergebnisse zurückgibt.

Jede Anfrage durchläuft API-Key-Prüfung, Tool, Konto-Registry und IMAP-Pool; Konfiguration und Secrets werden nur beim Start eingelesen.

```
MCP-Client ──HTTP──▶ API-Key-Prüfung ──▶ Tool ──▶ Konto-Registry ──▶ IMAP-Pool ──TLS──▶ IMAP-Server
```

## Mehrere Postfächer

Alle Konten stehen in einer YAML-Datei, die read-only in den Container gemountet wird. Passwörter stehen nie in der Datei, sondern werden über den Namen einer Env-Variable referenziert.

```yaml
# config/accounts.yaml
accounts:
  - id: support            # stabiler Schlüssel, den das LLM als Parameter nutzt
    label: Support-Postfach
    host: imap.example.com
    port: 993
    username: support@example.com
    password_env: MAIL_SUPPORT_PASSWORD
    mode: readonly         # readonly | readwrite (Phase 2)
    folders_allow: ["INBOX", "Archiv/*"]   # optional, Glob-Muster
    folders_deny: ["Junk", "Trash"]        # optional

  - id: info
    label: Info-Postfach
    host: imap.example.org
    port: 993
    username: info@example.org
    password_env: MAIL_INFO_PASSWORD
    mode: readonly
```

Regeln für die Konto-Registry:

- Beim Start wird die Datei validiert; fehlt eine referenzierte Env-Variable, startet der Container nicht.
- Das LLM sieht nur `id` und `label`, nie Host, Benutzer oder Passwort.
- `folders_deny` gewinnt immer vor `folders_allow`; gesperrte Ordner tauchen auch in `list_folders` nicht auf.
- Pro Konto gibt es höchstens zwei gleichzeitige IMAP-Verbindungen, die nach 5 Minuten Leerlauf geschlossen werden.
- TLS ist Pflicht; STARTTLS auf Port 143 nur per explizitem `security: starttls`, unverschlüsselt gar nicht.

## MCP-Tools

Phase 1 bringt sechs lesende Tools; die drei schreibenden Tools aus Phase 2 werden nur registriert, wenn mindestens ein Konto `mode: readwrite` hat. So bleibt die Tool-Liste für kleinere lokale Modelle kurz.

| Tool | Phase | Parameter | Ergebnis |
|---|---|---|---|
| `list_accounts` | 1 | – | id, label, mode je Konto |
| `list_folders` | 1 | account | Ordnername, Anzahl Mails, Anzahl ungelesen |
| `search_messages` | 1 | account, folder (Standard INBOX), from, to, subject, text, since, before, unseen, flagged, limit (Standard 20, max. 50) | Liste mit uid, Datum, Absender, Betreff, Flags, Anhang ja/nein, neueste zuerst |
| `get_message` | 1 | account, folder, uid, max_chars (Standard 20.000) | Header, Textkörper (HTML in Text umgewandelt), Anhangsliste |
| `get_thread` | 1 | account, folder, uid | Alle Mails derselben Konversation über Message-ID/References, gekürzt |
| `get_attachment` | 1 | account, folder, uid, part | Bei Text, CSV, PDF der extrahierte Text; sonst Metadaten; max. 10 MB |
| `set_flags` | 2 | account, folder, uids, seen, flagged | Neue Flags |
| `move_messages` | 2 | account, folder, uids, target_folder | Neue UIDs im Zielordner |
| `create_draft` | 2 | account, to, cc, subject, body, in_reply_to | Entwurf im Ordner Drafts (kein Versand) |

Verhalten, das für alle Tools gilt:

- Ordner werden mit `EXAMINE` geöffnet und Inhalte mit `BODY.PEEK` geladen, damit Lesen keine Mail als gelesen markiert.
- Mails werden über die IMAP-UID adressiert; jedes Ergebnis enthält zusätzlich `uidvalidity`, damit veraltete UIDs erkannt werden.
- Datumsangaben in ISO 8601, Suchparameter `since`/`before` als Datum ohne Uhrzeit (IMAP-Einschränkung).
- Fehler kommen als klare Meldung zurück, z. B. unbekanntes Konto mit Liste der gültigen ids oder gesperrter Ordner.
- Mailinhalte werden in ein eigenes Feld `content` gelegt und in der Tool-Beschreibung als nicht vertrauenswürdige Fremddaten gekennzeichnet.

## Sicherheit

Postfächer enthalten personenbezogene Daten; deshalb ist der Server standardmäßig nur lokal erreichbar, nur lesend und optional per API-Key geschützt.

- **Netz:** Port nur an 127.0.0.1 gebunden. Zugriff von außen ausschließlich über einen Reverse Proxy mit TLS.
- **API-Key:** Ist `MCP_API_KEYS` gesetzt, muss jede Anfrage an `/mcp` den Header `Authorization: Bearer <key>` oder `X-API-Key` mitsenden, sonst 401. Mehrere Keys kommagetrennt, Vergleich zeitkonstant. `/health` bleibt ohne Key, gibt aber keine Kontodaten preis.
- **Key-Scopes (optional):** Pro Key lässt sich festlegen, welche Konten er sehen darf, z. B. `key1:support+info`. So bekommt jede Anwendung nur ihre Postfächer.
- **Secrets:** Passwörter nur über Env-Variablen oder Docker Secrets, nie im Image, nie im Log. Möglichst App-Passwörter oder eigene IMAP-Benutzer mit minimalen Rechten.
- **Read-only zuerst:** Schreiben pro Konto bewusst freischalten. Löschen und Versenden gibt es bewusst gar nicht.
- **Prompt Injection:** Mails sind Fremdinhalte und können Anweisungen an das LLM enthalten. Inhalte werden gekennzeichnet und gekürzt; schreibende Aktionen sollten im Client eine Bestätigung durch den Nutzer verlangen.
- **Logging:** Protokolliert werden Tool-Name, Konto-id, Dauer und Fehler. Betreffe, Absender und Inhalte werden nicht geloggt.
- **Container-Härtung:** Nicht-Root-Benutzer, `read_only: true`, `cap_drop: [ALL]`, `no-new-privileges`, Konfig-Datei read-only gemountet.
- **Microsoft 365 / Exchange Online:** Dort ist IMAP mit Passwort abgeschaltet; nötig ist OAuth2 (XOAUTH2). Das ist für Phase 3 vorgesehen.

## Deployment

Der Server läuft als einzelner Container per Docker Compose; Konfiguration kommt aus `.env` und der gemounteten `accounts.yaml`.

| Variable | Pflicht | Standard | Zweck |
|---|---|---|---|
| `MCP_HOST` | nein | `0.0.0.0` | Bind-Adresse im Container |
| `MCP_PORT` | nein | `8000` | Port im Container |
| `MCP_API_KEYS` | nein | leer | Kommagetrennte Keys, optional mit Konto-Scope |
| `ACCOUNTS_FILE` | nein | `/config/accounts.yaml` | Pfad zur Konto-Datei |
| `MAIL_<ID>_PASSWORD` | ja, je Konto | – | Passwort, referenziert über `password_env` |
| `MAX_BODY_CHARS` | nein | `20000` | Kürzung von Mailtexten |
| `MAX_ATTACHMENT_MB` | nein | `10` | Größenlimit für Anhänge |
| `LOG_LEVEL` | nein | `INFO` | Log-Level |

```yaml
# docker-compose.yml
services:
  imap-mcp:
    build: .
    image: imap-mcp:latest
    restart: unless-stopped
    env_file: .env
    ports:
      - "127.0.0.1:8770:8000"
    volumes:
      - ./config/accounts.yaml:/config/accounts.yaml:ro
    read_only: true
    tmpfs: [/tmp]
    cap_drop: [ALL]
    security_opt: ["no-new-privileges:true"]
    healthcheck:
      test: ["CMD", "python", "-c", "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/health')"]
      interval: 30s
      timeout: 5s
      retries: 3
```

Technik: Python 3.12 (`python:3.12-slim`), offizielles MCP-SDK (in Version 2 heißt FastMCP `MCPServer`) mit Streamable HTTP, `imapclient` für IMAP, Standardbibliothek `email` für MIME, `html2text` für HTML-Mails, `pypdf` für PDF-Anhänge. Alle Versionen werden bei der Umsetzung exakt gepinnt.

`/health` meldet `ok` sowie je Konto nur, ob der letzte Login erfolgreich war (`ok`/`error`), ohne Details.

## Akzeptanzkriterien

- [ ] `docker compose up` startet, `/health` liefert 200.
- [ ] Ohne gültigen Key liefert `/mcp` 401, sobald `MCP_API_KEYS` gesetzt ist.
- [ ] `list_accounts` zeigt keine Hosts, Benutzer oder Passwörter.
- [ ] `get_message` lässt ungelesene Mails ungelesen.
- [ ] Gesperrte Ordner sind weder auflistbar noch lesbar.
- [ ] Ein Key mit Scope sieht nur seine Konten.
- [ ] Im Read-only-Modus sind keine Schreib-Tools registriert.
- [ ] Logs enthalten keine Mailinhalte, Betreffe oder Passwörter.

## Roadmap

Die Umsetzung läuft in drei Phasen; jede startet erst, wenn die vorherige im Alltag stabil läuft.

1. **Phase 1 – Lesen:** sechs Lese-Tools, mehrere Konten, API-Key, Docker-Deployment.
2. **Phase 2 – Schreiben:** `set_flags`, `move_messages`, `create_draft` für Konten mit `mode: readwrite`; Bestätigung im Client.
3. **Phase 3 – Erweiterungen:** OAuth2 (XOAUTH2) für Microsoft 365 und Gmail, optional SMTP-Versand nur mit expliziter Nutzerfreigabe, optional IMAP IDLE für neue Mails.
