"""Minimal Schwab Trader API client (stdlib only).

The token file is shared. This module refreshes the access token and writes the file
back atomically. It never revokes a token and never runs the browser login.
Order calls go through OrderGuard and are refused without a ticket Tod approved.
"""
from __future__ import annotations

import base64
import gzip
import json
import os
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from pathlib import Path

BASE = "https://api.schwabapi.com"
TOKEN_URL = BASE + "/v1/oauth/token"
REFRESH_AFTER_SECS = 1500


class SchwabError(RuntimeError):
    def __init__(self, status: int, body: str, url: str = ""):
        super().__init__(f"HTTP {status} {url}: {body[:300]}")
        self.status, self.body, self.url = status, body, url


def _decode(raw: bytes) -> str:
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    return raw.decode("utf-8", "replace")


@contextmanager
def _file_lock(path: Path):
    lock_path = path.with_name(path.name + ".lock")
    handle = open(lock_path, "a+")
    try:
        try:
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_EX)
        except ImportError:
            try:
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            except Exception:  # noqa: BLE001
                pass
        yield
    finally:
        handle.close()


class TokenStore:
    def __init__(self, path: str | Path, app_key: str | None = None, app_secret: str | None = None):
        self.path = Path(path).expanduser()
        self.key = app_key or os.environ.get("SCHWAB_APP_KEY")
        self.secret = app_secret or os.environ.get("SCHWAB_APP_SECRET")

    def _read(self) -> dict:
        return json.loads(self.path.read_text())

    def _write(self, data: dict) -> None:
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), prefix=".tokens.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as handle:
                json.dump(data, handle)
            try:
                os.chmod(tmp, 0o600)
            except OSError:
                pass
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def age(self) -> float:
        return time.time() - os.path.getmtime(self.path)

    def access_token(self, force_refresh: bool = False) -> str:
        if not force_refresh and self.age() <= REFRESH_AFTER_SECS:
            return self._read()["access_token"]
        with _file_lock(self.path):
            if not force_refresh and self.age() <= REFRESH_AFTER_SECS:
                return self._read()["access_token"]
            token = self._read()
            if not (self.key and self.secret):
                raise RuntimeError("SCHWAB_APP_KEY / SCHWAB_APP_SECRET are not set; cannot refresh")
            body = urllib.parse.urlencode({
                "grant_type": "refresh_token",
                "refresh_token": token["refresh_token"],
            }).encode()
            auth = base64.b64encode(f"{self.key}:{self.secret}".encode()).decode()
            req = urllib.request.Request(TOKEN_URL, data=body, method="POST", headers={
                "Authorization": "Basic " + auth,
                "Content-Type": "application/x-www-form-urlencoded",
            })
            try:
                refreshed = json.loads(_decode(urllib.request.urlopen(req, timeout=20).read()))
            except urllib.error.HTTPError as exc:
                raise RuntimeError(
                    f"token refresh failed (HTTP {exc.code}). The 7-day refresh token has probably "
                    f"expired. {_decode(exc.read())[:200]}") from None
            token.update(refreshed)
            self._write(token)
            return token["access_token"]


class SchwabClient:
    def __init__(self, tokens: TokenStore, order_guard=None, timeout: float = 20.0):
        self.tokens = tokens
        self.order_guard = order_guard
        self.timeout = timeout
        self._acct_hash: str | None = None
        self.acct_last3: str | None = None
        self.calls = 0

    def _do(self, method: str, path: str, params: dict | None = None, body=None, retry401: bool = True):
        url = BASE + path + ("?" + urllib.parse.urlencode(params) if params else "")
        headers = {
            "Authorization": "Bearer " + self.tokens.access_token(),
            "Accept": "application/json",
            "Accept-Encoding": "gzip",
        }
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        self.calls += 1
        try:
            response = urllib.request.urlopen(req, timeout=self.timeout)
            return response.status, dict(response.headers), _decode(response.read())
        except urllib.error.HTTPError as exc:
            text = _decode(exc.read())
            if exc.code == 401 and retry401:
                self.tokens.access_token(force_refresh=True)
                return self._do(method, path, params, body, retry401=False)
            raise SchwabError(exc.code, text, path) from None

    def get(self, path: str, params: dict | None = None):
        text = ""
        for attempt in range(3):
            try:
                _status, _headers, text = self._do("GET", path, params)
                break
            except SchwabError as exc:
                if exc.status < 500 or attempt == 2:
                    raise
                time.sleep(1.0 + attempt)
        return json.loads(text) if text.strip() else None

    def quotes(self, symbols: list[str]) -> dict:
        return self.get("/marketdata/v1/quotes", {"symbols": ",".join(symbols), "fields": "quote,reference"})

    def price_history_range(self, symbol: str, frequency_type: str, frequency: int,
                            start_ms: int, end_ms: int, extended: bool = False,
                            period_type: str = "day") -> list[dict]:
        params = {
            "symbol": symbol,
            "periodType": period_type,
            "frequencyType": frequency_type,
            "frequency": frequency,
            "startDate": int(start_ms),
            "endDate": int(end_ms),
            "needExtendedHoursData": str(extended).lower(),
        }
        return (self.get("/marketdata/v1/pricehistory", params) or {}).get("candles", [])

    def option_chain(self, symbol: str, day_iso: str, strike_count: int | None = None) -> dict:
        params = {
            "symbol": symbol,
            "fromDate": day_iso,
            "toDate": day_iso,
            "contractType": "ALL",
            "includeUnderlyingQuote": "true",
        }
        if strike_count:
            params["strikeCount"] = strike_count
        return self.get("/marketdata/v1/chains", params) or {}

    def market_hours(self, markets: str, day_iso: str) -> dict:
        return self.get("/marketdata/v1/markets", {"markets": markets, "date": day_iso}) or {}

    def account_hash(self, last3: str | None = None) -> str:
        if self._acct_hash is None:
            accounts = self.get("/trader/v1/accounts/accountNumbers") or []
            chosen = select_account(accounts, last3)
            self._acct_hash = chosen["hashValue"]
            self.acct_last3 = str(chosen["accountNumber"])[-3:]
        return self._acct_hash

    def account(self, positions: bool = True) -> dict:
        hashed = self.account_hash()
        return self.get(f"/trader/v1/accounts/{hashed}", {"fields": "positions"} if positions else None) or {}

    def orders_today(self, from_iso: str, to_iso: str) -> list[dict]:
        hashed = self.account_hash()
        return self.get(
            f"/trader/v1/accounts/{hashed}/orders",
            {"fromEnteredTime": from_iso, "toEnteredTime": to_iso}) or []

    def get_order(self, order_id: str) -> dict:
        return self.get(f"/trader/v1/accounts/{self.account_hash()}/orders/{order_id}") or {}

    def preview_order(self, order: dict) -> dict:
        hashed = self.account_hash()
        try:
            status, _headers, text = self._do("POST", f"/trader/v1/accounts/{hashed}/previewOrder", body=order)
        except SchwabError as exc:
            status, text = exc.status, exc.body
        try:
            body = json.loads(text) if text.strip() else {}
        except ValueError:
            body = {"raw": text}
        validation = body.get("orderValidationResult", {}) if isinstance(body, dict) else {}

        def message(item):
            return item.get("message") or item.get("activityMessage") or json.dumps(item)

        rejects = [message(item) for item in (validation.get("rejects") or [])]
        warnings = [message(item) for item in (validation.get("warns") or [])]
        return {
            "http": status,
            "ok": status < 400 and not rejects,
            "rejects": rejects,
            "warnings": warnings,
            "raw": None if status < 400 else text[:600],
            "body": body,
        }

    def place_order(self, order: dict, ticket_id: str) -> str:
        self.order_guard.authorize("place", ticket_id)
        hashed = self.account_hash()
        _status, headers, _text = self._do("POST", f"/trader/v1/accounts/{hashed}/orders", body=order)
        return _order_id_from(headers)

    def cancel_order(self, order_id: str, ticket_id: str) -> None:
        self.order_guard.authorize("cancel", ticket_id, order_id)
        hashed = self.account_hash()
        self._do("DELETE", f"/trader/v1/accounts/{hashed}/orders/{order_id}")


def select_account(accounts, last3: str | None) -> dict:
    """The configured suffix must match exactly one account."""
    rows = list(accounts or [])
    if last3:
        rows = [row for row in rows if str(row.get("accountNumber", "")).endswith(str(last3))]
    if len(rows) != 1:
        suffix = f" ending {last3}" if last3 else ""
        raise RuntimeError(f"expected exactly one Schwab account{suffix}, found {len(rows)}")
    return rows[0]


def _order_id_from(headers: dict) -> str:
    location = headers.get("Location") or headers.get("location") or ""
    return location.rstrip("/").rsplit("/", 1)[-1] if location else ""
