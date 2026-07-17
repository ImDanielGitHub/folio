# Standalone Finance Agent

This repository is a new, local-first desktop finance product for New Zealand sole traders. Its P0 experience is one continuing owner thread beside one stateful financial canvas. The user-facing product is not a generic chatbot, dashboard, queue manager or ledger replacement.

## Bootstrap status

This commit is the coordinator bootstrap lock described by `BUILD_CONTRACT.md`. It establishes frozen contracts, canonical synthetic fixtures, ports, commands and lane ownership. It does **not** claim that the finance engine, agent harness, API or Electron experience has been implemented or run.

Canonical runtime assumptions:

- Python `3.12`
- Node.js `22` or newer
- `pnpm 10.33.0`
- `uv 0.9.25` or compatible
- API `127.0.0.1:4317` (loopback only)
- renderer port selected by Vite
- LM Studio default adapter endpoint `127.0.0.1:1234/v1`

## Install and bootstrap checks

```bash
pnpm install
uv sync --project services/api --all-groups
pnpm contracts:check
```

`contracts:check` is real at bootstrap: it validates every manifest-listed contract example and UI fixture against the canonical Draft 2020-12 JSON Schemas and checks the demo CSV arithmetic and canonical IDs.

The remaining required commands are present but intentionally stop with `not implemented by lane yet` until the responsible implementation lane lands:

```bash
pnpm lint
pnpm typecheck
pnpm test
pnpm dev
pnpm demo:reset
pnpm demo:daily-close
pnpm test:golden
pnpm test:local-model
pnpm test:cloud-model
pnpm test:telegram-live
```

## Frozen ownership

| Lane | Owns | Does not own |
|---|---|---|
| Task 1: finance core | `finance/`, `storage/`, `jobs/`, `artifacts/`, matching tests, `fixtures/demo/` | contracts, root scripts/locks, API composition, agent/model/connectors, Electron |
| Task 2: harness/API | `agent/`, `models/`, `connectors/`, `api/routes/`, matching tests, `evals/` | finance calculations, migrations, contracts, root scripts/locks, Electron |
| Task 3: desktop | `apps/desktop/`, `fixtures/ui/` | backend, contracts, shared demo data, root scripts/locks |
| Coordinator | contracts, root workspace/locks/docs/scripts, Python manifest/lock, API composition, integration and golden proof | lane implementation while builders are isolated |

No lane may silently change `/contracts`. Proposed dependency changes return to the coordinator so the shared locks remain a single integration boundary.

## Canonical data

`fixtures/demo/` contains the entirely synthetic “Koru Studio” source story and its fixed IDs/outcomes. `fixtures/ui/` contains canonical producer snapshots so the desktop lane can work without a backend. Monetary JSON fields use integer NZD minor units; the CSV uses signed integer minor units.

See `BOOTSTRAP_RECEIPT.md` for the exact schema list, fixture IDs and content hashes. See `CLEAN_ROOM.md`, `BUILD_WEEK.md`, `ATTRIBUTION.md` and `LICENSE` for origin and release boundaries.
