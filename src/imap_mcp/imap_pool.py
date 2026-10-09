"""Small per-account pool of IMAP connections (blocking imapclient, used from worker threads)."""

from __future__ import annotations

import contextlib
import logging
import ssl
import threading
import time
from collections.abc import Iterator
from typing import Literal

from imapclient import IMAPClient

from .config import Account

log = logging.getLogger(__name__)

MAX_CONNECTIONS = 2
IDLE_TIMEOUT = 300.0
SOCKET_TIMEOUT = 30.0

LoginStatus = Literal["ok", "error", "unknown"]


class ImapUnavailable(Exception):
    """Login or connection failed. The message never contains credentials or server responses."""


class _Idle:
    __slots__ = ("client", "since")

    def __init__(self, client: IMAPClient) -> None:
        self.client = client
        self.since = time.monotonic()


class AccountPool:
    def __init__(self, account: Account, max_connections: int = MAX_CONNECTIONS, idle_timeout: float = IDLE_TIMEOUT):
        self.account = account
        self.idle_timeout = idle_timeout
        self._slots = threading.BoundedSemaphore(max_connections)
        self._idle: list[_Idle] = []
        self._lock = threading.Lock()
        self.login_status: LoginStatus = "unknown"

    @contextlib.contextmanager
    def connection(self) -> Iterator[IMAPClient]:
        if not self._slots.acquire(timeout=SOCKET_TIMEOUT * 2):
            raise ImapUnavailable(f"All connections for account '{self.account.id}' are busy, try again")
        client: IMAPClient | None = None
        try:
            client = self._take_idle() or self._connect()
            yield client
        except (OSError, IMAPClient.AbortError):
            # Broken socket: never return it to the pool.
            _logout_quietly(client)
            client = None
            raise
        finally:
            if client is not None:
                with self._lock:
                    self._idle.append(_Idle(client))
            self._slots.release()

    def close_idle(self, force: bool = False) -> None:
        now = time.monotonic()
        with self._lock:
            expired = [c for c in self._idle if force or now - c.since >= self.idle_timeout]
            self._idle = [c for c in self._idle if c not in expired]
        for entry in expired:
            _logout_quietly(entry.client)

    def _take_idle(self) -> IMAPClient | None:
        self.close_idle()
        while True:
            with self._lock:
                if not self._idle:
                    return None
                client = self._idle.pop().client
            try:
                client.noop()
                return client
            except Exception:
                _logout_quietly(client)

    def _connect(self) -> IMAPClient:
        account = self.account
        context = ssl.create_default_context()
        if not account.tls_verify:
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
        try:
            if account.security == "tls":
                client = IMAPClient(
                    account.host, port=account.port, ssl=True, ssl_context=context, timeout=SOCKET_TIMEOUT
                )
            else:
                client = IMAPClient(account.host, port=account.port, ssl=False, timeout=SOCKET_TIMEOUT)
                client.starttls(context)
            client.login(account.username, account.password)
        except Exception as exc:
            self.login_status = "error"
            log.warning("imap login failed account=%s error=%s", account.id, type(exc).__name__)
            raise ImapUnavailable(f"Could not connect to account '{account.id}'") from None
        self.login_status = "ok"
        return client


def _logout_quietly(client: IMAPClient | None) -> None:
    if client is None:
        return
    with contextlib.suppress(Exception):
        client.logout()
    with contextlib.suppress(Exception):
        client.shutdown()


class PoolRegistry:
    """One pool per account plus a background reaper that closes idle connections."""

    def __init__(self, accounts: dict[str, Account], idle_timeout: float = IDLE_TIMEOUT):
        self.pools = {account_id: AccountPool(acc, idle_timeout=idle_timeout) for account_id, acc in accounts.items()}
        self._stop = threading.Event()
        self._reaper: threading.Thread | None = None

    def __getitem__(self, account_id: str) -> AccountPool:
        return self.pools[account_id]

    def start(self) -> None:
        if self._reaper is None:
            self._reaper = threading.Thread(target=self._reap, name="imap-reaper", daemon=True)
            self._reaper.start()

    def stop(self) -> None:
        self._stop.set()
        for pool in self.pools.values():
            pool.close_idle(force=True)

    def health(self) -> dict[str, LoginStatus]:
        return {account_id: pool.login_status for account_id, pool in self.pools.items()}

    def _reap(self) -> None:
        while not self._stop.wait(30):
            for pool in self.pools.values():
                pool.close_idle()
