from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_release_assets_are_present_and_keep_the_runtime_local_and_hardened() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    compose = (ROOT / "compose.yaml").read_text(encoding="utf-8")
    dockerignore = (ROOT / ".dockerignore").read_text(encoding="utf-8")

    assert "FROM node:24.18.0" in dockerfile
    assert "FROM python:3.14.6" in dockerfile
    assert "USER marketmonitor" in dockerfile
    assert "scripts/run_local.py" in dockerfile
    assert "apps/web/dist" in dockerfile
    assert "0.0.0.0" not in dockerfile

    assert compose.count("\n  market-monitor:") == 1
    assert "network_mode: host" in compose
    assert "MARKET_MONITOR_BIND_HOST: 127.0.0.1" in compose
    assert "read_only: true" in compose
    assert 'user: "10001:10001"' in compose
    assert "cap_drop:" in compose and "- ALL" in compose
    assert "no-new-privileges:true" in compose
    assert "tmpfs:" in compose
    assert "healthcheck:" in compose
    assert "ports:" not in compose
    assert "password=" not in compose.lower()
    assert "secret=" not in compose.lower()

    assert {".env", ".venv", "node_modules", "var", "apps/web/dist"}.issubset(
        set(dockerignore.splitlines())
    )


def test_staged_disabled_production_assets_keep_official_gate_closed() -> None:
    compose = (ROOT / "deploy/compose.production.yaml").read_text(encoding="utf-8")
    environment = (ROOT / "deploy/production.env.example").read_text(encoding="utf-8")
    schema = (ROOT / "deploy/production.env.schema.json").read_text(encoding="utf-8")
    proxy = (ROOT / "deploy/nginx/market-monitor.conf.template").read_text(encoding="utf-8")
    monitor = (ROOT / "deploy/monitoring/market-monitor-capacity.sh").read_text(encoding="utf-8")

    assert 'MARKET_MONITOR_OFFICIAL_ENABLED: "false"' in compose
    assert 'MARKET_MONITOR_THRESHOLD_ACTIVATION: "0"' in compose
    assert "market_monitor_data:" in compose
    assert "market_monitor_artifacts:" in compose
    assert "market_monitor_backups:" in compose
    assert "stop_grace_period:" in compose
    assert "max-size:" in compose
    assert "health/ready" in compose
    assert "OFFICIAL_ENABLED=false" in environment
    assert "THRESHOLD_ACTIVATION=0" in environment
    assert '"MARKET_MONITOR_OFFICIAL_ENABLED"' in schema
    assert "__MARKET_MONITOR_DOMAIN__" in proxy
    assert "df -P" in monitor


def test_release_and_operations_documents_cover_owner_boundaries_and_recovery() -> None:
    required = {
        "deploy/README.md",
        "docs/operations/OPERATIONS_RUNBOOK.md",
        "docs/acceptance/RELEASE_CHECKLIST.md",
        "docs/operations/MARKET_DATA_NOTICE.md",
        "docs/operations/THIRD_PARTY_NOTICES_POLICY.md",
        "PRIVACY.md",
    }
    for relative in required:
        assert (ROOT / relative).is_file(), relative

    runbook = (ROOT / "docs/operations/OPERATIONS_RUNBOOK.md").read_text(encoding="utf-8").lower()
    for term in (
        "backup",
        "restore",
        "recovering",
        "rewarm",
        "reconcile",
        "retention",
        "checkpoint",
        "vacuum",
        "rollback",
        "webhook",
    ):
        assert term in runbook

    license_note = (
        (ROOT / "docs/decisions/LICENSE_SELECTION.md").read_text(encoding="utf-8").lower()
    )
    assert "mit" in license_note
    assert "owner" in license_note
    assert (ROOT / "LICENSE").is_file()

    privacy = (ROOT / "PRIVACY.md").read_text(encoding="utf-8").lower()
    assert "local" in privacy
    assert "webhook" in privacy
    assert "backup" in privacy

    deployment = (ROOT / "deploy/README.md").read_text(encoding="utf-8")
    assert "10001:10001" in deployment
    assert "owner-password" in deployment


def test_m9_development_commands_and_local_runtime_entrypoints_are_discoverable() -> None:
    result = subprocess.run(
        [sys.executable, "scripts/dev.py", "--help"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=15,
    )

    assert result.returncode == 0
    for command in (
        "test-m9",
        "m9-operations",
        "demo-m9",
        "capacity-m9",
        "start-local",
        "smoke-local",
    ):
        assert command in result.stdout

    runtime = (ROOT / "scripts/run_local.py").read_text(encoding="utf-8")
    assert "workers=1" in runtime
    assert "proxy_headers=False" in runtime
    assert "MARKET_MONITOR_BIND_HOST" in runtime
    assert (ROOT / "scripts/smoke_local.py").is_file()
