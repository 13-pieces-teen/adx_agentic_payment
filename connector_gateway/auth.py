"""Self-hosted beta identity, signed sessions and CSRF enforcement."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import re
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlencode

from argon2 import PasswordHasher, Type
from argon2.exceptions import InvalidHash, VerificationError
from fastapi import Request, Response
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from .config import ConnectorGatewayConfig
from .github_oauth import GithubOAuthClient, GithubOAuthError
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
    identity_provider: str = "password"
    provider_subject: str | None = None


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
        github_oauth_client: GithubOAuthClient | None = None,
    ) -> None:
        self.repository = repository
        self.config = config
        self.github_oauth_client = github_oauth_client
        self._signer = URLSafeTimedSerializer(
            config.session_secret,
            salt="adx-connector-session-v1",
        )
        self._github_state_signer = URLSafeTimedSerializer(
            config.session_secret,
            salt="arena402-github-oauth-state-v1",
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
        self, invite_code: str | None, username: str, password: str
    ) -> IssuedSession:
        if invite_code:
            return await self.accept_invite(invite_code, username, password)
        if not self.config.public_registration_enabled:
            raise AuthError(403, "Registration requires an invite")

        await self.initialize()
        normalized_username = self._normalize_username(username)
        self._validate_password(password)
        if await self.repository.get_user_by_username(normalized_username):
            raise AuthError(409, "Username is already registered")
        async with self._password_work_slots:
            password_hash = await asyncio.to_thread(
                self._password_hasher.hash,
                password,
            )
        try:
            user = await self.repository.create_password_user(
                f"user_{uuid.uuid4().hex[:20]}",
                normalized_username,
                password_hash,
                _now(),
            )
        except DuplicateIdentityError as exc:
            raise AuthError(409, "Username is already registered") from exc
        return await self._issue_session(user)

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
            or user.get("identity_provider", "password") != "password"
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
            identity_provider=str(
                record.get("identity_provider", "password")
            ),
            provider_subject=(
                str(record["provider_subject"])
                if record.get("provider_subject") is not None
                else None
            ),
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

    def begin_github_oauth(self, return_to: str | None) -> tuple[str, str]:
        if not (
            self.config.github_oauth_client_id
            and self.config.github_oauth_client_secret
        ):
            raise AuthError(503, "GitHub sign-in is unavailable")

        state = secrets.token_urlsafe(32)
        code_verifier = secrets.token_urlsafe(64)
        code_challenge = base64.urlsafe_b64encode(
            hashlib.sha256(code_verifier.encode("ascii")).digest()
        ).rstrip(b"=").decode("ascii")
        redirect_uri = self.config.github_oauth_callback_url
        signed_state = self._github_state_signer.dumps(
            {
                "state": state,
                "code_verifier": code_verifier,
                "return_to": self.safe_return_to(return_to),
            }
        )
        query = urlencode(
            {
                "client_id": self.config.github_oauth_client_id,
                "redirect_uri": redirect_uri,
                "scope": "read:user",
                "state": state,
                "code_challenge": code_challenge,
                "code_challenge_method": "S256",
                "prompt": "select_account",
            }
        )
        return f"https://github.com/login/oauth/authorize?{query}", signed_state

    def validate_github_oauth_state(
        self,
        signed_state: str | None,
        supplied_state: str | None,
    ) -> dict[str, str]:
        if not signed_state or not supplied_state:
            raise AuthError(401, "invalid_state")
        try:
            value = self._github_state_signer.loads(
                signed_state,
                max_age=self.config.github_oauth_state_ttl_seconds,
            )
        except (BadSignature, SignatureExpired) as exc:
            raise AuthError(401, "invalid_state") from exc
        if not isinstance(value, dict):
            raise AuthError(401, "invalid_state")
        expected_state = value.get("state")
        code_verifier = value.get("code_verifier")
        return_to = value.get("return_to")
        if (
            not isinstance(expected_state, str)
            or not hmac.compare_digest(expected_state, supplied_state)
            or not isinstance(code_verifier, str)
            or not 43 <= len(code_verifier) <= 128
            or not isinstance(return_to, str)
        ):
            raise AuthError(401, "invalid_state")
        return {
            "code_verifier": code_verifier,
            "return_to": self.safe_return_to(return_to),
        }

    async def sign_in_with_github(
        self,
        *,
        code: str,
        oauth_state: dict[str, str],
    ) -> IssuedSession:
        if self.github_oauth_client is None:
            raise AuthError(503, "github_unavailable")
        try:
            identity = await self.github_oauth_client.authenticate(
                code=code,
                code_verifier=oauth_state["code_verifier"],
                redirect_uri=self.config.github_oauth_callback_url,
            )
        except GithubOAuthError as exc:
            raise AuthError(502, "github_failed") from exc

        subject = identity.get("subject")
        login = identity.get("login")
        if (
            not isinstance(subject, str)
            or not subject.isdigit()
            or not isinstance(login, str)
        ):
            raise AuthError(502, "github_failed")
        normalized_login = login.strip().lower()
        if not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,38})", normalized_login):
            raise AuthError(502, "github_failed")
        try:
            user = await self.repository.get_or_create_oauth_user(
                "github",
                subject,
                normalized_login,
                _now(),
            )
        except DuplicateIdentityError as exc:
            raise AuthError(409, "github_failed") from exc
        if user.get("disabled_at") is not None:
            raise AuthError(403, "account_disabled")
        return await self._issue_session(user)

    @staticmethod
    def safe_return_to(value: str | None) -> str:
        if (
            not value
            or len(value) > 1024
            or not value.startswith("/")
            or value.startswith("//")
            or "\\" in value
            or any(ord(char) < 32 for char in value)
        ):
            return "/agents"
        return value

    def set_github_oauth_state_cookie(
        self,
        response: Response,
        signed_state: str,
    ) -> None:
        response.set_cookie(
            self.config.github_oauth_state_cookie_name,
            signed_state,
            max_age=self.config.github_oauth_state_ttl_seconds,
            path="/api/auth/github",
            secure=self.config.cookie_secure,
            httponly=True,
            samesite="lax",
        )

    def clear_github_oauth_state_cookie(self, response: Response) -> None:
        response.delete_cookie(
            self.config.github_oauth_state_cookie_name,
            path="/api/auth/github",
            secure=self.config.cookie_secure,
            httponly=True,
            samesite="lax",
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
            identity_provider=str(
                user.get("identity_provider", "password")
            ),
            provider_subject=(
                str(user["provider_subject"])
                if user.get("provider_subject") is not None
                else None
            ),
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
