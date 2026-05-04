"""
Auth module — file-based user store with bcrypt password hashing.

Users are stored in /output/users.json (Railway persistent volume).
Sessions use signed cookies via Starlette SessionMiddleware.

User schema:
  {
    "username": {
      "hash": "$2b$12$...",   # bcrypt hash
      "role": "admin"|"viewer",
      "created": "2026-05-04T12:00:00"
    }
  }

Bootstrap: on first startup with no users.json, creates an admin from
DASHBOARD_USER / DASHBOARD_PASSWORD env vars.
"""

import os
import json
from datetime import datetime, timezone
from pathlib import Path

from passlib.context import CryptContext


_pwd_ctx = CryptContext(schemes=["bcrypt"], deprecated="auto")

DASHBOARD_USER = os.getenv("DASHBOARD_USER", "admin")
DASHBOARD_PASSWORD = os.getenv("DASHBOARD_PASSWORD", "")


# ── Internal helpers ───────────────────────────────────────────────────────────

def _users_path(output_dir: Path) -> Path:
    return output_dir / "users.json"


def _load(output_dir: Path) -> dict:
    path = _users_path(output_dir)
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _save(users: dict, output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    _users_path(output_dir).write_text(
        json.dumps(users, indent=2), encoding="utf-8"
    )


# ── Public API ─────────────────────────────────────────────────────────────────

def init_users(output_dir: Path) -> None:
    """
    Called at server startup. If users.json doesn't exist (or is empty),
    creates an initial admin from the DASHBOARD_USER / DASHBOARD_PASSWORD env vars.
    """
    users = _load(output_dir)
    if users:
        return  # already bootstrapped

    if not DASHBOARD_PASSWORD:
        print("  [auth] WARNING: DASHBOARD_PASSWORD not set — skipping bootstrap")
        return

    username = DASHBOARD_USER or "admin"
    users[username] = {
        "hash": _pwd_ctx.hash(DASHBOARD_PASSWORD),
        "role": "admin",
        "created": datetime.now(timezone.utc).isoformat(),
    }
    _save(users, output_dir)
    print(f"  [auth] Created initial admin user: '{username}'")


def verify_user(username: str, password: str, output_dir: Path) -> dict | None:
    """
    Verify credentials. Returns user dict on success, None on failure.
    The returned dict includes 'username' and 'role' keys.
    """
    users = _load(output_dir)
    entry = users.get(username)
    if not entry:
        _pwd_ctx.dummy_verify()  # constant-time to prevent username enumeration
        return None
    if not _pwd_ctx.verify(password, entry["hash"]):
        return None
    return {"username": username, "role": entry.get("role", "viewer")}


def get_user(username: str, output_dir: Path) -> dict | None:
    """Return user metadata (no hash) or None."""
    users = _load(output_dir)
    entry = users.get(username)
    if not entry:
        return None
    return {
        "username": username,
        "role": entry.get("role", "viewer"),
        "created": entry.get("created", ""),
    }


def list_users(output_dir: Path) -> list[dict]:
    """Return all users as a list of dicts (no hashes)."""
    users = _load(output_dir)
    return [
        {
            "username": u,
            "role": data.get("role", "viewer"),
            "created": data.get("created", ""),
        }
        for u, data in users.items()
    ]


def create_user(username: str, password: str, role: str, output_dir: Path) -> dict:
    """
    Create a new user. Returns {success, message}.
    Roles: 'admin' or 'viewer'.
    """
    if not username or not password:
        return {"success": False, "message": "Username and password are required"}
    if role not in ("admin", "viewer"):
        return {"success": False, "message": "Role must be 'admin' or 'viewer'"}

    users = _load(output_dir)
    if username in users:
        return {"success": False, "message": f"User '{username}' already exists"}

    users[username] = {
        "hash": _pwd_ctx.hash(password),
        "role": role,
        "created": datetime.now(timezone.utc).isoformat(),
    }
    _save(users, output_dir)
    return {"success": True, "message": f"User '{username}' created with role '{role}'"}


def change_password(username: str, new_password: str, output_dir: Path) -> dict:
    """Change a user's password. Returns {success, message}."""
    if not new_password:
        return {"success": False, "message": "Password cannot be empty"}

    users = _load(output_dir)
    if username not in users:
        return {"success": False, "message": f"User '{username}' not found"}

    users[username]["hash"] = _pwd_ctx.hash(new_password)
    _save(users, output_dir)
    return {"success": True, "message": f"Password updated for '{username}'"}


def change_role(username: str, role: str, output_dir: Path) -> dict:
    """Change a user's role. Returns {success, message}."""
    if role not in ("admin", "viewer"):
        return {"success": False, "message": "Role must be 'admin' or 'viewer'"}

    users = _load(output_dir)
    if username not in users:
        return {"success": False, "message": f"User '{username}' not found"}

    users[username]["role"] = role
    _save(users, output_dir)
    return {"success": True, "message": f"Role updated to '{role}' for '{username}'"}


def delete_user(username: str, output_dir: Path) -> dict:
    """
    Delete a user. Refuses if they are the last admin.
    Returns {success, message}.
    """
    users = _load(output_dir)
    if username not in users:
        return {"success": False, "message": f"User '{username}' not found"}

    # Prevent deleting last admin
    admins = [u for u, d in users.items() if d.get("role") == "admin"]
    if users[username].get("role") == "admin" and len(admins) <= 1:
        return {"success": False, "message": "Cannot delete the last admin user"}

    del users[username]
    _save(users, output_dir)
    return {"success": True, "message": f"User '{username}' deleted"}


def admin_count(output_dir: Path) -> int:
    users = _load(output_dir)
    return sum(1 for d in users.values() if d.get("role") == "admin")
