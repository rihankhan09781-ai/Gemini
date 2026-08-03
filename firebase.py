"""
Firebase Realtime Database initialization and low-level access.
All higher-level operations live in database.py.
"""

import json
import os
from typing import Any, Dict, Optional

import firebase_admin
from firebase_admin import credentials, db
from utils.logger import logger
from config import config


_firebase_initialized = False


def initialize_firebase() -> None:
    """
    Initialize the Firebase Admin SDK.
    Safe to call multiple times – skips if already initialized.
    """
    global _firebase_initialized

    if _firebase_initialized or firebase_admin._apps:
        _firebase_initialized = True
        return

    if not config.FIREBASE_DATABASE_URL:
        raise ValueError("FIREBASE_DATABASE_URL is required but not set.")

    cred_obj: Optional[credentials.Base] = None
    cred_path = config.FIREBASE_CREDENTIALS_PATH

    # ── Try loading credentials ──────────────────────────────────────────────
    if cred_path and os.path.isfile(cred_path):
        try:
            cred_obj = credentials.Certificate(cred_path)
            logger.info("Firebase credentials loaded from file: %s", cred_path)
        except Exception as e:
            logger.error("Failed to load Firebase credentials from file: %s", e)
            raise

    # ── Fallback: inline JSON via env var ────────────────────────────────────
    if cred_obj is None:
        raw_json = os.getenv("FIREBASE_CREDENTIALS_JSON", "")
        if raw_json:
            try:
                cred_dict = json.loads(raw_json)
                cred_obj = credentials.Certificate(cred_dict)
                logger.info("Firebase credentials loaded from FIREBASE_CREDENTIALS_JSON env var.")
            except Exception as e:
                logger.error("Failed to parse FIREBASE_CREDENTIALS_JSON: %s", e)
                raise

    if cred_obj is None:
        raise ValueError(
            "No Firebase credentials found. Set FIREBASE_CREDENTIALS_PATH or "
            "FIREBASE_CREDENTIALS_JSON environment variable."
        )

    firebase_admin.initialize_app(
        cred_obj,
        {"databaseURL": config.FIREBASE_DATABASE_URL},
    )
    _firebase_initialized = True
    logger.info("Firebase initialized. Database URL: %s", config.FIREBASE_DATABASE_URL)


# ── Low-level helpers ──────────────────────────────────────────────────────────

def get_ref(path: str) -> db.Reference:
    """Return a Firebase database reference for the given path."""
    return db.reference(path)


def fb_get(path: str) -> Any:
    """
    Read a value from Firebase at the given path.

    Returns:
        The stored value, or None if the path doesn't exist.
    """
    try:
        return db.reference(path).get()
    except Exception as e:
        logger.error("Firebase GET error [%s]: %s", path, e)
        return None


def fb_set(path: str, value: Any) -> bool:
    """
    Write (overwrite) a value at the given path.

    Returns:
        True on success, False on failure.
    """
    try:
        db.reference(path).set(value)
        return True
    except Exception as e:
        logger.error("Firebase SET error [%s]: %s", path, e)
        return False


def fb_update(path: str, data: Dict[str, Any]) -> bool:
    """
    Merge-update fields at the given path (does not overwrite siblings).

    Returns:
        True on success, False on failure.
    """
    try:
        db.reference(path).update(data)
        return True
    except Exception as e:
        logger.error("Firebase UPDATE error [%s]: %s", path, e)
        return False


def fb_push(path: str, value: Any) -> Optional[str]:
    """
    Push a new child node at the given path (auto-generated key).

    Returns:
        New child key on success, None on failure.
    """
    try:
        ref = db.reference(path).push(value)
        return ref.key
    except Exception as e:
        logger.error("Firebase PUSH error [%s]: %s", path, e)
        return None


def fb_delete(path: str) -> bool:
    """
    Delete the node at the given path.

    Returns:
        True on success, False on failure.
    """
    try:
        db.reference(path).delete()
        return True
    except Exception as e:
        logger.error("Firebase DELETE error [%s]: %s", path, e)
        return False


def fb_transaction(path: str, update_fn) -> bool:
    """
    Run a Firebase transaction at the given path.

    Args:
        path: Database path
        update_fn: Callable that receives current value and returns new value.
                   Return the input unchanged to abort.

    Returns:
        True on success, False on failure.
    """
    try:
        db.reference(path).transaction(update_fn)
        return True
    except Exception as e:
        logger.error("Firebase TRANSACTION error [%s]: %s", path, e)
        return False
