"""Admin-only access control (Phase 2B). Identity = numeric Telegram user id."""

from __future__ import annotations

from bot.config import admin_ids, public_access_enabled


def is_admin(user_id: int | None) -> bool:
    if user_id is None:
        return False
    return int(user_id) in admin_ids()


def can_use_bot(user_id: int | None) -> bool:
    if is_admin(user_id):
        return True
    # Public layer prepared but disabled by default
    if public_access_enabled():
        return user_id is not None
    return False


def deny_message() -> str:
    return (
        "Access denied.\n"
        "This bot is currently restricted to administrators.\n"
        "Your Telegram numeric user id is required for access control."
    )
