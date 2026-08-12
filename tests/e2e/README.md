# End-to-end harnesses

This directory contains executable acceptance harnesses and their disposable
Compose overrides. They are intentionally separate from the pytest suite in
`tests/test_*.py` because they may require Docker, Go, Playwright, local model
CLI authentication, or explicit one-time invitations.

| Harness | Boundary exercised |
|---|---|
| `mcp_docker_e2e.py` | WSS wake, stateless MCP claim/submit, and PostgreSQL Result Sink |
| `real_runtimes_docker_e2e.py` | Host Connector plus real local Runtime against an isolated Docker Arena |
| `mixed_codex_fallback_docker_e2e.py` | Hosted/Connector fallback and fault recovery |
| `hosted_worker_process_recovery_e2e.py` | Production Hosted Worker lease and crash recovery |
| `connector_go_e2e.py` | Cross-language Go Connector smoke without a paid model call |
| `connector_ui_smoke.py` | Manual Connector management-page browser smoke |

Run these files from the repository root using their documented prerequisites.
The normal `python -m pytest` command does not execute them directly; focused
pytest contract tests import selected pure helpers from this package.
