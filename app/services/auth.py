from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import AuditLog, User, UserSession


COOKIE_NAME = "resident_analysis_session"
ROLE_ORDER = {"researcher": 10, "reviewer": 20, "admin": 30}


def hash_password(password: str, *, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    derived = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        600_000,
        dklen=32,
    )
    return f"pbkdf2-sha256$600000${salt.hex()}${derived.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, rounds_text, salt_hex, digest_hex = encoded.split("$", 3)
        if algorithm != "pbkdf2-sha256":
            return False
        actual = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            bytes.fromhex(salt_hex),
            int(rounds_text),
            dklen=32,
        ).hex()
        return hmac.compare_digest(actual, digest_hex)
    except (ValueError, TypeError):
        return False


def ensure_bootstrap_admin(session: Session) -> User | None:
    settings = get_settings()
    if not settings.bootstrap_admin_username or not settings.bootstrap_admin_password:
        return None
    if len(settings.bootstrap_admin_password) < 10:
        raise RuntimeError("BOOTSTRAP_ADMIN_PASSWORD 至少需要10个字符")
    existing = session.scalar(
        select(User).where(User.username == settings.bootstrap_admin_username)
    )
    if existing:
        return existing
    user = User(
        username=settings.bootstrap_admin_username,
        password_hash=hash_password(settings.bootstrap_admin_password),
        role="admin",
    )
    session.add(user)
    session.commit()
    return user


def authenticate(session: Session, username: str, password: str) -> User | None:
    user = session.scalar(select(User).where(User.username == username.strip()))
    if not user or not user.active or not verify_password(password, user.password_hash):
        return None
    return user


def create_user_session(session: Session, user: User) -> str:
    token = secrets.token_urlsafe(32)
    session.add(
        UserSession(
            user_id=user.id,
            token_hash=hashlib.sha256(token.encode("utf-8")).hexdigest(),
            expires_at=datetime.now() + timedelta(hours=12),
        )
    )
    session.commit()
    return token


def user_from_token(session: Session, token: str | None) -> User | None:
    if not token:
        return None
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    record = session.scalar(
        select(UserSession).where(
            UserSession.token_hash == digest,
            UserSession.expires_at > datetime.now(),
        )
    )
    return record.user if record and record.user.active else None


def revoke_token(session: Session, token: str | None) -> None:
    if not token:
        return
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    session.execute(delete(UserSession).where(UserSession.token_hash == digest))
    session.commit()


def has_role(user: User | None, minimum_role: str) -> bool:
    if not get_settings().auth_required:
        return True
    if user is None:
        return False
    return ROLE_ORDER.get(user.role, 0) >= ROLE_ORDER.get(minimum_role, 0)


def audit(
    session: Session,
    *,
    user: User | None,
    action: str,
    resource_type: str = "",
    resource_id: str = "",
    metadata: dict[str, object] | None = None,
) -> None:
    session.add(
        AuditLog(
            user_id=user.id if user else None,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            metadata_json=metadata or {},
        )
    )
    session.commit()
