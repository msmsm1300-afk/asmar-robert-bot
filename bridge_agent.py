"""Outbound Bridge Agent for Render -> local Chrome/iChancy communication.

Run this on the intermediary computer, not on Render. The agent polls Render
outbound, executes official Agent API calls from a persistent browser context,
and posts sanitized results back. It never exposes a listening port.
"""

from __future__ import annotations

import base64
import json
import os
import secrets
import time
from pathlib import Path
from typing import Any, Dict, Optional

import requests
from cryptography.fernet import Fernet, InvalidToken
from playwright.sync_api import sync_playwright


class BridgeError(RuntimeError):
    pass


class BrowserIchancyClient:
    BASE_URL = "https://agents.ichancy.com"

    def __init__(self, context):
        self.context = context
        self.page = context.pages[0] if context.pages else context.new_page()
        self.page.goto(self.BASE_URL, wait_until="domcontentloaded", timeout=30000)

    def _fetch(self, path: str, body: Dict[str, Any], token: Optional[str] = None) -> Dict[str, Any]:
        result = self.page.evaluate(
            """async ({url, body, token}) => {
                const headers = {'Content-Type': 'application/json'};
                if (token) headers['Authorization'] = `Bearer ${token}`;
                const response = await fetch(url, {
                    method: 'POST', credentials: 'include', headers,
                    body: JSON.stringify(body)
                });
                const text = await response.text();
                let data;
                try { data = JSON.parse(text); } catch (_) { data = {result: false, notification: []}; }
                return {status: response.status, server: response.headers.get('server') || '', data};
            }""",
            {"url": f"{self.BASE_URL}/{path.lstrip('/')}", "body": body, "token": token},
        )
        if result.get("status") >= 400:
            raise BridgeError(f"ichancy_http_{result.get('status')}")
        return result.get("data") or {}

    def sign_in(self, username: str, password: str) -> Dict[str, Any]:
        return self._fetch("global/api/UserApi/signIn", {"username": username, "password": password})

    def refresh(self, refresh_token: str) -> Dict[str, Any]:
        return self._fetch("global/api/UserApi/refreshToken", {"refreshToken": refresh_token})

    def authorized(self, path: str, body: Dict[str, Any], access_token: str) -> Dict[str, Any]:
        payload = self._fetch(path, body, access_token)
        return payload


class TokenStore:
    def __init__(self, path: str):
        self.path = Path(path)
        key = os.environ.get("BRIDGE_TOKEN_KEY", "").strip()
        if not key:
            raise BridgeError("BRIDGE_TOKEN_KEY is required for encrypted token storage")
        try:
            self.fernet = Fernet(key.encode())
        except Exception as exc:
            raise BridgeError("BRIDGE_TOKEN_KEY is not a valid Fernet key") from exc

    def load(self) -> Optional[Dict[str, Any]]:
        if not self.path.exists():
            return None
        try:
            return json.loads(self.fernet.decrypt(self.path.read_bytes()).decode())
        except (InvalidToken, ValueError, OSError):
            return None

    def save(self, value: Dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        encrypted = self.fernet.encrypt(json.dumps(value).encode())
        tmp = self.path.with_suffix(".tmp")
        tmp.write_bytes(encrypted)
        os.chmod(tmp, 0o600)
        tmp.replace(self.path)


class BridgeAgent:
    def __init__(self):
        self.render_url = os.environ["RENDER_BRIDGE_URL"].rstrip("/")
        self.bridge_key = os.environ["BRIDGE_SHARED_SECRET"]
        self.device_id = os.environ.get("BRIDGE_DEVICE_ID") or secrets.token_hex(12)
        self.device_name = os.environ.get("BRIDGE_DEVICE_NAME", os.environ.get("COMPUTERNAME", "bridge-device"))
        self.profile_dir = os.environ.get("ICHANCY_CHROME_PROFILE", "./ichancy-chrome-profile")
        self.token_store = TokenStore(os.environ.get("BRIDGE_TOKEN_FILE", "./bridge-tokens.enc"))
        self.ichancy_username = os.environ.get("ICHANCY_USERNAME", "")
        self.ichancy_password = os.environ.get("ICHANCY_PASSWORD", "")
        self.state = self.token_store.load() or {}
        self.session = requests.Session()
        self.session.headers.update({"X-Bridge-Key": self.bridge_key})
        self.last_heartbeat = 0.0

    def _render(self, method: str, path: str, **kwargs):
        response = self.session.request(method, f"{self.render_url}{path}", timeout=20, **kwargs)
        response.raise_for_status()
        return response.json()

    def heartbeat(self, ichancy_connected: bool):
        self._render("POST", "/bridge/v1/heartbeat", json={
            "device_id": self.device_id,
            "device_name": self.device_name,
            "ichancy_connected": ichancy_connected,
            "metadata": {"agent": "bridge-agent-v1"},
        })
        self.last_heartbeat = time.time()

    def _ensure_token(self, client: BrowserIchancyClient):
        now = time.time()
        if self.state.get("access_token") and now < self.state.get("access_expires_at", 0) - 30:
            return self.state["access_token"]
        refresh = self.state.get("refresh_token")
        if refresh and now < self.state.get("refresh_expires_at", 0):
            payload = client.refresh(refresh)
            result = payload.get("result") or {}
            if result.get("accessToken") and result.get("refreshToken"):
                self._save_tokens(result)
                return result["accessToken"]
        if not self.ichancy_username or not self.ichancy_password:
            raise BridgeError("ICHANCY_USERNAME and ICHANCY_PASSWORD are required on bridge")
        payload = client.sign_in(self.ichancy_username, self.ichancy_password)
        result = payload.get("result") or {}
        if not result.get("accessToken") or not result.get("refreshToken"):
            raise BridgeError("ichancy_sign_in_failed")
        self._save_tokens(result)
        return result["accessToken"]

    def _save_tokens(self, result):
        now = time.time()
        completed = self.state.get("completed_requests", {})
        self.state = {
            "access_token": result["accessToken"],
            "refresh_token": result["refreshToken"],
            "access_expires_at": now + 3600,
            "refresh_expires_at": now + 7 * 24 * 3600,
            "completed_requests": completed,
        }
        self.token_store.save(self.state)

    def execute(self, job: Dict[str, Any], client: BrowserIchancyClient) -> Dict[str, Any]:
        job_type = job["job_type"]
        payload = job.get("payload") or {}
        request_id = str(job.get("request_id", ""))
        completed = self.state.setdefault("completed_requests", {})
        if request_id and request_id in completed:
            return completed[request_id]
        token = self._ensure_token(client)
        routes = {
            "get_players": ("global/api/UserApi/getPlayersForCurrentAgent", payload),
            "get_balance": ("global/api/UserApi/getPlayerBalanceById", {"playerId": str(payload["player_id"])}),
            "register_player": ("global/api/UserApi/registerPlayer", {"player": payload["player"]}),
            "deposit": ("global/api/UserApi/depositToPlayer", payload),
            "withdraw": ("global/api/UserApi/withdrawFromPlayer", payload),
        }
        if job_type not in routes:
            raise BridgeError("unsupported_job_type")
        path, body = routes[job_type]
        result = client.authorized(path, body, token)
        if result.get("result") == "ex":
            # One refresh + retry only; never repeat financial jobs more than once.
            refresh = self.state.get("refresh_token")
            if not refresh:
                raise BridgeError("refresh_token_missing")
            refreshed = client.refresh(refresh).get("result") or {}
            if not refreshed.get("accessToken"):
                raise BridgeError("refresh_failed")
            self._save_tokens(refreshed)
            result = client.authorized(path, body, refreshed["accessToken"])
        if result.get("result") == "ex":
            raise BridgeError("access_token_expired_after_refresh")
        if request_id and job_type in {"deposit", "withdraw"}:
            completed[request_id] = result
            # Keep a bounded local ledger so a crash between iChancy success and
            # Render acknowledgement cannot repeat the same financial command.
            self.state["completed_requests"] = dict(list(completed.items())[-1000:])
            self.token_store.save(self.state)
        return result

    def run(self):
        interval = float(os.environ.get("BRIDGE_POLL_SECONDS", "3"))
        with sync_playwright() as playwright:
            context = playwright.chromium.launch_persistent_context(
                self.profile_dir,
                headless=False,
                viewport={"width": 1280, "height": 900},
            )
            client = BrowserIchancyClient(context)
            try:
                while True:
                    ichancy_connected = True
                    try:
                        if time.time() - self.last_heartbeat >= max(5.0, interval):
                            self.heartbeat(True)
                        job = self._render("POST", "/bridge/v1/jobs/next", json={"device_id": self.device_id}).get("job")
                        if not job:
                            time.sleep(interval)
                            continue
                        try:
                            result = self.execute(job, client)
                            self._render("POST", f"/bridge/v1/jobs/{job['job_id']}/complete", json={
                                "device_id": self.device_id, "status": "succeeded", "result": result,
                            })
                        except Exception as exc:
                            self._render("POST", f"/bridge/v1/jobs/{job['job_id']}/complete", json={
                                "device_id": self.device_id, "status": "failed", "error": type(exc).__name__, "result": {},
                            })
                    except Exception:
                        ichancy_connected = False
                        time.sleep(interval)
                    finally:
                        if time.time() - self.last_heartbeat >= max(5.0, interval):
                            try:
                                self.heartbeat(ichancy_connected)
                            except Exception:
                                pass
            finally:
                context.close()


if __name__ == "__main__":
    BridgeAgent().run()
