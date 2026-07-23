"""Self-hosted beta identity, signed sessions and CSRF enforcement."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import re
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from argon2 import PasswordHasher, Type
from argon2.exceptions import InvalidHash, VerificationError
from fastapi import Request, Response
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from .config import ConnectorGatewayConfig
from .repository import (
    ConnectorRepository,
    DuplicateIdentityError,
    InvalidInviteError,
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class AuthError(Exception):
    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


@dataclass(frozen=True)
class AuthPrincipal:
    user_id: str
    username: str
    temporary: bool
    session_token_hash: str
    csrf_hash: str


@dataclass(frozen=True)
class IssuedSession:
    principal: AuthPrincipal
    signed_cookie: str
    csrf_token: str
    expires_at: datetime


class ConnectorAuth:
    _username_pattern = re.compile(r"^[a-z0-9][a-z0-9._-]{2,63}$")

    def __init__(
        self,
        repository: ConnectorRepository,
        config: ConnectorGatewayConfig,
    ) -> None:
        self.repository = repository
        self.config = config
        self._signer = URLSafeTimedSerializer(
            config.session_secret,
            salt="adx-connector-session-v1",
        )
        self._password_hasher = PasswordHasher(
            time_cost=3,
            memory_cost=64 * 1024,
            parallelism=2,
            hash_len=32,
            salt_len=16,
            type=Type.ID,
        )
        # A fixed-cost fallback prevents username enumeration through a cheap
        # "unknown account" password path.
        self._dummy_password_hash = (
            "$argon2id$v=19$m=65536,t=3,p=2$Lhqnna+VXMV/ThxbWMiDAg"
            "$3TMja17jVbdxigGX7eQ74RzG5Ec9sYzRp1jpBOZ4yOU"
        )
        # Bound Argon2 memory/CPU on the 2-vCPU beta host even if requests
        # arrive from many independently rate-limited client addresses.
        self._password_work_slots = asyncio.Semaphore(2)
        self._initialization_lock = asyncio.Lock()
        self._initialized = False

    async def initialize(self) -> None:
        if self._initialized:
            return
        async with self._initialization_lock:
            if self._initialized:
                return
            await self.repository.initialize()
            if self.config.bootstrap_invite_hash:
                await self.repository.seed_invite(self.config.bootstrap_invite_hash)
            self._initialized = True

    async def accept_invite(
        self,
        invite_code: str,
        username: str,
        password: str,
    ) -> IssuedSession:
        await self.initialize()
        if len(invite_code) < 20 or len(invite_code) > 512:
            raise AuthError(401, "Invalid or already used invite")
        invite_hash = _digest(invite_code)
        # Invite tokens are high entropy and rate limited at the HTTP boundary.
        # Reject an invalid/consumed token before spending CPU on Argon2.
        if not await self.repository.is_invite_available(invite_hash, _now()):
            raise AuthError(401, "Invalid or already used invite")
        normalized_username = self._normalize_username(username)
        self._validate_password(password)
        if await self.repository.get_user_by_username(normalized_username):
            raise AuthError(409, "Username is already registered")
        async with self._password_work_slots:
            password_hash = await asyncio.to_thread(
                self._password_hasher.hash,
                password,
            )
        user_id = f"user_{uuid.uuid4().hex[:20]}"
        try:
            user = await self.repository.consume_invite_and_create_user(
                invite_hash,
                user_id,
                normalized_username,
                password_hash,
                False,
                _now(),
            )
        except InvalidInviteError as exc:
            raise AuthError(401, "Invalid or already used invite") from exc
        except DuplicateIdentityError as exc:
            raise AuthError(409, "Username is already registered") from exc
        return await self._issue_session(user)

    async def register(
        self, invite_code: str, username: str, password: str
    ) -> IssuedSession:
        return await self.accept_invite(invite_code, username, password)

    async def login(self, username: str, password: str) -> IssuedSession:
        await self.initialize()
        normalized_username = self._normalize_username(username)
        user = await self.repository.get_user_by_username(normalized_username)
        password_hash = (
            str(user.get("password_hash"))
            if user and user.get("password_hash")
            else self._dummy_password_hash
        )
        verified = False
        try:
            async with self._password_work_slots:
                verified = await asyncio.to_thread(
                    self._password_hasher.verify,
                    password_hash,
                    password,
                )
        except (VerificationError, InvalidHash):
            verified = False
        if (
            not verified
            or user is None
            or user.get("temporary")
            or user.get("disabled_at") is not None
        ):
            raise AuthError(401, "Invalid username or password")
        return await self._issue_session(user)

    async def authenticate(self, request: Request) -> AuthPrincipal:
        await self.initialize()
        signed_cookie = request.cookies.get(self.config.session_cookie_name)
        if not signed_cookie:
            raise AuthError(401, "Authentication required")
        try:
            raw_token = self._signer.loads(
                signed_cookie,
                max_age=self.config.session_ttl_seconds,
            )
        except (BadSignature, SignatureExpired) as exc:
            raise AuthError(401, "Invalid or expired session") from exc
        if not isinstance(raw_token, str) or len(raw_token) < 32:
            raise AuthError(401, "Invalid or expired session")
        token_hash = _digest(raw_token)
        record = await self.repository.get_session(token_hash)
        now = _now()
        if (
            not record
            or record.get("revoked_at") is not None
            or record.get("disabled_at") is not None
            or record["expires_at"] <= now
        ):
            raise AuthError(401, "Invalid or expired session")
        return AuthPrincipal(
            user_id=str(record["user_id"]),
            username=str(record["username"]),
            temporary=bool(record.get("temporary")),
            session_token_hash=token_hash,
            csrf_hash=str(record["csrf_hash"]),
        )

    async def require_csrf(self, request: Request, principal: AuthPrincipal) -> None:
        supplied = request.headers.get("x-csrf-token", "")
        csrf_cookie = request.cookies.get(self.config.csrf_cookie_name, "")
        if (
            not supplied
            or not csrf_cookie
            or not hmac.compare_digest(supplied, csrf_cookie)
            or not hmac.compare_digest(_digest(supplied), principal.csrf_hash)
        ):
            raise AuthError(403, "CSRF validation failed")

    async def logout(self, principal: AuthPrincipal) -> None:
        await self.repository.revoke_session(
            principal.session_token_hash,
            _now(),
        )

    def set_session_cookies(self, response: Response, issued: IssuedSession) -> None:
        response.set_cookie(
            self.config.session_cookie_name,
            issued.signed_cookie,
            max_age=self.config.session_ttl_seconds,
            expires=issued.expires_at,
            path="/",
            secure=self.config.cookie_secure,
            httponly=True,
            samesite="lax",
        )
        response.set_cookie(
            self.config.csrf_cookie_name,
            issued.csrf_token,
            max_age=self.config.session_ttl_seconds,
            expires=issued.expires_at,
            path="/",
            secure=self.config.cookie_secure,
            httponly=False,
            samesite="lax",
        )

    def clear_session_cookies(self, response: Response) -> None:
        response.delete_cookie(
            self.config.session_cookie_name,
            path="/",
            secure=self.config.cookie_secure,
            httponly=True,
            samesite="lax",
        )
        response.delete_cookie(
            self.config.csrf_cookie_name,
            path="/",
            secure=self.config.cookie_secure,
            httponly=False,
            samesite="lax",
        )

    async def _issue_session(self, user: dict[str, Any]) -> IssuedSession:
        raw_token = secrets.token_urlsafe(48)
        csrf_token = secrets.token_urlsafe(32)
        created_at = _now()
        expires_at = created_at + timedelta(seconds=self.config.session_ttl_seconds)
        token_hash = _digest(raw_token)
        csrf_hash = _digest(csrf_token)
        await self.repository.create_session(
            token_hash,
            str(user["user_id"]),
            csrf_hash,
            created_at,
            expires_at,
        )
        principal = AuthPrincipal(
            user_id=str(user["user_id"]),
            username=str(user["username"]),
            temporary=bool(user.get("temporary")),
            session_token_hash=token_hash,
            csrf_hash=csrf_hash,
        )
        return IssuedSession(
            principal=principal,
            signed_cookie=self._signer.dumps(raw_token),
            csrf_token=csrf_token,
            expires_at=expires_at,
        )

    def _normalize_username(self, value: str) -> str:
        normalized = value.strip().lower()
        if not self._username_pattern.fullmatch(normalized):
            raise AuthError(
                422,
                "Username must be 3-64 lowercase letters, numbers, dots, dashes or underscores",
            )
        return normalized

    @staticmethod
    def _validate_password(value: str) -> None:
        if len(value) < 12 or len(value) > 1024:
            raise AuthError(422, "Password must contain 12-1024 characters")
