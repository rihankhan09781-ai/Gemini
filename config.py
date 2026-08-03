"""
Configuration module for Free Gemini Pro Referral Bot.
Loads all settings from environment variables.
"""

import os
from typing import List
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()


class Config:
    """Central configuration class for the bot."""

    # ── Telegram ──────────────────────────────────────────────────────────────
    BOT_TOKEN: str = os.getenv("BOT_TOKEN", "")
    BOT_USERNAME: str = os.getenv("BOT_USERNAME", "YourBot").lstrip("@")

    # ── Firebase ──────────────────────────────────────────────────────────────
    FIREBASE_CREDENTIALS_PATH: str = os.getenv(
        "FIREBASE_CREDENTIALS_PATH", "firebase_credentials.json"
    )
    FIREBASE_DATABASE_URL: str = os.getenv("FIREBASE_DATABASE_URL", "")

    # ── Admins ────────────────────────────────────────────────────────────────
    @staticmethod
    def get_initial_admin_ids() -> List[int]:
        """Parse comma-separated admin IDs from environment."""
        raw = os.getenv("ADMIN_IDS", "")
        ids: List[int] = []
        for part in raw.split(","):
            part = part.strip()
            if part.isdigit():
                ids.append(int(part))
        return ids

    # ── Webhook ───────────────────────────────────────────────────────────────
    WEBHOOK_HOST: str = os.getenv("WEBHOOK_HOST", "")
    WEBHOOK_PATH: str = os.getenv("WEBHOOK_PATH", "/webhook")
    WEBHOOK_PORT: int = int(os.getenv("WEBHOOK_PORT", "8443"))
    USE_POLLING: bool = os.getenv("USE_POLLING", "true").lower() == "true"

    # ── Bot defaults ──────────────────────────────────────────────────────────
    DEFAULT_MIN_REFERRALS: int = 10
    DEFAULT_REFERRAL_REWARD: int = 1
    DEFAULT_CLAIM_REWARD_NAME: str = "Free Gemini Pro"

    # ── Anti-spam ─────────────────────────────────────────────────────────────
    THROTTLE_RATE: float = 0.5        # seconds between messages per user
    BROADCAST_DELAY: float = 0.05     # seconds between broadcast messages

    # ── Cache TTL (seconds) ───────────────────────────────────────────────────
    CACHE_USER_TTL: int = 60
    CACHE_SETTINGS_TTL: int = 300
    CACHE_CHANNELS_TTL: int = 120

    @classmethod
    def validate(cls) -> None:
        """Raise ValueError if required settings are missing."""
        if not cls.BOT_TOKEN:
            raise ValueError("BOT_TOKEN is not set in environment variables.")
        if not cls.FIREBASE_DATABASE_URL:
            raise ValueError("FIREBASE_DATABASE_URL is not set in environment variables.")


config = Config()
