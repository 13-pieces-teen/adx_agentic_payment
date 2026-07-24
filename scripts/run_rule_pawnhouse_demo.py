"""Run the persistent two-rule-Agent King's Pawnhouse demo through HTTP."""

from __future__ import annotations

import argparse
import json
import time
import urllib.error
import urllib.request


def request_json(
    *,
    method: str,
    url: str,
    token: str | None = None,
    payload: dict[str, object] | None = None,
) -> dict[str, object]:
    data = None
    headers = {"Accept": "application/json"}
    if token is not None:
        headers["X-Arena-Dev-Token"] = token
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        url=url,
        method=method,
        headers=headers,
        data=data,
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"{method} {url} failed: {exc.code} {detail}") from exc


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument(
        "--dev-token",
        default="arena402-local-development-control-token",
    )
    parser.add_argument("--game-id", default=None)
    args = parser.parse_args()

    game_id = args.game_id or f"rule-demo-{int(time.time())}"
    dev_base = f"{args.base_url}/api/dev/pawnhouse/games/{game_id}"
    request_json(
        method="POST",
        url=f"{args.base_url}/api/dev/pawnhouse/games",
        token=args.dev_token,
        payload={"gameId": game_id, "eventSeed": "fixed-demo-seed"},
    )
    request_json(
        method="POST",
        url=f"{dev_base}/rule-participants",
        token=args.dev_token,
        payload={
            "userId": "rule-buyer-user",
            "agentId": "rule-buyer-agent",
            "portfolio": {"cash": "20", "holdings": {}},
            "strategy": {
                "intent": "buy",
                "good": "iron",
                "targetPrice": "7",
                "publicMessage": "Seeking one lot of iron.",
            },
        },
    )
    request_json(
        method="POST",
        url=f"{dev_base}/rule-participants",
        token=args.dev_token,
        payload={
            "userId": "rule-seller-user",
            "agentId": "rule-seller-agent",
            "portfolio": {"cash": "15", "holdings": {"iron": 1}},
            "strategy": {
                "intent": "sell",
                "good": "iron",
                "targetPrice": "6",
                "publicMessage": "Offering one lot of iron.",
            },
        },
    )
    request_json(method="POST", url=f"{dev_base}/start", token=args.dev_token)
    result = request_json(
        method="POST",
        url=f"{dev_base}/run-rule-market",
        token=args.dev_token,
    )
    timeline = request_json(
        method="GET",
        url=f"{args.base_url}/api/v1/pawnhouse/games/{game_id}/timeline",
    )
    print(
        json.dumps(
            {
                "gameId": game_id,
                "result": result,
                "timeline": timeline,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
