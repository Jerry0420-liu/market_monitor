from __future__ import annotations

import argparse
import importlib
import re
from collections.abc import Callable
from pathlib import Path
from typing import cast


def _check_generated_types(root: Path) -> str | None:
    try:
        from scripts.generate_api_types import check_generated_types
    except ModuleNotFoundError:  # Direct execution sets scripts/ as sys.path[0].
        module = importlib.import_module("generate_api_types")
        direct_check = cast(Callable[[Path], str | None], module.check_generated_types)
        return direct_check(root)
    return check_generated_types(root)


REQUIRED_FILES = (
    ".env.example",
    ".dockerignore",
    ".github/workflows/ci.yml",
    ".gitignore",
    ".node-version",
    ".python-version",
    "Dockerfile",
    "alembic.ini",
    "CONTRIBUTING.md",
    "docs/decisions/FINAL_OWNER_DECISIONS_v1.0.md",
    "docs/decisions/LICENSE_SELECTION.md",
    "PRIVACY.md",
    "README.md",
    "SECURITY.md",
    "package-lock.json",
    "package.json",
    "pyproject.toml",
    "docs/architecture/API_CONTRACT_V1.3.md",
    "docs/architecture/TDX_DATA_SEMANTICS_v1.0.md",
    "docs/README.md",
    "docs/acceptance/CURRENT_STATUS.md",
    "docs/acceptance/RELEASE_CHECKLIST.md",
    "docs/operations/OPERATIONS_RUNBOOK.md",
    "docs/operations/PRODUCTION_SERVER_DEPLOYMENT.md",
    "docs/operations/MARKET_DATA_NOTICE.md",
    "docs/operations/THIRD_PARTY_NOTICES_POLICY.md",
    "docs/archive/architecture/API_CONTRACT_V1.2.md",
    "docs/archive/progress/README.md",
    "docs/archive/progress/CR-003_IMPLEMENTATION_REPORT.md",
    "docs/archive/progress/M8_REPORT.md",
    "docs/archive/progress/M9_REPORT.md",
    "docs/archive/progress/PRODUCTION_DEPLOYMENT_PREPARATION_REPORT.md",
    "docs/archive/release/RC_NOTES.md",
    "runtime/README.md",
    "compose.yaml",
    "deploy/compose.production.yaml",
    "deploy/monitoring/market-monitor-capacity.sh",
    "deploy/nginx/market-monitor.conf.template",
    "deploy/prepare_host.sh",
    "deploy/production.env.example",
    "deploy/production.env.schema.json",
    "migrations/versions/0007_api_security.py",
    "migrations/versions/0008_cr002_native_tdx.py",
    "openapi/market-monitor-v1.yaml",
    "openapi/history/market-monitor-v1.2.yaml",
    "openapi/README.md",
    "packages/contracts/src/market_monitor_contracts/models.py",
    "requirements-dev.lock",
    "scripts/generate_api_types.py",
    "scripts/m9_capacity.py",
    "scripts/m9_operations.py",
    "scripts/official_runner.py",
    "scripts/m7_diagnostics.py",
    "scripts/run_local.py",
    "scripts/smoke_local.py",
    "apps/web/README.md",
    "apps/web/package.json",
    "apps/web/playwright.config.ts",
    "apps/web/vite.config.ts",
    "apps/web/e2e/web.spec.ts",
    "apps/web/src/App.tsx",
    "apps/web/src/api/client.ts",
    "apps/web/src/api/generated.ts",
    "apps/web/src/api/resources.ts",
    "apps/web/src/auth/AuthContext.tsx",
    "apps/web/src/hooks/useResource.ts",
    "apps/web/src/pages/HomePage.tsx",
    "apps/web/src/presentation.ts",
    "apps/web/src/routing.tsx",
    "apps/web/src/styles.css",
    "apps/web/src/test/fixtures.ts",
)
REQUIRED_DIRECTORIES = (
    "apps/api",
    "apps/web",
    "apps/web/e2e",
    "apps/web/src/api",
    "apps/web/src/auth",
    "apps/web/src/components",
    "apps/web/src/hooks",
    "apps/web/src/pages",
    "apps/web/src/test",
    "deploy",
    "deploy/monitoring",
    "deploy/nginx",
    "docs/architecture",
    "docs/development",
    "docs/acceptance",
    "docs/archive",
    "docs/operations",
    "docs/archive/architecture",
    "docs/archive/progress",
    "docs/archive/release",
    "runtime",
    "migrations/versions",
    "openapi",
    "openapi/history",
    "packages/contracts",
    "packages/contracts/src/market_monitor_contracts",
    "packages/persistence/src/market_monitor_persistence",
    "packages/data/src/market_monitor_data",
    "packages/analysis/src/market_monitor_analysis",
    "packages/notifications/src/market_monitor_notifications",
    "packages/shared",
    "scripts",
    "tests",
    "tests/m1",
    "tests/m2",
    "tests/m3",
    "tests/m4",
    "tests/m5",
    "tests/m6",
    "tests/m7",
    "tests/m9",
)
IGNORED_PARTS = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "coverage",
    "dist",
    "node_modules",
    "playwright-report",
    "test-results",
    "runtime",
    "var",
}
SECRET_VALUE = re.compile(
    r"(?i)(?<![A-Za-z0-9_-])(?:api[_-]?key|password|secret|token)(?![A-Za-z0-9_-])"
    r"\s*[:=]\s*[\"']?"
    r"(?!example|placeholder|changeme|<)"
    r"(?![A-Za-z_][A-Za-z0-9_.]*(?:\[|\())"
    r"(?![A-Za-z_][A-Za-z0-9_.]*\s*(?:[,;)}\]]|$|\r?$|\r?\n))"
    r"[^\s\"'#]{8,}"
)
MACHINE_PATH = re.compile(r"(?:[A-Za-z]:\\+Users\\+[^\\\s]+\\+|/ho" + r"me/[^/\s]+/)")
TEXT_NAMES = {
    ".dockerignore",
    ".env.example",
    ".gitignore",
    ".node-version",
    ".npmrc",
    ".python-version",
    "Dockerfile",
}
TEXT_SUFFIXES = {
    ".css",
    ".html",
    ".ini",
    ".js",
    ".json",
    ".md",
    ".py",
    ".toml",
    ".ts",
    ".tsx",
    ".yaml",
    ".yml",
}


def _source_files(root: Path) -> list[Path]:
    return [
        path
        for path in root.rglob("*")
        if path.is_file()
        and not any(part in IGNORED_PARTS for part in path.relative_to(root).parts)
        and (path.name in TEXT_NAMES or path.suffix.lower() in TEXT_SUFFIXES)
    ]


def check_repository(root: Path) -> list[str]:
    root = root.resolve()
    errors: list[str] = []

    if root.name != "market-monitor":
        errors.append(f"repository directory must be named market-monitor, got {root.name}")
    for relative in REQUIRED_FILES:
        if not (root / relative).is_file():
            errors.append(f"missing required file: {relative}")
    for relative in REQUIRED_DIRECTORIES:
        if not (root / relative).is_dir():
            errors.append(f"missing required directory: {relative}")

    gitignore = root / ".gitignore"
    if gitignore.is_file():
        ignored = gitignore.read_text(encoding="utf-8")
        for entry in (".env", ".venv/", "*.tsbuildinfo", "node_modules/", "var/"):
            if entry not in ignored:
                errors.append(f".gitignore must include: {entry}")

    for path in _source_files(root):
        try:
            content = path.read_text(encoding="utf-8-sig")
        except UnicodeDecodeError:
            continue
        relative = path.relative_to(root).as_posix()
        if SECRET_VALUE.search(content):
            errors.append(f"possible secret value in: {relative}")
        if MACHINE_PATH.search(content):
            errors.append(f"machine-specific absolute path in: {relative}")

    generated_error = _check_generated_types(root)
    if generated_error is not None:
        errors.append(generated_error)

    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate M0 repository invariants")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    errors = check_repository(parser.parse_args().root)
    if errors:
        for error in errors:
            print(f"ERROR: {error}")
        return 1
    print("repository checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
