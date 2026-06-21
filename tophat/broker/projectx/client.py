"""REST client for the TopstepX / ProjectX Gateway API.

Docs: https://gateway.docs.projectx.com/
"""

from __future__ import annotations

import os
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx

ET = ZoneInfo("America/New_York")
DEFAULT_API_URL = "https://api.topstepx.com"
DEFAULT_RTC_URL = "https://rtc.topstepx.com"
NQ_SYMBOL = "F.US.ENQ"


class ProjectXError(RuntimeError):
    pass


class ProjectXClient:
    def __init__(
        self,
        username: str | None = None,
        api_key: str | None = None,
        *,
        base_url: str | None = None,
        timeout: float = 30.0,
    ) -> None:
        self.username = username or os.environ["PROJECTX_USERNAME"]
        self.api_key = api_key or os.environ["PROJECTX_API_KEY"]
        self.base_url = (base_url or os.getenv("PROJECTX_API_URL", DEFAULT_API_URL)).rstrip("/")
        self.rtc_url = os.getenv("PROJECTX_RTC_URL", DEFAULT_RTC_URL).rstrip("/")
        self._client = httpx.Client(base_url=self.base_url, timeout=timeout)
        self._token: str | None = None

    @property
    def token(self) -> str | None:
        return self._token

    def close(self) -> None:
        self._client.close()

    def login(self) -> str:
        data = self._post("/api/Auth/loginKey", {
            "userName": self.username,
            "apiKey": self.api_key,
        }, auth=False)
        self._token = data["token"]
        return self._token

    def validate_session(self) -> str:
        data = self._post("/api/Auth/validate", {}, auth=True)
        if data.get("newToken"):
            self._token = data["newToken"]
        return self._token or ""

    def _ensure_token(self) -> None:
        if not self._token:
            self.login()

    def search_accounts(self, *, only_active: bool = True) -> list[dict]:
        data = self._post("/api/Account/search", {"onlyActiveAccounts": only_active})
        return data.get("accounts", [])

    def available_contracts(self, *, live: bool = False) -> list[dict]:
        data = self._post("/api/Contract/available", {"live": live})
        return data.get("contracts", [])

    def active_nq_contract(self, *, live: bool = False,
                           symbol: str | None = None) -> dict:
        sym = symbol or os.getenv("PROJECTX_NQ_SYMBOL", NQ_SYMBOL)
        hits = [
            c for c in self.available_contracts(live=live)
            if c.get("symbolId") == sym and c.get("activeContract")
        ]
        if not hits:
            raise ProjectXError(f"no active contract for {sym}")
        return hits[0]

    def place_order(self, body: dict) -> int:
        data = self._post("/api/Order/place", body)
        return int(data["orderId"])

    def search_open_orders(self, account_id: int) -> list[dict]:
        data = self._post("/api/Order/searchOpen", {"accountId": account_id})
        return data.get("orders", [])

    def search_open_positions(self, account_id: int) -> list[dict]:
        data = self._post("/api/Position/searchOpen", {"accountId": account_id})
        return data.get("positions", [])

    def close_contract(self, account_id: int, contract_id: str) -> None:
        self._post("/api/Position/closeContract", {
            "accountId": account_id,
            "contractId": contract_id,
        })

    def drive_direction_from_bars(self, contract_id: str, *, live: bool = False) -> int:
        """Fallback: one-shot REST bar fetch for drive (used by CLI probe)."""
        now = datetime.now(ET)
        start = now.replace(hour=9, minute=30, second=0, microsecond=0)
        end = now.replace(hour=9, minute=45, second=0, microsecond=0)
        if now < start:
            raise ProjectXError("before RTH open (09:30 ET) - drive not ready")
        if now < end:
            end = now
        bars = self.retrieve_bars(
            contract_id,
            start_time=start.astimezone(ZoneInfo("UTC")),
            end_time=end.astimezone(ZoneInfo("UTC")),
            unit=2, unit_number=1, live=live, limit=20,
        )
        if not bars:
            return 0
        or_open, or_close = bars[0]["o"], bars[-1]["c"]
        if or_close > or_open:
            return 1
        if or_close < or_open:
            return -1
        return 0

    def retrieve_bars(
        self, contract_id: str, *, start_time: datetime, end_time: datetime,
        unit: int, unit_number: int, live: bool = False,
        limit: int = 500, include_partial: bool = True,
    ) -> list[dict]:
        data = self._post("/api/History/retrieveBars", {
            "contractId": contract_id,
            "live": live,
            "startTime": start_time.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "endTime": end_time.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "unit": unit,
            "unitNumber": unit_number,
            "limit": limit,
            "includePartialBar": include_partial,
        })
        return data.get("bars", [])

    def _post(self, path: str, body: dict, *, auth: bool = True) -> dict:
        if auth:
            self._ensure_token()
        headers = {"accept": "text/plain", "Content-Type": "application/json"}
        if auth and self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        resp = self._client.post(path, json=body, headers=headers)
        if resp.status_code == 401 and auth:
            self.login()
            headers["Authorization"] = f"Bearer {self._token}"
            resp = self._client.post(path, json=body, headers=headers)
        if resp.status_code == 429:
            # Rate limited — honor Retry-After (bounded) and retry once.
            try:
                wait = float(resp.headers.get("Retry-After", "1") or 1)
            except ValueError:
                wait = 1.0
            time.sleep(min(max(wait, 0.5), 5.0))
            resp = self._client.post(path, json=body, headers=headers)
        try:
            data = resp.json()
        except Exception as exc:
            raise ProjectXError(f"{path}: HTTP {resp.status_code}: {resp.text}") from exc
        if resp.status_code >= 400:
            raise ProjectXError(
                f"{path}: HTTP {resp.status_code}: {data.get('errorMessage') or data}")
        if not data.get("success", True):
            raise ProjectXError(
                f"{path}: error {data.get('errorCode')}: {data.get('errorMessage')}")
        return data
