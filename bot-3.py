"""
Telegram forwarding engine.
Uses python-telegram-bot in pure requests mode (no webhooks needed).
"""
import asyncio
import logging
import time
from datetime import datetime, timezone

import httpx

import database as db

logger = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"


# ── Low-level API helpers ─────────────────────────────────────────────────────

async def _api_call(token: str, method: str, payload: dict, timeout: int = 30) -> dict:
    url = TELEGRAM_API.format(token=token, method=method)
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(url, json=payload)
        return resp.json()


async def validate_bot_token(token: str) -> dict | None:
    """Returns bot info dict if valid, None otherwise."""
    result = await _api_call(token, "getMe", {})
    if result.get("ok"):
        return result["result"]
    return None


async def get_chat_info(token: str, chat_id: str) -> dict | None:
    result = await _api_call(token, "getChat", {"chat_id": chat_id})
    if result.get("ok"):
        return result["result"]
    return None


async def send_message(token: str, chat_id: str, text: str,
                       parse_mode: str = "HTML") -> tuple[bool, str | None]:
    """
    Returns (success, error_reason).
    Handles rate limit (429) with one automatic backoff.
    """
    payload = {"chat_id": chat_id, "text": text}
    if parse_mode:
        payload["parse_mode"] = parse_mode

    for attempt in range(2):
        result = await _api_call(token, "sendMessage", payload)
        if result.get("ok"):
            return True, None

        err = result.get("description", "Unknown error")
        err_code = result.get("error_code", 0)

        # Rate limited — wait and retry once
        if err_code == 429 and attempt == 0:
            retry_after = result.get("parameters", {}).get("retry_after", 10)
            logger.warning("Rate limited on %s — waiting %ds", chat_id, retry_after)
            await asyncio.sleep(retry_after)
            continue

        # Permanent errors — don't retry
        if err_code in (400, 403):
            return False, err

        return False, err

    return False, "Max retries exceeded (rate limit)"


# ── Forwarding task ───────────────────────────────────────────────────────────

async def run_campaign(campaign_id: int, notify_admin_fn=None) -> dict:
    """
    Execute one pass of a campaign: send message to all assigned groups.
    Returns summary dict.
    """
    campaign = db.get_campaign(campaign_id)
    if not campaign:
        return {"error": "Campaign not found"}

    account = db.get_account(campaign["account_id"])
    if not account:
        return {"error": "Account not found"}

    token = account["bot_token"]
    text = campaign["message_text"]
    groups = campaign.get("groups", [])

    results = {"success": 0, "failed": 0, "errors": []}

    for group in groups:
        if not group.get("is_active", 1) or not group.get("is_accessible", 1):
            continue

        chat_id = group["chat_id"]
        ok, reason = await send_message(token, chat_id, text)

        db.log_message(
            campaign_id=campaign_id,
            group_id=group["id"],
            account_id=account["id"],
            status="success" if ok else "failed",
            error_reason=reason,
            message_preview=text,
        )

        if ok:
            results["success"] += 1
        else:
            results["failed"] += 1
            err_info = {
                "group_name": group.get("group_name", chat_id),
                "chat_id": chat_id,
                "reason": reason,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "campaign": campaign["name"],
            }
            results["errors"].append(err_info)

            # Mark inaccessible on permanent errors
            if reason and any(x in reason for x in
                              ("chat not found", "bot was kicked", "not a member",
                               "CHAT_WRITE_FORBIDDEN", "have no rights")):
                db.mark_group_inaccessible(group["id"])
                logger.warning("Marked group %s inaccessible: %s", chat_id, reason)

            # Notify admin
            if notify_admin_fn:
                await notify_admin_fn(err_info)

        # Polite inter-group delay (Telegram recommends ≥ 1 msg/sec per chat)
        await asyncio.sleep(1.2)

    return results


# ── Group discovery helper ────────────────────────────────────────────────────

async def fetch_group_info(token: str, chat_id: str) -> dict | None:
    info = await get_chat_info(token, chat_id)
    if not info:
        return None
    return {
        "chat_id": str(info.get("id", chat_id)),
        "group_name": info.get("title") or info.get("username") or str(chat_id),
        "group_type": info.get("type", "group"),
        "member_count": info.get("member_count"),
    }
