"""
Database layer — all business-level read/write operations.
Uses firebase.py for low-level access.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone, timedelta
from functools import wraps
from typing import Any, Dict, List, Optional, Tuple

from cachetools import TTLCache

from firebase import (
    fb_get, fb_set, fb_update, fb_push, fb_delete, fb_transaction, get_ref
)
from utils.logger import logger
from utils.helpers import get_utc_now, generate_request_id, sanitize_firebase_key
from utils.constants import (
    DB_USERS, DB_ADMINS, DB_REFERRALS, DB_CLAIMS, DB_SETTINGS,
    DB_FORCE_JOIN, DB_STATISTICS, DB_LOGS, DB_BROADCAST_HISTORY,
    CLAIM_PENDING, CLAIM_APPROVED, CLAIM_REJECTED,
    USER_ACTIVE, USER_BLOCKED, DEFAULT_SETTINGS,
)
from config import config

# ── In-memory caches ──────────────────────────────────────────────────────────
_user_cache: TTLCache = TTLCache(maxsize=1000, ttl=config.CACHE_USER_TTL)
_settings_cache: TTLCache = TTLCache(maxsize=1, ttl=config.CACHE_SETTINGS_TTL)
_channels_cache: TTLCache = TTLCache(maxsize=1, ttl=config.CACHE_CHANNELS_TTL)
_admins_cache: TTLCache = TTLCache(maxsize=1, ttl=60)

# Lock to prevent duplicate writes for the same user concurrently
_user_locks: Dict[int, asyncio.Lock] = {}


def _get_user_lock(user_id: int) -> asyncio.Lock:
    if user_id not in _user_locks:
        _user_locks[user_id] = asyncio.Lock()
    return _user_locks[user_id]


# ─────────────────────────────────────────────────────────────────────────────
# SETTINGS
# ─────────────────────────────────────────────────────────────────────────────

async def get_settings() -> Dict[str, Any]:
    """Return bot settings, initializing defaults if missing."""
    if "settings" in _settings_cache:
        return _settings_cache["settings"]

    loop = asyncio.get_event_loop()
    data = await loop.run_in_executor(None, fb_get, DB_SETTINGS)

    if not data:
        await loop.run_in_executor(None, fb_set, DB_SETTINGS, DEFAULT_SETTINGS)
        data = dict(DEFAULT_SETTINGS)

    # Ensure all default keys exist
    merged = dict(DEFAULT_SETTINGS)
    merged.update(data)

    _settings_cache["settings"] = merged
    return merged


async def update_settings(updates: Dict[str, Any]) -> bool:
    """Update specific settings fields and invalidate cache."""
    loop = asyncio.get_event_loop()
    ok = await loop.run_in_executor(None, fb_update, DB_SETTINGS, updates)
    if ok:
        _settings_cache.pop("settings", None)
    return ok


# ─────────────────────────────────────────────────────────────────────────────
# ADMINS
# ─────────────────────────────────────────────────────────────────────────────

async def get_all_admin_ids() -> List[int]:
    """Return list of all admin Telegram IDs (Firebase + env)."""
    if "admins" in _admins_cache:
        return _admins_cache["admins"]

    loop = asyncio.get_event_loop()
    data = await loop.run_in_executor(None, fb_get, DB_ADMINS)

    ids: List[int] = list(config.get_initial_admin_ids())

    if data and isinstance(data, dict):
        for uid_str in data.keys():
            try:
                uid = int(uid_str)
                if uid not in ids:
                    ids.append(uid)
            except ValueError:
                pass

    _admins_cache["admins"] = ids
    return ids


async def is_admin(user_id: int) -> bool:
    """Check if a user ID is in the admin list."""
    admins = await get_all_admin_ids()
    return user_id in admins


async def add_admin(user_id: int, added_by: int) -> bool:
    """Add a new admin by Telegram user ID."""
    loop = asyncio.get_event_loop()
    now = get_utc_now().isoformat()
    ok = await loop.run_in_executor(
        None, fb_set, f"{DB_ADMINS}/{user_id}",
        {"added_by": added_by, "added_at": now}
    )
    if ok:
        _admins_cache.pop("admins", None)
        await _log_action("admin_added", {"target_id": user_id, "by": added_by})
    return ok


async def remove_admin(user_id: int, removed_by: int) -> bool:
    """Remove an admin by Telegram user ID."""
    loop = asyncio.get_event_loop()
    ok = await loop.run_in_executor(None, fb_delete, f"{DB_ADMINS}/{user_id}")
    if ok:
        _admins_cache.pop("admins", None)
        await _log_action("admin_removed", {"target_id": user_id, "by": removed_by})
    return ok


async def get_admins_data() -> Dict[str, Any]:
    """Return full admins node dict."""
    loop = asyncio.get_event_loop()
    data = await loop.run_in_executor(None, fb_get, DB_ADMINS)
    return data or {}


# ─────────────────────────────────────────────────────────────────────────────
# FORCE JOIN CHANNELS
# ─────────────────────────────────────────────────────────────────────────────

async def get_force_join_channels() -> List[Dict[str, Any]]:
    """Return list of enabled force-join channels."""
    if "channels" in _channels_cache:
        return _channels_cache["channels"]

    loop = asyncio.get_event_loop()
    data = await loop.run_in_executor(None, fb_get, DB_FORCE_JOIN)

    channels: List[Dict[str, Any]] = []
    if data and isinstance(data, dict):
        for key, val in data.items():
            if isinstance(val, dict):
                val["_key"] = key
                channels.append(val)

    _channels_cache["channels"] = channels
    return channels


async def get_all_force_join_channels() -> List[Dict[str, Any]]:
    """Return ALL channels including disabled ones (for admin panel)."""
    loop = asyncio.get_event_loop()
    data = await loop.run_in_executor(None, fb_get, DB_FORCE_JOIN)

    channels: List[Dict[str, Any]] = []
    if data and isinstance(data, dict):
        for key, val in data.items():
            if isinstance(val, dict):
                val["_key"] = key
                channels.append(val)
    return channels


async def add_force_join_channel(
    channel_id: str,
    channel_username: str,
    invite_link: str,
    added_by: int,
) -> bool:
    """Add a new force-join channel."""
    loop = asyncio.get_event_loop()
    now = get_utc_now().isoformat()
    safe_key = sanitize_firebase_key(channel_id.lstrip("-"))

    data = {
        "channel_id": channel_id,
        "channel_username": channel_username,
        "invite_link": invite_link,
        "status": True,
        "added_date": now,
        "added_by": added_by,
    }
    ok = await loop.run_in_executor(None, fb_set, f"{DB_FORCE_JOIN}/{safe_key}", data)
    if ok:
        _channels_cache.pop("channels", None)
        await _log_action("channel_added", {"channel_id": channel_id, "by": added_by})
    return ok


async def remove_force_join_channel(channel_key: str) -> bool:
    """Remove a force-join channel by its Firebase key."""
    loop = asyncio.get_event_loop()
    ok = await loop.run_in_executor(None, fb_delete, f"{DB_FORCE_JOIN}/{channel_key}")
    if ok:
        _channels_cache.pop("channels", None)
    return ok


async def toggle_force_join_channel(channel_key: str, enabled: bool) -> bool:
    """Enable or disable a force-join channel."""
    loop = asyncio.get_event_loop()
    ok = await loop.run_in_executor(
        None, fb_update, f"{DB_FORCE_JOIN}/{channel_key}", {"status": enabled}
    )
    if ok:
        _channels_cache.pop("channels", None)
    return ok


# ─────────────────────────────────────────────────────────────────────────────
# USERS
# ─────────────────────────────────────────────────────────────────────────────

def _build_user_path(user_id: int) -> str:
    return f"{DB_USERS}/{user_id}"


async def get_user(user_id: int) -> Optional[Dict[str, Any]]:
    """Return user data dict or None if not registered."""
    cache_key = f"user_{user_id}"
    if cache_key in _user_cache:
        return _user_cache[cache_key]

    loop = asyncio.get_event_loop()
    data = await loop.run_in_executor(None, fb_get, _build_user_path(user_id))

    if data:
        _user_cache[cache_key] = data
    return data


async def user_exists(user_id: int) -> bool:
    """Quick existence check."""
    return await get_user(user_id) is not None


async def create_user(
    user_id: int,
    username: Optional[str],
    full_name: str,
) -> Dict[str, Any]:
    """
    Create a new user record.
    Returns the created user dict.
    """
    now = get_utc_now().isoformat()
    user_data: Dict[str, Any] = {
        "user_id": user_id,
        "username": username or "",
        "full_name": full_name,
        "join_date": now,
        "last_active": now,
        "referral_count": 0,
        "referral_points": 0,
        "claim_count": 0,
        "total_claims": 0,
        "status": USER_ACTIVE,
        "blocked": False,
        "language": "en",
        "premium_claim_history_count": 0,
    }

    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, fb_set, _build_user_path(user_id), user_data)

    cache_key = f"user_{user_id}"
    _user_cache[cache_key] = user_data

    # Update statistics
    await _increment_stat("total_users", 1)
    today_key = get_utc_now().strftime("%Y-%m-%d")
    await _increment_stat(f"daily_users/{today_key}", 1)

    await _log_action("user_joined", {"user_id": user_id, "name": full_name})
    logger.info("New user registered: %s (%s)", full_name, user_id)

    return user_data


async def update_user_profile(
    user_id: int,
    username: Optional[str],
    full_name: str,
) -> None:
    """Update mutable profile fields and last_active timestamp."""
    loop = asyncio.get_event_loop()
    updates = {
        "username": username or "",
        "full_name": full_name,
        "last_active": get_utc_now().isoformat(),
    }
    await loop.run_in_executor(None, fb_update, _build_user_path(user_id), updates)
    _user_cache.pop(f"user_{user_id}", None)


async def update_user_last_active(user_id: int) -> None:
    """Touch last_active timestamp only."""
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(
        None, fb_update, _build_user_path(user_id),
        {"last_active": get_utc_now().isoformat()}
    )
    _user_cache.pop(f"user_{user_id}", None)


async def block_user(user_id: int, blocked_by: int) -> bool:
    """Mark a user as blocked."""
    loop = asyncio.get_event_loop()
    ok = await loop.run_in_executor(
        None, fb_update, _build_user_path(user_id),
        {"blocked": True, "status": USER_BLOCKED}
    )
    if ok:
        _user_cache.pop(f"user_{user_id}", None)
        await _log_action("user_blocked", {"user_id": user_id, "by": blocked_by})
    return ok


async def unblock_user(user_id: int, unblocked_by: int) -> bool:
    """Remove block from a user."""
    loop = asyncio.get_event_loop()
    ok = await loop.run_in_executor(
        None, fb_update, _build_user_path(user_id),
        {"blocked": False, "status": USER_ACTIVE}
    )
    if ok:
        _user_cache.pop(f"user_{user_id}", None)
        await _log_action("user_unblocked", {"user_id": user_id, "by": unblocked_by})
    return ok


async def get_all_user_ids() -> List[int]:
    """Return list of all user IDs (for broadcast)."""
    loop = asyncio.get_event_loop()
    data = await loop.run_in_executor(None, fb_get, DB_USERS)
    if not data or not isinstance(data, dict):
        return []
    return [int(uid) for uid in data.keys()]


async def get_user_count() -> int:
    """Return total number of registered users."""
    ids = await get_all_user_ids()
    return len(ids)


# ─────────────────────────────────────────────────────────────────────────────
# PENDING REFERRALS  (saved before force-join, consumed after join check passes)
# ─────────────────────────────────────────────────────────────────────────────

async def save_pending_referral(invitee_id: int, referrer_id: int) -> None:
    """
    Temporarily store a referral link parameter in Firebase so it survives
    the force-join detour.  Written at pending_referrals/{invitee_id}.
    """
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(
        None, fb_set,
        f"pending_referrals/{invitee_id}",
        {"referrer_id": referrer_id, "ts": get_utc_now().isoformat()},
    )


async def pop_pending_referral(invitee_id: int) -> Optional[int]:
    """
    Read and delete the pending referrer for *invitee_id*.
    Returns the referrer_id if one was stored, else None.
    """
    loop = asyncio.get_event_loop()
    data = await loop.run_in_executor(
        None, fb_get, f"pending_referrals/{invitee_id}"
    )
    if not data or not isinstance(data, dict):
        return None
    # Delete it so it can never fire twice
    await loop.run_in_executor(
        None, fb_delete, f"pending_referrals/{invitee_id}"
    )
    try:
        return int(data["referrer_id"])
    except (KeyError, ValueError, TypeError):
        return None


# ─────────────────────────────────────────────────────────────────────────────
# REFERRALS
# ─────────────────────────────────────────────────────────────────────────────

async def referral_exists(inviter_id: int, invitee_id: int) -> bool:
    """Check whether this specific referral pair already exists."""
    loop = asyncio.get_event_loop()
    val = await loop.run_in_executor(
        None, fb_get, f"{DB_REFERRALS}/{inviter_id}/{invitee_id}"
    )
    return val is not None


async def record_referral(inviter_id: int, invitee_id: int) -> bool:
    """
    Record a successful referral and award points to the inviter.
    Returns True if recorded, False if duplicate or error.
    """
    # Double-check for duplicate
    if await referral_exists(inviter_id, invitee_id):
        logger.warning(
            "Duplicate referral blocked: inviter=%s invitee=%s", inviter_id, invitee_id
        )
        return False

    loop = asyncio.get_event_loop()
    now = get_utc_now().isoformat()

    # Write referral node
    ref_data = {"invitee_id": invitee_id, "date": now}
    ok = await loop.run_in_executor(
        None, fb_set, f"{DB_REFERRALS}/{inviter_id}/{invitee_id}", ref_data
    )
    if not ok:
        return False

    # Atomically increment points and referral count
    settings = await get_settings()
    reward = int(settings.get("referral_reward", 1))

    ok = await loop.run_in_executor(
        None, _atomic_increment_referral, inviter_id, reward
    )

    if ok:
        _user_cache.pop(f"user_{inviter_id}", None)
        await _increment_stat("total_referrals", 1)
        await _log_action(
            "referral_success",
            {"inviter": inviter_id, "invitee": invitee_id, "reward": reward}
        )
        logger.info("Referral recorded: inviter=%s invitee=%s", inviter_id, invitee_id)

    return ok


def _atomic_increment_referral(user_id: int, reward: int) -> bool:
    """Synchronous atomic update for referral_count and referral_points."""
    try:
        from firebase_admin import db as fdb
        ref = fdb.reference(f"{DB_USERS}/{user_id}")

        def updater(current):
            if current is None:
                return None  # Abort if user doesn't exist
            current["referral_count"] = int(current.get("referral_count", 0)) + 1
            current["referral_points"] = int(current.get("referral_points", 0)) + reward
            return current

        ref.transaction(updater)
        return True
    except Exception as e:
        logger.error("Atomic referral increment failed for %s: %s", user_id, e)
        return False


# ─────────────────────────────────────────────────────────────────────────────
# CLAIMS
# ─────────────────────────────────────────────────────────────────────────────

async def has_pending_claim(user_id: int) -> bool:
    """Return True if user already has a pending claim request."""
    loop = asyncio.get_event_loop()
    data = await loop.run_in_executor(None, fb_get, DB_CLAIMS)
    if not data or not isinstance(data, dict):
        return False
    for claim in data.values():
        if (
            isinstance(claim, dict)
            and int(claim.get("user_id", 0)) == user_id
            and claim.get("status") == CLAIM_PENDING
        ):
            return True
    return False


async def create_claim_request(
    user_id: int,
    username: Optional[str],
    full_name: str,
    points_used: int,
) -> Optional[str]:
    """
    Create a new claim request.

    Returns:
        Unique request ID on success, None on failure.
    """
    # Prevent duplicate pending claims
    if await has_pending_claim(user_id):
        logger.warning("Duplicate claim attempt blocked for user %s", user_id)
        return None

    loop = asyncio.get_event_loop()
    now = get_utc_now()
    request_id = generate_request_id()

    claim_data = {
        "request_id": request_id,
        "user_id": user_id,
        "username": username or "",
        "full_name": full_name,
        "date": now.strftime("%Y-%m-%d"),
        "time": now.strftime("%H:%M:%S UTC"),
        "timestamp": now.isoformat(),
        "points_used": points_used,
        "status": CLAIM_PENDING,
    }

    ok = await loop.run_in_executor(None, fb_set, f"{DB_CLAIMS}/{request_id}", claim_data)
    if not ok:
        return None

    # Reset user referral count and points immediately
    ok2 = await loop.run_in_executor(
        None, fb_update, _build_user_path(user_id),
        {
            "referral_count": 0,
            "referral_points": 0,
            "claim_count": 0,
            "total_claims": _get_incremented_field(user_id, "total_claims"),
        }
    )
    _user_cache.pop(f"user_{user_id}", None)

    # Update statistics
    await _increment_stat("claims/pending", 1)
    await _increment_stat("claims/total", 1)
    await _log_action("claim_created", {"user_id": user_id, "request_id": request_id})
    logger.info("Claim request created: %s for user %s", request_id, user_id)

    return request_id


def _get_incremented_field(user_id: int, field: str) -> int:
    """Synchronously read and increment a numeric user field."""
    try:
        from firebase_admin import db as fdb
        val = fdb.reference(f"{DB_USERS}/{user_id}/{field}").get()
        return int(val or 0) + 1
    except Exception:
        return 1


async def reset_user_after_claim(user_id: int) -> None:
    """Reset referral_count and referral_points to 0 after claim."""
    loop = asyncio.get_event_loop()
    user = await get_user(user_id)
    if not user:
        return
    new_total = int(user.get("total_claims", 0)) + 1
    await loop.run_in_executor(
        None, fb_update, _build_user_path(user_id),
        {
            "referral_count": 0,
            "referral_points": 0,
            "claim_count": 0,
            "total_claims": new_total,
        }
    )
    _user_cache.pop(f"user_{user_id}", None)


async def get_pending_claims() -> List[Dict[str, Any]]:
    """Return all claims with status=pending."""
    loop = asyncio.get_event_loop()
    data = await loop.run_in_executor(None, fb_get, DB_CLAIMS)
    if not data or not isinstance(data, dict):
        return []
    return [
        v for v in data.values()
        if isinstance(v, dict) and v.get("status") == CLAIM_PENDING
    ]


async def get_all_claims() -> List[Dict[str, Any]]:
    """Return all claims sorted by timestamp desc."""
    loop = asyncio.get_event_loop()
    data = await loop.run_in_executor(None, fb_get, DB_CLAIMS)
    if not data or not isinstance(data, dict):
        return []
    claims = [v for v in data.values() if isinstance(v, dict)]
    claims.sort(key=lambda x: x.get("timestamp", ""), reverse=True)
    return claims


async def update_claim_status(request_id: str, status: str, admin_id: int) -> bool:
    """Update a claim's status (approved/rejected)."""
    loop = asyncio.get_event_loop()
    now = get_utc_now().isoformat()
    ok = await loop.run_in_executor(
        None, fb_update, f"{DB_CLAIMS}/{request_id}",
        {"status": status, "reviewed_by": admin_id, "reviewed_at": now}
    )
    if ok:
        stat_key = "approved" if status == CLAIM_APPROVED else "rejected"
        await _increment_stat(f"claims/{stat_key}", 1)
        await _increment_stat("claims/pending", -1)
        await _log_action(
            f"claim_{status}",
            {"request_id": request_id, "by": admin_id}
        )
    return ok


async def delete_claim(request_id: str) -> bool:
    """Delete a claim request."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, fb_delete, f"{DB_CLAIMS}/{request_id}")


# ─────────────────────────────────────────────────────────────────────────────
# STATISTICS
# ─────────────────────────────────────────────────────────────────────────────

async def get_statistics() -> Dict[str, Any]:
    """Return full statistics node."""
    loop = asyncio.get_event_loop()
    data = await loop.run_in_executor(None, fb_get, DB_STATISTICS)
    return data or {}


async def get_dashboard_stats() -> Dict[str, Any]:
    """Aggregate stats for admin dashboard."""
    loop = asyncio.get_event_loop()

    stats_raw, users_raw, claims_raw = await asyncio.gather(
        loop.run_in_executor(None, fb_get, DB_STATISTICS),
        loop.run_in_executor(None, fb_get, DB_USERS),
        loop.run_in_executor(None, fb_get, DB_CLAIMS),
    )

    now = get_utc_now()
    today = now.strftime("%Y-%m-%d")
    yesterday = (now - timedelta(days=1)).strftime("%Y-%m-%d")

    # Daily stats
    daily = (stats_raw or {}).get("daily_users", {})
    today_count = int((daily or {}).get(today, 0))
    yesterday_count = int((daily or {}).get(yesterday, 0))

    # Weekly / monthly
    weekly = sum(
        int(v) for k, v in (daily or {}).items()
        if k >= (now - timedelta(days=7)).strftime("%Y-%m-%d")
    )
    monthly = sum(
        int(v) for k, v in (daily or {}).items()
        if k >= (now - timedelta(days=30)).strftime("%Y-%m-%d")
    )

    total_users = len(users_raw) if users_raw else 0
    blocked_users = sum(
        1 for u in (users_raw or {}).values()
        if isinstance(u, dict) and u.get("blocked")
    )

    # Claims breakdown
    pending = approved = rejected = 0
    if claims_raw and isinstance(claims_raw, dict):
        for c in claims_raw.values():
            if not isinstance(c, dict):
                continue
            s = c.get("status", "")
            if s == CLAIM_PENDING:
                pending += 1
            elif s == CLAIM_APPROVED:
                approved += 1
            elif s == CLAIM_REJECTED:
                rejected += 1

    return {
        "total_users": total_users,
        "today_users": today_count,
        "yesterday_users": yesterday_count,
        "weekly_users": weekly,
        "monthly_users": monthly,
        "blocked_users": blocked_users,
        "total_referrals": int((stats_raw or {}).get("total_referrals", 0)),
        "claims_pending": pending,
        "claims_approved": approved,
        "claims_rejected": rejected,
        "claims_total": pending + approved + rejected,
        "force_join_channels": len(await get_all_force_join_channels()),
    }


# ─────────────────────────────────────────────────────────────────────────────
# LOGGING
# ─────────────────────────────────────────────────────────────────────────────

async def _log_action(action: str, data: Dict[str, Any]) -> None:
    """Append an entry to the /logs node asynchronously."""
    try:
        loop = asyncio.get_event_loop()
        entry = {
            "action": action,
            "timestamp": get_utc_now().isoformat(),
            **data,
        }
        await loop.run_in_executor(None, fb_push, DB_LOGS, entry)
    except Exception as e:
        logger.error("Log write failed [%s]: %s", action, e)


async def log_error(context: str, error: str) -> None:
    """Log an error event."""
    await _log_action("error", {"context": context, "error": str(error)})


# ─────────────────────────────────────────────────────────────────────────────
# BROADCAST
# ─────────────────────────────────────────────────────────────────────────────

async def save_broadcast_record(
    admin_id: int,
    total: int,
    delivered: int,
    failed: int,
    blocked: int,
    message_type: str,
) -> None:
    """Persist a broadcast summary record."""
    loop = asyncio.get_event_loop()
    now = get_utc_now()
    record = {
        "admin_id": admin_id,
        "date": now.strftime("%Y-%m-%d"),
        "time": now.strftime("%H:%M:%S UTC"),
        "timestamp": now.isoformat(),
        "total": total,
        "delivered": delivered,
        "failed": failed,
        "blocked": blocked,
        "success_pct": round((delivered / total * 100) if total else 0, 1),
        "message_type": message_type,
    }
    await loop.run_in_executor(None, fb_push, DB_BROADCAST_HISTORY, record)


# ─────────────────────────────────────────────────────────────────────────────
# INTERNAL HELPERS
# ─────────────────────────────────────────────────────────────────────────────

async def _increment_stat(key: str, delta: int) -> None:
    """Increment (or decrement) a statistics counter atomically."""
    try:
        loop = asyncio.get_event_loop()
        path = f"{DB_STATISTICS}/{key}"

        def updater(current):
            return max(0, int(current or 0) + delta)

        await loop.run_in_executor(None, get_ref(path).transaction, updater)
    except Exception as e:
        logger.debug("Stat increment failed [%s]: %s", key, e)
