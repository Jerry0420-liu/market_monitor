from __future__ import annotations

import hashlib
from pathlib import Path


def test_missing_readme_is_reported(tmp_path: Path) -> None:
    from scripts.check_repository import check_repository

    errors = check_repository(tmp_path)

    assert any("README.md" in error for error in errors)


def test_current_repository_passes_structure_check() -> None:
    from scripts.check_repository import check_repository

    root = Path(__file__).resolve().parents[1]

    assert check_repository(root) == []


def test_m8_gate_artifacts_and_browser_outputs_are_declared() -> None:
    from scripts.check_repository import IGNORED_PARTS, REQUIRED_DIRECTORIES, REQUIRED_FILES

    assert {
        "apps/web/e2e/web.spec.ts",
        "apps/web/playwright.config.ts",
        "apps/web/src/App.tsx",
        "apps/web/src/api/client.ts",
        "apps/web/src/auth/AuthContext.tsx",
        "apps/web/src/pages/HomePage.tsx",
        "docs/archive/progress/M8_REPORT.md",
    }.issubset(REQUIRED_FILES)
    assert {
        "apps/web/e2e",
        "apps/web/src/components",
        "apps/web/src/pages",
    }.issubset(REQUIRED_DIRECTORIES)
    assert {"playwright-report", "test-results"}.issubset(IGNORED_PARTS)


def test_v12_history_and_v13_contract_artifacts_are_required() -> None:
    from scripts.check_repository import REQUIRED_FILES

    assert {
        "docs/architecture/API_CONTRACT_V1.3.md",
        "docs/archive/architecture/API_CONTRACT_V1.2.md",
        "openapi/history/market-monitor-v1.2.yaml",
        "openapi/market-monitor-v1.yaml",
    }.issubset(REQUIRED_FILES)
    assert "docs/archive/architecture/API_CONTRACT_V1.2.md" in REQUIRED_FILES


def test_m9_release_and_operations_artifacts_are_required() -> None:
    from scripts.check_repository import REQUIRED_DIRECTORIES, REQUIRED_FILES

    assert {
        ".dockerignore",
        "Dockerfile",
        "compose.yaml",
        "PRIVACY.md",
        "docs/operations/OPERATIONS_RUNBOOK.md",
        "docs/archive/progress/M9_REPORT.md",
        "docs/acceptance/RELEASE_CHECKLIST.md",
        "scripts/m9_capacity.py",
        "scripts/m9_operations.py",
        "scripts/run_local.py",
        "scripts/smoke_local.py",
        "docs/archive/progress/CR-003_IMPLEMENTATION_REPORT.md",
        "docs/archive/progress/PRODUCTION_DEPLOYMENT_PREPARATION_REPORT.md",
        "deploy/compose.production.yaml",
        "deploy/production.env.example",
        "deploy/production.env.schema.json",
        "deploy/nginx/market-monitor.conf.template",
        "deploy/monitoring/market-monitor-capacity.sh",
        "deploy/prepare_host.sh",
        "scripts/official_runner.py",
    }.issubset(REQUIRED_FILES)
    assert {"docs/operations", "docs/acceptance", "tests/m9"}.issubset(REQUIRED_DIRECTORIES)


def test_v12_markdown_baseline_remains_byte_exact() -> None:
    root = Path(__file__).resolve().parents[1]
    expected = "038e880991d075b4e5924651126e13fa8916ec554437bb78d2cdcdc9678b81f8"
    relative = "docs/archive/architecture/API_CONTRACT_V1.2.md"
    assert hashlib.sha256((root / relative).read_bytes()).hexdigest() == expected


def test_missing_persistence_package_is_reported(tmp_path: Path) -> None:
    from scripts.check_repository import check_repository

    errors = check_repository(tmp_path)

    assert any("packages/persistence" in error for error in errors)


def test_secret_value_is_reported(tmp_path: Path) -> None:
    from scripts.check_repository import check_repository

    key_name = "api_" + "key"
    secret_value = "live-" + "secret-value"
    (tmp_path / "config.py").write_text(f'{key_name} = "{secret_value}"', encoding="utf-8")

    assert any(
        "possible secret value in: config.py" in error for error in check_repository(tmp_path)
    )


def test_secret_named_identifier_reference_is_not_reported(tmp_path: Path) -> None:
    from scripts.check_repository import check_repository

    (tmp_path / "client.ts").write_text(
        "const request = { csrfToken: auth.csrfToken, password: form.password };",
        encoding="utf-8",
    )

    assert not any(
        "possible secret value in: client.ts" in error for error in check_repository(tmp_path)
    )


def test_secret_file_reference_and_dotted_value_are_not_reported(tmp_path: Path) -> None:
    from scripts.check_repository import check_repository

    password_name = "pass" + "word"
    (tmp_path / "compose.yaml").write_text(
        "MARKET_MONITOR_OWNER_" + password_name.upper() + "_FILE: /run/secrets/owner_password:ro\n",
        encoding="utf-8",
    )
    (tmp_path / "settings.py").write_text(
        f"{password_name} = settings.owner_{password_name}\n",
        encoding="utf-8",
    )

    assert not any("possible secret value" in error for error in check_repository(tmp_path))


def test_machine_specific_path_is_reported(tmp_path: Path) -> None:
    from scripts.check_repository import check_repository

    machine_path = "C:" + "\\\\" + "Users\\\\Example\\\\market-monitor"
    (tmp_path / "config.py").write_text(f'data_dir = "{machine_path}"', encoding="utf-8")

    assert any(
        "machine-specific absolute path in: config.py" in error
        for error in check_repository(tmp_path)
    )
