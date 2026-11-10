"""Shared async HTTP transport and access-token management."""

from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx

from .exceptions import WeComAPIError, WeComHTTPError


class WeComClient:
    """Async client for a WeCom self-built application.

    Use as ``async with WeComClient(corp_id, secret) as client``. For tests or
    token brokers, an externally managed ``httpx.AsyncClient`` and token can be
    supplied. The client never logs credentials or token values.
    """

    BASE_URL = "https://qyapi.weixin.qq.com/cgi-bin"

    def __init__(
        self,
        corp_id: str,
        corp_secret: str,
        *,
        access_token: str | None = None,
        base_url: str = BASE_URL,
        http_client: httpx.AsyncClient | None = None,
        timeout: float = 20.0,
    ) -> None:
        self.corp_id = corp_id
        self.corp_secret = corp_secret
        self.base_url = base_url.rstrip("/")
        self._http = http_client or httpx.AsyncClient(timeout=timeout)
        self._owns_http = http_client is None
        self._access_token = access_token
        self._token_expires_at = float("inf") if access_token else 0.0
        self._token_lock = asyncio.Lock()
        self._modules: dict[str, Any] = {}

    async def __aenter__(self) -> "WeComClient":
        return self

    async def __aexit__(self, *_exc) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    async def access_token(self, *, force_refresh: bool = False) -> str:
        if not force_refresh and self._access_token and time.monotonic() < self._token_expires_at - 120:
            return self._access_token
        async with self._token_lock:
            if not force_refresh and self._access_token and time.monotonic() < self._token_expires_at - 120:
                return self._access_token
            try:
                response = await self._http.get(
                    f"{self.base_url}/gettoken",
                    params={"corpid": self.corp_id, "corpsecret": self.corp_secret},
                )
                response.raise_for_status()
                data = response.json()
            except (httpx.HTTPError, ValueError) as exc:
                raise WeComHTTPError("Unable to obtain WeCom access token") from exc
            self._raise_api_error(data)
            self._access_token = data["access_token"]
            self._token_expires_at = time.monotonic() + int(data.get("expires_in", 7200))
            return self._access_token

    @staticmethod
    def _raise_api_error(data: Any) -> None:
        if isinstance(data, dict) and data.get("errcode", 0) != 0:
            code = int(data["errcode"])
            raise WeComAPIError(
                f"WeCom API error {code}: {data.get('errmsg', 'unknown error')}",
                errcode=code,
                payload=data,
            )

    async def post(self, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """POST JSON to a cgi-bin path; refresh the token once on expiry."""
        token = await self.access_token()
        url = f"{self.base_url}/{path.lstrip('/')}"
        for attempt in range(2):
            try:
                response = await self._http.post(url, params={"access_token": token}, json=payload or {})
                response.raise_for_status()
                data = response.json()
            except (httpx.HTTPError, ValueError) as exc:
                raise WeComHTTPError(f"WeCom request failed: {path}") from exc
            try:
                self._raise_api_error(data)
            except WeComAPIError as exc:
                if exc.errcode == 42001 and attempt == 0:
                    token = await self.access_token(force_refresh=True)
                    continue
                raise
            if not isinstance(data, dict):
                raise WeComHTTPError(f"Unexpected response from WeCom: {path}")
            return data
        raise WeComHTTPError(f"WeCom request failed after token refresh: {path}")

    async def get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """GET JSON from APIs documented with the GET method."""
        token = await self.access_token()
        query = {"access_token": token, **(params or {})}
        try:
            response = await self._http.get(f"{self.base_url}/{path.lstrip('/')}", params=query)
            response.raise_for_status()
            data = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise WeComHTTPError(f"WeCom request failed: {path}") from exc
        self._raise_api_error(data)
        if not isinstance(data, dict):
            raise WeComHTTPError(f"Unexpected response from WeCom: {path}")
        return data

    @property
    def meetings(self):
        from .modules.meetings import MeetingsAPI
        return self._modules.setdefault("meetings", MeetingsAPI(self))

    @property
    def meeting_rooms(self):
        from .modules.meeting_rooms import MeetingRoomsAPI
        return self._modules.setdefault("meeting_rooms", MeetingRoomsAPI(self))

    @property
    def smart_sheets(self):
        from .modules.smart_sheets import SmartSheetsAPI
        return self._modules.setdefault("smart_sheets", SmartSheetsAPI(self))

    @property
    def forms(self):
        from .modules.forms import FormsAPI
        return self._modules.setdefault("forms", FormsAPI(self))

    @property
    def contacts(self):
        from .modules.contacts import ContactsAPI
        return self._modules.setdefault("contacts", ContactsAPI(self))

    @property
    def messages(self):
        from .modules.messages import MessagesAPI
        return self._modules.setdefault("messages", MessagesAPI(self))

    @property
    def media(self):
        from .modules.media import MediaAPI
        return self._modules.setdefault("media", MediaAPI(self))
