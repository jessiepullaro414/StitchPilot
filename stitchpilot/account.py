"""StitchPilot Plus: the AI features run through StitchPilot's AI service, included in the subscription.

- Accounts, the subscription and billing live on stitchpilot.jessiescars.com (SHOP_URL).
- This computer is linked to an account once ("Connect"): the app shows a short code, the person
  approves it in the browser, and the app keeps the device token it is given.
- AI requests go to the AI gateway (GATEWAY_URL) with that token. The gateway checks the account's
  allowance (a few free tries, or the monthly Plus allowance) and calls Claude / OpenAI itself, so
  no API keys live in the app.
"""
from __future__ import annotations

import json
import os
import platform
import urllib.error
import urllib.request

SHOP_URL = os.environ.get("STITCHPILOT_SHOP_URL", "https://stitchpilot.jessiescars.com").rstrip("/")
# Filled in from the gateway stack's output when it's deployed (deploy/aws/gateway.yaml).
GATEWAY_URL = os.environ.get("STITCHPILOT_GATEWAY_URL", "").rstrip("/")


class AccountError(Exception):
    """Something people can act on (shown as is)."""


class NeedsAccount(AccountError):
    """This computer isn't connected to an account (or was disconnected)."""


class OutOfAllowance(AccountError):
    """The free tries or this month's Plus allowance are used up."""

    def __init__(self, message: str, plan: str = "free"):
        super().__init__(message)
        self.plan = plan


def device_name() -> str:
    return (platform.node() or "StitchPilot")[:60]


def _call(method: str, path: str, payload: dict | None = None, token: str | None = None, timeout: float = 20):
    req = urllib.request.Request(
        SHOP_URL + path, method=method, data=json.dumps(payload).encode() if payload is not None else None,
        headers={"content-type": "application/json", "user-agent": "StitchPilot",
                 **({"authorization": f"Bearer {token}"} if token else {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read() or b"{}").get("error")
        except ValueError:
            msg = None
        if e.code == 401:
            raise NeedsAccount(msg or "This computer isn't connected to an account.")
        raise AccountError(msg or f"stitchpilot.jessiescars.com answered {e.code}. Try again in a moment.")
    except (urllib.error.URLError, TimeoutError, OSError):
        raise AccountError("Couldn't reach stitchpilot.jessiescars.com. Check your internet connection.")


def start_link() -> dict:
    """{code, pollSecret, url}: show the code, open the url, then poll_link until linked."""
    return _call("POST", "/api/stitchpilot/link/start", {"deviceName": device_name()})


def poll_link(code: str, poll_secret: str) -> dict:
    """{"status": "pending" | "expired"} or {"status": "linked", "token": ...}."""
    return _call("POST", "/api/stitchpilot/link/poll", {"code": code, "pollSecret": poll_secret})


def me(token: str) -> dict:
    """Who this computer is connected to: email, plan ("plus"/"free"), usage and limits."""
    return _call("GET", "/api/stitchpilot/me", token=token)


def _gateway() -> str:
    if not GATEWAY_URL:
        raise AccountError("StitchPilot's AI service isn't available in this version yet.")
    return GATEWAY_URL


def anthropic_client(token: str):
    """Claude through the gateway: same SDK, the device token in place of an API key."""
    import anthropic

    return anthropic.Anthropic(api_key=token, base_url=_gateway() + "/anthropic", max_retries=1, timeout=300)


def openai_client(token: str):
    """OpenAI pictures through the gateway."""
    import openai

    return openai.OpenAI(api_key=token, base_url=_gateway() + "/openai/v1", max_retries=1, timeout=300)


def gateway_error(status: int | None, body) -> AccountError | None:
    """The gateway's own refusals (not connected / out of allowance), from an SDK error's status and body."""
    err = (body or {}).get("error", body) if isinstance(body, dict) else {}
    err = err if isinstance(err, dict) else {}
    message = err.get("message") or ""
    if status == 402 or err.get("type") == "quota_exceeded":
        plan = (err.get("stitchpilot") or {}).get("plan", "free")
        return OutOfAllowance(message or "You've used your AI allowance.", plan)
    if status == 401:
        return NeedsAccount(message or "Connect StitchPilot to your account first.")
    return None
