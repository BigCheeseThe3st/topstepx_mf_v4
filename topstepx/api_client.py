# © 2026 BigCheeseThe3st
# Licensed under NCSAL v1.1 (see LICENSE.txt)
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass
from typing import Any, Dict, Optional

import requests

DEFAULT_API_BASE = "https://api.topstepx.com"

class ApiError(RuntimeError):
    pass

@dataclass
class Session:
    token: str
    created_ts: float

class TopstepXClient:
    """
    Minimal REST client for TopstepX ProjectX gateway endpoints used by the dashboard:
      - POST /api/Auth/loginKey
      - POST /api/Auth/validate
      - POST /api/Account/search
      - POST /api/Contract/available
      - POST /api/Contract/search
      - POST /api/History/retrieveBars
    """
    def __init__(self, api_base: str = DEFAULT_API_BASE, timeout: int = 20):
        self.api_base = api_base.rstrip("/")
        self.timeout = timeout
        self._session: Optional[Session] = None
        self._username: Optional[str] = None

    @property
    def token(self) -> Optional[str]:
        return self._session.token if self._session else None

    def _headers(self) -> Dict[str, str]:
        h = {"Content-Type": "application/json", "accept": "text/plain"}
        if self.token:
            # ProjectX uses JWT bearer token for authenticated endpoints
            h["Authorization"] = f"Bearer {self.token}"
        return h

    def login_with_key(self, username: str, api_key: str) -> str:
        url = f"{self.api_base}/api/Auth/loginKey"
        payload = {"userName": username, "apiKey": api_key}
        r = requests.post(url, json=payload, headers=self._headers(), timeout=self.timeout)
        if r.status_code != 200:
            raise ApiError(f"HTTP {r.status_code} from /api/Auth/loginKey: {r.text}")
        data = r.json()
        if not data.get("success", False):
            raise ApiError(f"Login failed: {data.get('errorMessage')}")
        token = data.get("token") or data.get("accessToken") or data.get("jwt")
        if not token:
            raise ApiError(f"Login response missing token field: keys={list(data.keys())}")
        self._session = Session(token=token, created_ts=time.time())
        self._username = username
        return token

    def validate(self) -> bool:
        """
        Validate/refresh token. Docs show POST /api/Auth/validate (no body).
        """
        url = f"{self.api_base}/api/Auth/validate"
        r = requests.post(url, json={}, headers=self._headers(), timeout=self.timeout)
        if r.status_code != 200:
            raise ApiError(f"HTTP {r.status_code} from /api/Auth/validate: {r.text}")
        data = r.json()
        return bool(data.get("success", False))

    def account_search(self, only_active_accounts: bool = True) -> Dict[str, Any]:
        url = f"{self.api_base}/api/Account/search"
        payload = {"onlyActiveAccounts": bool(only_active_accounts)}
        r = requests.post(url, json=payload, headers=self._headers(), timeout=self.timeout)
        if r.status_code != 200:
            raise ApiError(f"HTTP {r.status_code} from /api/Account/search: {r.text}")
        data = r.json()
        if not data.get("success", False):
            raise ApiError(f"Account search failed: {data.get('errorMessage')}")
        return data

    def contract_available(self, live: bool = False) -> Dict[str, Any]:
        url = f"{self.api_base}/api/Contract/available"
        payload = {"live": bool(live)}
        r = requests.post(url, json=payload, headers=self._headers(), timeout=self.timeout)
        if r.status_code != 200:
            raise ApiError(f"HTTP {r.status_code} from /api/Contract/available: {r.text}")
        data = r.json()
        if not data.get("success", False):
            raise ApiError(f"Contract available failed: {data.get('errorMessage')}")
        return data

    def contract_search(self, query: str, live: bool = False) -> Dict[str, Any]:
        # Some tenants expose /api/Contract/search; used as fallback
        url = f"{self.api_base}/api/Contract/search"
        payload = {"searchText": query, "live": bool(live)}
        r = requests.post(url, json=payload, headers=self._headers(), timeout=self.timeout)
        if r.status_code != 200:
            raise ApiError(f"HTTP {r.status_code} from /api/Contract/search: {r.text}")
        data = r.json()
        if not data.get("success", False):
            raise ApiError(f"Contract search failed: {data.get('errorMessage')}")
        return data

    def retrieve_bars(
        self,
        contract_id: str,
        timeframe: Optional[str] = None,
        limit: int = 200,
        *,
        live: bool = False,
        unit: Optional[int] = None,
        unit_number: Optional[int] = None,
        start_time: Optional[str] = None,
        end_time: Optional[str] = None,
        include_partial_bar: Optional[bool] = None,
    ) -> Dict[str, Any]:
        """
        Retrieve bars via /api/History/retrieveBars.
        Preferred params per docs: unit + unit_number, live, start_time/end_time, include_partial_bar.
        For compatibility, timeframe like "1m" is converted into unit=Minute, unit_number=1.
        """
        url = f"{self.api_base}/api/History/retrieveBars"
        payload: Dict[str, Any] = {"contractId": contract_id, "limit": int(limit)}
        if timeframe and (unit is None or unit_number is None):
            tf = timeframe.strip().lower()
            if tf.endswith("m"):
                unit = 2  # Minute
                unit_number = int(tf[:-1] or "1")
            elif tf.endswith("h"):
                unit = 3  # Hour
                unit_number = int(tf[:-1] or "1")
            elif tf.endswith("d"):
                unit = 4  # Day
                unit_number = int(tf[:-1] or "1")
        if unit is not None:
            payload["unit"] = int(unit)
        if unit_number is not None:
            payload["unitNumber"] = int(unit_number)
        payload["live"] = bool(live)
        # If the API requires start/end times, auto-fill for timeframe requests.
        if (start_time is None or end_time is None) and unit is not None and unit_number is not None:
            end_dt = datetime.now(timezone.utc)
            if end_time is not None:
                try:
                    end_dt = datetime.fromisoformat(end_time.replace("Z", "+00:00"))
                except Exception:
                    end_dt = datetime.now(timezone.utc)
            if unit == 1:  # Second
                step = timedelta(seconds=unit_number)
            elif unit == 2:  # Minute
                step = timedelta(minutes=unit_number)
            elif unit == 3:  # Hour
                step = timedelta(hours=unit_number)
            elif unit == 4:  # Day
                step = timedelta(days=unit_number)
            elif unit == 5:  # Week
                step = timedelta(weeks=unit_number)
            else:  # Month approx
                step = timedelta(days=30 * unit_number)
            start_dt = end_dt - (step * int(limit))
            if start_time is None:
                start_time = start_dt.isoformat()
            if end_time is None:
                end_time = end_dt.isoformat()
        if start_time:
            payload["startTime"] = start_time
        if end_time:
            payload["endTime"] = end_time
        if include_partial_bar is not None:
            payload["includePartialBar"] = bool(include_partial_bar)
        r = requests.post(url, json=payload, headers=self._headers(), timeout=self.timeout)
        if r.status_code != 200:
            raise ApiError(f"HTTP {r.status_code} from /api/History/retrieveBars: {r.text}")
        data = r.json()
        if not data.get("success", False):
            raise ApiError(f"retrieveBars failed: {data.get('errorMessage')}")
        return data

    def place_order(
        self,
        *,
        account_id: int,
        contract_id: str,
        side: int,
        size: int,
        stop_ticks: Optional[int] = None,
        take_profit_ticks: Optional[int] = None,
        custom_tag: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Place a market order with optional SL/TP brackets (ticks).
        Docs example:
          POST /api/Order/place with type=2 (market), side=0 (buy), side=1 (sell)
        """
        url = f"{self.api_base}/api/Order/place"
        payload: Dict[str, Any] = {
            "accountId": int(account_id),
            "contractId": str(contract_id),
            "type": 2,  # market
            "side": int(side),
            "size": int(size),
            "limitPrice": None,
            "stopPrice": None,
            "trailPrice": None,
            "customTag": custom_tag,
        }
        if stop_ticks and stop_ticks > 0:
            # Use STOP order for stop loss brackets (per API)
            payload["stopLossBracket"] = {"ticks": int(stop_ticks), "type": 4}
        if take_profit_ticks and take_profit_ticks > 0:
            payload["takeProfitBracket"] = {"ticks": int(take_profit_ticks), "type": 1}
        r = requests.post(url, json=payload, headers=self._headers(), timeout=self.timeout)
        if r.status_code != 200:
            raise ApiError(f"HTTP {r.status_code} from /api/Order/place: {r.text}")
        data = r.json()
        if not data.get("success", False):
            raise ApiError(f"Order place failed: {data.get('errorMessage')}")
        return data

    def search_open_orders(self, *, account_id: int) -> Dict[str, Any]:
        """
        Search open orders via /api/Order/searchOpen.
        """
        url = f"{self.api_base}/api/Order/searchOpen"
        payload: Dict[str, Any] = {"accountId": int(account_id)}
        r = requests.post(url, json=payload, headers=self._headers(), timeout=self.timeout)
        if r.status_code != 200:
            raise ApiError(f"HTTP {r.status_code} from /api/Order/searchOpen: {r.text}")
        data = r.json()
        if not data.get("success", False):
            raise ApiError(f"Order searchOpen failed: {data.get('errorMessage')}")
        return data

    def cancel_order(self, *, account_id: int, order_id: int) -> Dict[str, Any]:
        """
        Cancel an order via /api/Order/cancel.
        """
        url = f"{self.api_base}/api/Order/cancel"
        payload: Dict[str, Any] = {"accountId": int(account_id), "orderId": int(order_id)}
        r = requests.post(url, json=payload, headers=self._headers(), timeout=self.timeout)
        if r.status_code != 200:
            raise ApiError(f"HTTP {r.status_code} from /api/Order/cancel: {r.text}")
        data = r.json()
        if not data.get("success", False):
            raise ApiError(f"Order cancel failed: {data.get('errorMessage')}")
        return data
