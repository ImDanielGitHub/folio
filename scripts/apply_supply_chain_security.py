from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def write(path: str, content: str) -> None:
    target = ROOT / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


SECURITY_CHECK = r'''from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXCLUDED_PARTS = {".git", "node_modules", ".venv", "dist", "dist-electron", "var"}
TEXT_SUFFIXES = {
    ".py", ".ts", ".tsx", ".cts", ".mjs", ".js", ".json", ".md", ".yml", ".yaml", ".toml", ".env", ""
}
SECRET_PATTERNS = {
    "OpenAI key": re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}\b"),
    "GitHub token": re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b"),
    "AWS access key": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "Plaid access token": re.compile(r"\baccess-(?:sandbox|development|production)-[A-Za-z0-9_-]{12,}\b"),
    "private key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
}
ALLOWED_TEST_MARKERS = {
    "access-sandbox-…",
    "sk-proj keys",
}


def source_files() -> list[Path]:
    return [
        path
        for path in ROOT.rglob("*")
        if path.is_file()
        and not any(part in EXCLUDED_PARTS for part in path.parts)
        and path.suffix.lower() in TEXT_SUFFIXES
        and path.name not in {"pnpm-lock.yaml", "uv.lock"}
    ]


def assert_no_credentials() -> None:
    failures: list[str] = []
    for path in source_files():
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for label, pattern in SECRET_PATTERNS.items():
            for match in pattern.finditer(text):
                if match.group() in ALLOWED_TEST_MARKERS:
                    continue
                failures.append(f"{path.relative_to(ROOT)}: possible {label}")
    if failures:
        raise AssertionError("\n".join(sorted(set(failures))))


def assert_example_secrets_are_blank() -> None:
    values: dict[str, str] = {}
    for line in (ROOT / ".env.example").read_text(encoding="utf-8").splitlines():
        if not line or line.lstrip().startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    secret_names = {
        "OPENAI_API_KEY", "AKAHU_APP_TOKEN", "AKAHU_USER_TOKEN", "PLAID_CLIENT_ID",
        "PLAID_SECRET", "PLAID_ACCESS_TOKEN", "TELEGRAM_BOT_TOKEN", "FOLIO_SESSION_TOKEN",
    }
    populated = sorted(name for name in secret_names if values.get(name))
    if populated:
        raise AssertionError(f".env.example contains populated secret fields: {populated}")


def assert_electron_boundary() -> None:
    main = (ROOT / "apps/desktop/src/main/main.ts").read_text(encoding="utf-8")
    required = {
        "contextIsolation: true": "context isolation",
        "nodeIntegration: false": "Node integration disabled",
        "sandbox: true": "renderer sandbox",
        'setPermissionRequestHandler((_webContents, _permission, callback) => callback(false))': "permission deny handler",
        "setWindowOpenHandler": "new-window denial",
        'protocol.handle("app"': "custom production origin",
    }
    missing = [label for marker, label in required.items() if marker not in main]
    if missing:
        raise AssertionError(f"Electron hardening markers missing: {missing}")


def assert_workflows_use_minimal_permissions() -> None:
    workflow_dir = ROOT / ".github/workflows"
    failures: list[str] = []
    for path in workflow_dir.glob("*.yml"):
        text = path.read_text(encoding="utf-8")
        if "permissions:" not in text:
            failures.append(f"{path.name}: no explicit permissions")
        if "permissions: write-all" in text:
            failures.append(f"{path.name}: write-all is forbidden")
    if failures:
        raise AssertionError("\n".join(failures))


def main() -> int:
    try:
        assert_no_credentials()
        assert_example_secrets_are_blank()
        assert_electron_boundary()
        assert_workflows_use_minimal_permissions()
    except AssertionError as error:
        print(f"security:static FAILED\n{error}", file=sys.stderr)
        return 1
    print("security:static PASS (credentials, example env, Electron boundary, workflow permissions)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''

DEPENDABOT = '''version: 2
updates:
  - package-ecosystem: npm
    directory: /
    schedule:
      interval: weekly
      day: monday
      time: "08:00"
      timezone: Pacific/Auckland
    open-pull-requests-limit: 5
    groups:
      javascript-runtime:
        dependency-type: production
      javascript-development:
        dependency-type: development
    commit-message:
      prefix: deps

  - package-ecosystem: pip
    directory: /services/api
    schedule:
      interval: weekly
      day: monday
      time: "08:30"
      timezone: Pacific/Auckland
    open-pull-requests-limit: 5
    groups:
      python-runtime:
        dependency-type: production
      python-development:
        dependency-type: development
    commit-message:
      prefix: deps

  - package-ecosystem: github-actions
    directory: /
    schedule:
      interval: weekly
      day: monday
      time: "09:00"
      timezone: Pacific/Auckland
    open-pull-requests-limit: 5
    commit-message:
      prefix: ci
'''

SECURITY_WORKFLOW = '''name: Security

on:
  pull_request:
  push:
    branches: [main]
  schedule:
    - cron: "17 19 * * 1"
  workflow_dispatch:

permissions:
  contents: read

concurrency:
  group: security-${{ github.workflow }}-${{ github.ref }}
  cancel-in-progress: true

jobs:
  dependency-audit:
    runs-on: ubuntu-latest
    timeout-minutes: 25
    steps:
      - uses: actions/checkout@v4
      - uses: pnpm/action-setup@v4
        with:
          version: 10.33.0
          run_install: false
      - uses: actions/setup-node@v4
        with:
          node-version: 22
          cache: pnpm
          cache-dependency-path: pnpm-lock.yaml
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - uses: astral-sh/setup-uv@v6
        with:
          version: "0.10.0"
          enable-cache: true
      - run: pnpm install --frozen-lockfile
      - run: uv sync --project services/api --frozen
      - run: pnpm security:static
      - run: pnpm audit --audit-level=high
      - run: uv run --project services/api pip-audit

  dependency-review:
    if: github.event_name == 'pull_request'
    runs-on: ubuntu-latest
    permissions:
      contents: read
      pull-requests: read
    steps:
      - uses: actions/checkout@v4
      - uses: actions/dependency-review-action@v4
        with:
          fail-on-severity: high
          license-check: true
          deny-licenses: AGPL-1.0-only, AGPL-1.0-or-later, SSPL-1.0

  codeql:
    runs-on: ubuntu-latest
    permissions:
      security-events: write
      packages: read
      contents: read
    strategy:
      fail-fast: false
      matrix:
        language: [javascript-typescript, python]
    steps:
      - uses: actions/checkout@v4
      - uses: github/codeql-action/init@v3
        with:
          languages: ${{ matrix.language }}
      - uses: github/codeql-action/analyze@v3
        with:
          category: /language:${{ matrix.language }}

  secret-scan:
    runs-on: ubuntu-latest
    permissions:
      contents: read
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0
      - uses: gitleaks/gitleaks-action@v2
        env:
          GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}
'''

THREAT_MODEL = '''# Folio threat model

## Scope

This model covers the Electron renderer and main process, the loopback FastAPI service, SQLite workspace data, model adapters, imported files, provider connectors, generated artefacts, backups, scheduler jobs, and GitHub release pipeline.

## Assets

1. Bank transactions, owner statements, documents, evidence and generated reports.
2. Provider credentials and the per-launch loopback session token.
3. Deterministic finance state, event history, provenance and Undo integrity.
4. Backup archives and destruction receipts.
5. Source code, dependency lockfiles, build artefacts and signing identities.

## Trust boundaries

- Renderer to preload IPC.
- Renderer to loopback HTTP and SSE.
- FastAPI service to SQLite and local files.
- Service to LM Studio loopback.
- Service to explicitly configured cloud/model and bank-provider hosts.
- Repository to third-party package registries and GitHub Actions.

## Primary threats and controls

| Threat | Required control |
|---|---|
| Malicious webpage or renderer calls the loopback API | Per-launch session token, strict CORS/Host checks, custom production origin, request receipts |
| Prompt injection changes finance truth | Closed plans, deterministic finance code, bounded tools, narrative validation |
| Provider payload corrupts ledger currency | Provider event quarantine, explicit currency authority, no implicit FX |
| Local file import exhausts memory or disk | Request and file limits, incremental reads, fail-closed parsing |
| Backup tampering or wrong passphrase | SHA-256 manifests, AES-GCM authentication, SQLite integrity checks |
| Dependency or action compromise | Lockfiles, Dependabot, CodeQL, dependency review, package audits, secret scanning |
| Owner destroys data accidentally | Exact confirmation, encrypted export before wipe, external destruction receipt |
| Secret appears in logs or fixtures | Process-scoped secrets, redacted errors, static credential scanner, fictional fixtures |
| Stale or duplicated scheduled work | Leases, idempotency keys, durable scheduler state and receipts |

## Explicitly unresolved

- Transparent encryption of the active SQLite database and platform-backed key recovery.
- Signing and notarisation identities for production packages.
- Accredited Akahu OAuth credential lifecycle and real-account acceptance.
- Independent penetration testing and incident response exercise.

These unresolved items must not be described as implemented merely because related source or documentation exists.
'''

DEPENDENCY_POLICY = '''# Dependency policy

Folio minimises dependencies because every package expands the attack and maintenance surface around sensitive financial data.

## Admission criteria

A runtime dependency must provide a capability that is materially safer or more reliable than an in-house implementation, have an active upstream, a clear licence, reproducible installation through the committed lockfile, and focused tests at Folio's boundary.

Cryptography is never implemented from scratch. Provider SDKs are optional when direct, pinned HTTP contracts are smaller and easier to audit. A dependency used only in CI or development belongs in the development group.

## Update policy

- Dependabot checks JavaScript, Python and GitHub Actions weekly.
- High-severity advisories block the security workflow.
- Dependency review blocks newly introduced high-severity advisories and denied licences.
- Lockfile changes must be reviewed with the manifest change that caused them.
- Major upgrades require focused compatibility tests and a short migration note.

## Licence policy

Apache-2.0, MIT, BSD, ISC and similarly permissive dependencies are ordinarily acceptable. Copyleft or source-available licences require an explicit compatibility review. AGPL and SSPL dependencies are denied by the automated dependency review unless the repository owner records a deliberate exception.
'''

PR_TEMPLATE = '''## What changed

## Why this boundary

## Verification evidence

- [ ] `pnpm verify`
- [ ] `pnpm security:static`
- [ ] User-visible runtime evidence attached when applicable

## Proof boundary

State what remains unverified. Do not collapse source, tests, build, observed runtime, provider calls, packaging, or release state into one claim.

## Data and external effects

- [ ] No real financial/customer data added
- [ ] No credentials, tokens, cookies or private URLs added
- [ ] New egress or connector behaviour documented
- [ ] Destructive actions and migration/rollback behaviour documented
'''

CODEOWNERS = '''# Security and finance truth boundaries require owner review.
/services/api/src/finance_agent/finance/ @ImDanielGitHub
/services/api/src/finance_agent/storage/ @ImDanielGitHub
/services/api/src/finance_agent/connectors/ @ImDanielGitHub
/services/api/src/finance_agent/models/ @ImDanielGitHub
/apps/desktop/src/main/ @ImDanielGitHub
/apps/desktop/src/preload/ @ImDanielGitHub
/.github/workflows/ @ImDanielGitHub
/contracts/ @ImDanielGitHub
'''


def add_security_files() -> None:
    write("scripts/security_check.py", SECURITY_CHECK)
    write(".github/dependabot.yml", DEPENDABOT)
    write(".github/workflows/security.yml", SECURITY_WORKFLOW)
    write(".github/pull_request_template.md", PR_TEMPLATE)
    write(".github/CODEOWNERS", CODEOWNERS)
    write("docs/THREAT_MODEL.md", THREAT_MODEL)
    write("docs/DEPENDENCY_POLICY.md", DEPENDENCY_POLICY)


def update_manifests() -> None:
    path = "services/api/pyproject.toml"
    content = read(path)
    marker = '  "mypy>=1.17,<2",\n'
    if '"pip-audit>=' not in content:
        if marker not in content:
            raise RuntimeError("Python dev dependency marker missing")
        content = content.replace(marker, marker + '  "pip-audit>=2.9,<3",\n', 1)
        write(path, content)

    path = "package.json"
    value = json.loads(read(path))
    scripts = value["scripts"]
    scripts["security:static"] = "uv run --project services/api python scripts/security_check.py"
    verify = scripts["verify"]
    if "security:static" not in verify:
        scripts["verify"] = verify.replace("pnpm contracts:check", "pnpm security:static && pnpm contracts:check", 1)
    write(path, json.dumps(value, indent=2) + "\n")


def update_docs() -> None:
    path = "SECURITY.md"
    content = read(path)
    addition = '''\n## Automated assurance\n\nEvery pull request runs the static credential and Electron-boundary check. The permanent security workflow runs package audits, dependency review, CodeQL and full-history secret scanning. See [docs/THREAT_MODEL.md](docs/THREAT_MODEL.md) and [docs/DEPENDENCY_POLICY.md](docs/DEPENDENCY_POLICY.md).\n'''
    if "## Automated assurance" not in content:
        write(path, content.rstrip() + addition + "\n")

    path = "docs/AUDIT_PROGRAMME.md"
    content = read(path)
    section = '''\n\n## Stack 10: supply-chain and repository security\n\n- A permanent static check detects credential patterns, populated example secrets, Electron hardening drift, and workflows without explicit permissions.\n- Weekly Dependabot coverage spans pnpm, Python, and GitHub Actions.\n- Pull requests receive dependency review with high-severity and licence gates.\n- CodeQL analyses Python and TypeScript, while Gitleaks scans full history.\n- High-severity npm and Python advisories block the security workflow.\n- Threat-model, dependency-policy, CODEOWNERS, and pull-request proof templates are committed.\n'''
    if "## Stack 10: supply-chain and repository security" not in content:
        write(path, content.rstrip() + section + "\n")


def main() -> None:
    add_security_files()
    update_manifests()
    update_docs()
    print("supply chain security changes applied")


if __name__ == "__main__":
    main()
