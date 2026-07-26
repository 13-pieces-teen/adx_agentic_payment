#!/bin/bash
# Arena 402 repository setup and import smoke check.
set -euo pipefail

echo "[1/3] Configuring repository hooks"
git config core.hooksPath .githooks

echo "[2/3] Checking Python"
python3 --version

echo "[3/3] Verifying the current composition root"
python3 -m compileall -q \
  arena_agent_contracts \
  arena_core \
  arena_game \
  arena_memorial \
  connector_gateway \
  hosted_agent_control_plane \
  hosted_agent_runtime \
  web
python3 -c "from web.api import create_app; app = create_app(connector_demo_enabled=False); assert '/api/health' in app.openapi()['paths']; print('Arena 402 API import check passed')"

echo "Setup complete"
echo "Run API: uvicorn web.api:create_app --factory --reload --port 8000"
