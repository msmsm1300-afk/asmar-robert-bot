"""Official iChancy Agent API client.

This module intentionally exposes read-only player operations first. Financial
operations are implemented as explicit methods but are not called by the bot
until connectivity, permissions, idempotency, and audit logging are verified.
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional

import requests


class IchancyError(RuntimeError):
    """Base error with a user-safe message and optional HTTP status."""

    def __init__(self, message: str, status_code: Optional[int] = None):
        super().__init__(message)
        self.status_code = status_code


class IchancyCloudflareError(IchancyError):
    """The hosting/network edge rejected a server-to-server request."""


class IchancyAuthError(IchancyError):
    """Credentials or token refresh are invalid."""


class IchancyPermissionError(IchancyError):
    """The Agent lacks a specific API permission."""


@dataclass
class TokenPair:
    access_token: str
    refresh_token: str
    access_expires_at: float
    refresh_expires_at: float


class IchancyAgentClient:
    """Thread-safe client for the official UserApi Agent endpoints.

    The API permits only one valid token pair per Agent. A single in-memory
    token manager is therefore shared by all requests in this process.
    """

    def __init__(
        self,
        username: Optional[str] = None,
        password: Optional[str] = None,
        base_url: Optional[str] = None,
        session: Optional[requests.Session] = None,
        timeout: float = 8.0,
    ) -> None:
        self.username = (username or os.environ.get("ICHANCY_USERNAME", "")).strip()
        self.password = password or os.environ.get("ICHANCY_PASSWORD", "")
        self.base_url = (
            base_url or os.environ.get("ICHANCY_API_BASE_URL", "https://agents.ichancy.com")
        ).rstrip("/")
        self.timeout = timeout
        self.session = session or requests.Session()
        self._token: Optional[TokenPair] = None
        self._lock = threading.RLock()

    def _url(self, path: str) -> str:
        return f"{self.base_url}/{path.lstrip('/')}"

    @staticmethod
    def _safe_notification(payload: Any) -> str:
        if not isinstance(payload, dict):
            return "iChancy API returned an unexpected response"
        notifications = payload.get("notification") or []
        if isinstance(notifications, list):
            messages = [
                str(item.get("content"))
                for item in notifications
                if isinstance(item, dict) and item.get("content")
            ]
            if messages:
                return "; ".join(messages)[:240]
        return "iChancy API request failed"

    def _post(self, path: str, body: Dict[str, Any], headers: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        try:
            response = self.session.post(
                self._url(path),
                json=body,
                headers={"Content-Type": "application/json", **(headers or {})},
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise IchancyError(f"iChancy network error: {type(exc).__name__}") from exc

        if response.status_code == 403 and response.headers.get("server", "").lower() == "cloudflare":
            raise IchancyCloudflareError("iChancy blocked the server-to-server request at Cloudflare", 403)
        if response.status_code == 401:
            raise IchancyAuthError("iChancy authentication failed or token expired", 401)
        if response.status_code == 403:
            raise IchancyPermissionError("iChancy Agent lacks permission for this operation", 403)
        if response.status_code >= 400:
            raise IchancyError(f"iChancy HTTP error {response.status_code}", response.status_code)

        try:
            payload = response.json()
        except ValueError as exc:
            raise IchancyError("iChancy returned invalid JSON", response.status_code) from exc
        if not isinstance(payload, dict):
            raise IchancyError("iChancy returned an unexpected payload", response.status_code)
        return payload

    def _token_from_payload(self, payload: Dict[str, Any]) -> TokenPair:
        result = payload.get("result")
        if not isinstance(result, dict) or not result.get("accessToken") or not result.get("refreshToken"):
            raise IchancyAuthError(self._safe_notification(payload))
        now = time.time()
        return TokenPair(
            access_token=str(result["accessToken"]),
            refresh_token=str(result["refreshToken"]),
            access_expires_at=now + 3600,
            refresh_expires_at=now + 7 * 24 * 3600,
        )

    def sign_in(self) -> None:
        if not self.username or not self.password:
            raise IchancyAuthError("ICHANCY_USERNAME and ICHANCY_PASSWORD are required")
        payload = self._post(
            "global/api/UserApi/signIn",
            {"username": self.username, "password": self.password},
        )
        if payload.get("result") is False:
            raise IchancyAuthError(self._safe_notification(payload), 401)
        self._token = self._token_from_payload(payload)

    def refresh_token(self) -> None:
        with self._lock:
            if not self._token or time.time() >= self._token.refresh_expires_at:
                self.sign_in()
                return
            payload = self._post(
                "global/api/UserApi/refreshToken",
                {"refreshToken": self._token.refresh_token},
            )
            self._token = self._token_from_payload(payload)

    def _authorized_post(self, path: str, body: Dict[str, Any]) -> Dict[str, Any]:
        with self._lock:
            if not self._token or time.time() >= self._token.access_expires_at - 30:
                self.refresh_token()
            assert self._token is not None
            headers = {"Authorization": f"Bearer {self._token.access_token}"}
            payload = self._post(path, body, headers)
            # The API may return an application-level expired-token envelope.
            if payload.get("result") == "ex":
                self.refresh_token()
                assert self._token is not None
                payload = self._post(
                    path,
                    body,
                    {"Authorization": f"Bearer {self._token.access_token}"},
                )
            return payload

    def get_players(self, username: Optional[str] = None, player_id: Optional[str] = None) -> Dict[str, Any]:
        filters: Dict[str, Any] = {"withoutTotalCount": {"action": "=", "value": True}}
        if player_id:
            filters["playerId"] = {"action": "=", "value": str(player_id), "valueLabel": str(player_id)}
        elif username:
            filters["userName"] = {"action": "like", "value": username, "valueLabel": username}
        body = {"start": 0, "limit": 20, "filter": filters, "isNextPage": False}
        return self._authorized_post("global/api/UserApi/getPlayersForCurrentAgent", body)

    def get_player_balance(self, player_id: str) -> Dict[str, Any]:
        return self._authorized_post(
            "global/api/UserApi/getPlayerBalanceById", {"playerId": str(player_id)}
        )

    def register_player(self, player: Dict[str, Any]) -> Dict[str, Any]:
        return self._authorized_post("global/api/UserApi/registerPlayer", {"player": player})

    def deposit_to_player(self, player_id: str, amount: float, currency_code: str, comment: str, money_status: int = 5) -> Dict[str, Any]:
        return self._authorized_post(
            "global/api/UserApi/depositToPlayer",
            {
                "amount": amount,
                "comment": comment,
                "playerId": str(player_id),
                "currencyCode": currency_code,
                "currency": currency_code,
                "moneyStatus": money_status,
            },
        )

    def withdraw_from_player(self, player_id: str, amount: float, currency_code: str, comment: str, money_status: int = 5) -> Dict[str, Any]:
        return self._authorized_post(
            "global/api/UserApi/withdrawFromPlayer",
            {
                "amount": -abs(amount),
                "comment": comment,
                "playerId": str(player_id),
                "currencyCode": currency_code,
                "currency": currency_code,
                "moneyStatus": money_status,
            },
        )
