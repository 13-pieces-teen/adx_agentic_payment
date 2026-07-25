"""Minimal GitHub OAuth web-flow client for Arena browser identity."""

from __future__ import annotations

from typing import Protocol

import httpx


class GithubOAuthError(Exception):
    """A sanitized GitHub OAuth failure safe for control-flow handling."""


class GithubOAuthClient(Protocol):
    async def authenticate(
        self,
        *,
        code: str,
        code_verifier: str,
        redirect_uri: str,
    ) -> dict[str, str]: ...


class HttpxGithubOAuthClient:
    """Exchange a short-lived code and return only durable public identity."""

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        *,
        relay_url: str | None = None,
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._relay_url = relay_url

    async def authenticate(
        self,
        *,
        code: str,
        code_verifier: str,
        redirect_uri: str,
    ) -> dict[str, str]:
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                token_headers = {
                    "Accept": "application/json",
                    "User-Agent": "Arena402/1.0",
                }
                token_payload = {
                    "client_id": self._client_id,
                    "code": code,
                    "redirect_uri": redirect_uri,
                    "code_verifier": code_verifier,
                }
                if self._relay_url:
                    token_headers["Authorization"] = (
                        f"Bearer {self._client_secret}"
                    )
                    token_response = await client.post(
                        self._relay_url,
                        headers=token_headers,
                        json=token_payload,
                    )
                else:
                    token_response = await client.post(
                        "https://github.com/login/oauth/access_token",
                        headers=token_headers,
                        data={
                            **token_payload,
                            "client_secret": self._client_secret,
                        },
                    )
                token_response.raise_for_status()
                token_body = token_response.json()
                if not isinstance(token_body, dict):
                    raise GithubOAuthError(
                        "GitHub returned an invalid token response"
                    )
                access_token = token_body.get("access_token")
                if (
                    not isinstance(access_token, str)
                    or len(access_token) < 20
                    or token_body.get("error")
                ):
                    raise GithubOAuthError("GitHub rejected the authorization code")

                user_response = await client.get(
                    "https://api.github.com/user",
                    headers={
                        "Accept": "application/vnd.github+json",
                        "Authorization": f"Bearer {access_token}",
                        "User-Agent": "Arena402/1.0",
                        "X-GitHub-Api-Version": "2026-03-10",
                    },
                )
                user_response.raise_for_status()
                user_body = user_response.json()
                if not isinstance(user_body, dict):
                    raise GithubOAuthError(
                        "GitHub returned an invalid identity response"
                    )
        except GithubOAuthError:
            raise
        except (httpx.HTTPError, ValueError, TypeError) as exc:
            raise GithubOAuthError("GitHub OAuth request failed") from exc

        subject = user_body.get("id")
        login = user_body.get("login")
        if (
            not isinstance(subject, int)
            or subject <= 0
            or not isinstance(login, str)
            or not login
        ):
            raise GithubOAuthError("GitHub returned an invalid identity")
        return {"subject": str(subject), "login": login}
