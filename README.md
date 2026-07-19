# Folio

Folio is a provisional, local-first desktop finance workspace for New Zealand sole traders. Its P0 experience is one continuing owner conversation beside one stateful financial canvas. Koru Studio is the entirely synthetic demo workspace bundled with the repository.

## Prototype status

The repository contains the deterministic finance core, bounded agent harness, loopback FastAPI service, safe contract renderer and Electron/Vite/React desktop client described by `BUILD_CONTRACT.md` and `REFERENCE_UI_DECISION.md`. Folio runs locally by default; model output cannot execute HTML, JavaScript or React in the renderer.

Canonical runtime assumptions:

- Python `3.12`
- Node.js `22` or newer
- `pnpm 10.33.0`
- `uv 0.9.25` or compatible
- API `127.0.0.1:8787` (loopback only)
- browser renderer `127.0.0.1:4173`
- LM Studio default adapter endpoint `127.0.0.1:1234/v1`

## Install and run

```bash
pnpm install --frozen-lockfile
uv sync --project services/api --frozen
pnpm api
pnpm dev:browser
```

Open `http://127.0.0.1:4173`. Electron can be started with `pnpm dev:electron` once the renderer is running. The browser client honestly falls back to sealed Koru Studio fixtures when the local API is unavailable.

## Judge path (local)

```bash
pnpm install:all
pnpm api                 # 127.0.0.1:8787
pnpm dev:browser         # 127.0.0.1:4173
```

Then in another terminal:

```bash
pnpm contracts:check
pnpm --filter @folio/desktop typecheck
pnpm demo:reset-hashes   # 5× offline reset + Akahu fixture, one canonical hash
pnpm demo:golden         # full Koru HTTP flow including Akahu fixture
pnpm eval:offline        # offline plan-parser harness
```

Open `http://127.0.0.1:4173`. Walk onboarding with **Connect with Akahu** (fixture path), then Sources / Cash / Activity. With the API up, the header shows **Local**; without LM Studio, the fallback banner stays honest.

## Verify

```bash
pnpm contracts:check
pnpm lint
pnpm typecheck
pnpm test:python
pnpm build
pnpm test:golden
pnpm demo:reset
pnpm demo:daily-close
pnpm demo:reset-hashes
pnpm demo:golden
```

`contracts:check` validates every manifest-listed example and UI fixture against the canonical closed Draft 2020-12 schemas, then checks the synthetic CSV arithmetic and canonical IDs. Live cloud, live Akahu OAuth and Telegram-provider checks remain deliberately gated; the committed prototype never reads credentials or calls an external provider. The sealed Akahu fixture at `fixtures/demo/akahu-sync.json` is first-class for the golden path.

## Frozen ownership

| Lane | Owns | Does not own |
|---|---|---|
| Task 1: finance core | `finance/`, `storage/`, `jobs/`, `artifacts/`, matching tests, `fixtures/demo/` | contracts, root scripts/locks, API composition, agent/model/connectors, Electron |
| Task 2: harness/API | `agent/`, `models/`, `connectors/`, `api/routes/`, matching tests, `evals/` | finance calculations, migrations, contracts, root scripts/locks, Electron |
| Task 3: desktop | `apps/desktop/`, `fixtures/ui/` | backend, contracts, shared demo data, root scripts/locks |
| Coordinator | contracts, root workspace/locks/docs/scripts, Python manifest/lock, API composition, integration and golden proof | lane implementation while builders are isolated |

No lane may silently change `/contracts`. Dependency and contract changes return to the coordinator so shared locks remain one integration boundary.

## Canonical data

`fixtures/demo/` contains the entirely synthetic “Koru Studio” source story and its fixed IDs/outcomes. `fixtures/ui/` contains canonical producer snapshots so the desktop lane can work without a backend. Monetary JSON fields use integer NZD minor units; the CSV uses signed integer minor units.

See `BOOTSTRAP_RECEIPT.md` for the frozen bootstrap hashes and `PROTOTYPE_RECEIPT.md` for current runtime proof. `CLEAN_ROOM.md`, `BUILD_WEEK.md`, `ATTRIBUTION.md` and `LICENSE` define origin and release boundaries.

## Licence status

Package metadata is intentionally `UNLICENSED`. `LICENSE` records Apache-2.0 as the intended provisional direction, but no open-source grant takes effect until legal and ownership confirmation is complete.
