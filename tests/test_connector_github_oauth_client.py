from __future__ import annotations

import asyncio

from connector_gateway import github_oauth
from connector_gateway.github_oauth import HttpxGithubOAuthClient


class _FakeResponse:
    def __init__(self, body: dict, status_code: int = 200) -> None:
        self._body = body
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise AssertionError(f"unexpected HTTP {self.status_code}")

    def json(self) -> dict:
        return self._body


class _RecordingClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args) -> None:
        return None

    async def post(self, url: str, **kwargs):
        self.calls.append(("POST", url, kwargs))
        return _FakeResponse(
            {
                "access_token": "github-access-token-for-test",
                "token_type": "bearer",
                "scope": "read:user",
            }
        )

    async def get(self, url: str, **kwargs):
        self.calls.append(("GET", url, kwargs))
        return _FakeResponse({"id": 1234567, "login": "octo-cat"})


def test_github_oauth_uses_authenticated_token_relay(monkeypatch) -> None:
    recording = _RecordingClient()
    monkeypatch.setattr(
        github_oauth.httpx,
        "AsyncClient",
        lambda **_kwargs: recording,
    )
    client = HttpxGithubOAuthClient(
        "Ov23li5jawa0KFXEhpX4",
        "github-client-secret-for-test-value-1234",
        relay_url="https://www.arena402.com/api/internal/github/oauth",
    )

    identity = asyncio.run(
        client.authenticate(
            code="temporary-code",
            code_verifier="v" * 64,
            redirect_uri="https://api.arena402.com/api/auth/github/callback",
        )
    )

    assert identity == {"subject": "1234567", "login": "octo-cat"}
    method, url, kwargs = recording.calls[0]
    assert (method, url) == (
        "POST",
        "https://www.arena402.com/api/internal/github/oauth",
    )
    assert kwargs["headers"]["Authorization"] == (
        "Bearer github-client-secret-for-test-value-1234"
    )
    assert kwargs["json"] == {
        "client_id": "Ov23li5jawa0KFXEhpX4",
        "code": "temporary-code",
        "redirect_uri": "https://api.arena402.com/api/auth/github/callback",
        "code_verifier": "v" * 64,
    }
    assert "client_secret" not in kwargs["json"]
    assert recording.calls[1][0:2] == (
        "GET",
        "https://api.github.com/user",
    )
