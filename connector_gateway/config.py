"""Fail-closed production configuration for the Connector Gateway."""

from __future__ import annotations

import os
from dataclasses import dataclass
from urllib.parse import urlparse


class ConnectorConfigurationError(RuntimeError):
    """Raised when a production Connector setting is missing or unsafe."""


def _positive_int(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = os.getenv(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConnectorConfigurationError(f"{name} must be an integer") from exc
    if not minimum <= value <= maximum:
        raise ConnectorConfigurationError(
            f"{name} must be between {minimum} and {maximum}"
        )
    return value


@dataclass(frozen=True)
class ConnectorGatewayConfig:
    database_url: str
    session_secret: str
    public_app_url: str
    github_oauth_client_id: str | None = None
    github_oauth_client_secret: str | None = None
    session_cookie_name: str = "adx_session"
    csrf_cookie_name: str = "adx_csrf"
    github_oauth_state_cookie_name: str = "adx_github_oauth_state"
    github_oauth_state_ttl_seconds: int = 10 * 60
    session_ttl_seconds: int = 7 * 24 * 60 * 60
    cookie_secure: bool = True
    bootstrap_invite_hash: str | None = None
    auth_rate_limit_attempts: int = 10
    pairing_rate_limit_attempts: int = 60
    rate_limit_window_seconds: int = 60
    max_pending_pairings: int = 500

    @classmethod
    def from_env(cls) -> "ConnectorGatewayConfig":
        database_url = (
            os.getenv("ADX_CONNECTOR_DATABASE_URL") or os.getenv("DATABASE_URL") or ""
        ).strip()
        session_secret = os.getenv("ADX_CONNECTOR_SESSION_SECRET", "").strip()
        public_app_url = os.getenv("ADX_PUBLIC_APP_URL", "").strip().rstrip("/")
        github_oauth_client_id = os.getenv("ADX_GITHUB_OAUTH_CLIENT_ID", "").strip()
        github_oauth_client_secret = os.getenv(
            "ADX_GITHUB_OAUTH_CLIENT_SECRET", ""
        ).strip()
        environment = os.getenv("ADX_ENV", "production").strip().lower()
        cookie_secure_raw = os.getenv("ADX_CONNECTOR_COOKIE_SECURE", "true")
        cookie_secure = cookie_secure_raw.strip().lower() in {"1", "true", "yes"}

        if not database_url:
            raise ConnectorConfigurationError(
                "ADX_CONNECTOR_DATABASE_URL (or DATABASE_URL) is required"
            )
        if not database_url.startswith(("postgresql://", "postgres://")):
            raise ConnectorConfigurationError(
                "Connector database URL must use PostgreSQL"
            )
        if len(session_secret) < 32:
            raise ConnectorConfigurationError(
                "ADX_CONNECTOR_SESSION_SECRET must contain at least 32 characters"
            )
        parsed_app_url = urlparse(public_app_url)
        if not parsed_app_url.netloc:
            raise ConnectorConfigurationError("ADX_PUBLIC_APP_URL must be absolute")
        if environment == "production" and parsed_app_url.scheme != "https":
            raise ConnectorConfigurationError(
                "ADX_PUBLIC_APP_URL must use HTTPS in production"
            )
        if bool(github_oauth_client_id) != bool(github_oauth_client_secret):
            raise ConnectorConfigurationError(
                "ADX_GITHUB_OAUTH_CLIENT_ID and ADX_GITHUB_OAUTH_CLIENT_SECRET "
                "must be configured together"
            )
        if environment == "production" and not cookie_secure:
            raise ConnectorConfigurationError(
                "Secure Connector cookies cannot be disabled in production"
            )

        cookie_name = os.getenv("ADX_CONNECTOR_COOKIE_NAME", "adx_session").strip()
        if not cookie_name or any(char.isspace() for char in cookie_name):
            raise ConnectorConfigurationError("ADX_CONNECTOR_COOKIE_NAME is invalid")
        bootstrap_invite_hash = os.getenv("ADX_BOOTSTRAP_INVITE_HASH")
        if bootstrap_invite_hash is not None:
            bootstrap_invite_hash = bootstrap_invite_hash.strip().lower()
            if len(bootstrap_invite_hash) != 64 or any(
                char not in "0123456789abcdef" for char in bootstrap_invite_hash
            ):
                raise ConnectorConfigurationError(
                    "ADX_BOOTSTRAP_INVITE_HASH must be a SHA-256 hex digest"
                )
        elif environment == "production":
            raise ConnectorConfigurationError(
                "ADX_BOOTSTRAP_INVITE_HASH is required for the beta deployment"
            )

        return cls(
            database_url=database_url,
            session_secret=session_secret,
            public_app_url=public_app_url,
            github_oauth_client_id=github_oauth_client_id or None,
            github_oauth_client_secret=github_oauth_client_secret or None,
            session_cookie_name=cookie_name,
            # The browser client reads this stable non-HttpOnly double-submit
            # cookie name. Keep it independent from a customized session name.
            csrf_cookie_name="adx_csrf",
            github_oauth_state_ttl_seconds=_positive_int(
                "ADX_GITHUB_OAUTH_STATE_TTL_SECONDS",
                10 * 60,
                60,
                15 * 60,
            ),
            session_ttl_seconds=_positive_int(
                "ADX_CONNECTOR_SESSION_TTL_SECONDS",
                7 * 24 * 60 * 60,
                300,
                30 * 24 * 60 * 60,
            ),
            cookie_secure=cookie_secure,
            bootstrap_invite_hash=bootstrap_invite_hash,
            auth_rate_limit_attempts=_positive_int(
                "ADX_CONNECTOR_AUTH_RATE_LIMIT_ATTEMPTS", 10, 1, 1000
            ),
            pairing_rate_limit_attempts=_positive_int(
                "ADX_CONNECTOR_PAIRING_RATE_LIMIT_ATTEMPTS", 60, 1, 10000
            ),
            rate_limit_window_seconds=_positive_int(
                "ADX_CONNECTOR_RATE_LIMIT_WINDOW_SECONDS", 60, 1, 3600
            ),
            max_pending_pairings=_positive_int(
                "ADX_CONNECTOR_MAX_PENDING_PAIRINGS", 500, 1, 10000
            ),
        )
