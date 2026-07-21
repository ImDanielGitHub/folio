# Folio

Folio is a local-first finance operator for New Zealand sole traders and small businesses. It starts as one calm conversation, quietly maintains a source-linked picture of the business, and opens a financial canvas only when an answer needs a chart, table, transaction, evidence view or prepared document.

This repository contains a working Build Week prototype. It is not a bank, accounting ledger, payment product, tax-filing service or financial adviser.

## The product loop

The sample business demonstrates one end-to-end workflow:

1. Folio ingests a synthetic bank CSV and runs an idempotent Daily Close.
2. Deterministic services reconcile transactions, flag a likely duplicate, surface an unsupported expense and calculate a 30-day cash scenario.
3. The conversation leads with the practical meaning instead of exposing a workflow engine.
4. The owner explains a MITRE 10 purchase naturally and at length.
5. Folio stores the full statement, applies a narrowly scoped correction, preserves the previous understanding as superseded history and exposes Undo.
6. A cash scenario or owner pack appears beside the conversation only when requested.
7. Every material amount remains linked to committed source evidence.

The model plans and explains; deterministic finance code owns amounts, transaction selection, effects, forecasts, evidence and generated documents.

## What is implemented

| Capability | Current proof |
|---|---|
| Chat-first Electron/React workspace | Source, TypeScript build and browser runtime |
| Dynamic finance canvas | Living brief, transaction, cash scenario, records, owner pack and receipt surfaces |
| Deterministic finance authority | Exact minor-unit arithmetic, fixture contracts and Python tests |
| Daily Close | Idempotent local API workflow over the synthetic fixture |
| Durable working understanding | Immutable owner statements, structured facts, provenance, retrieval receipts, contradiction tracking and supersession |
| Long-conversation continuity | Restart/model-switch integration test with early-turn retrieval and correction |
| Small-model harness | Closed plan schemas, bounded parsing/repair, validation, loop limits and deterministic fallback |
| Local model route | LM Studio loopback adapter and capability discovery; one Qwen 3.5 9B transport smoke recorded separately |
| Optional cloud route | Thin OpenAI Responses API adapter; no live credential is required for local or fixture operation |
| Evidence-backed artefacts | Deterministic HTML/PDF owner-pack generator and source links |
| Telegram-shaped input | Synthetic fixture adapter only; no real bot or owner account is connected |
| Akahu connector | Sealed NZ fixture by default; optional config-gated read-only live sync |
| Plaid connector | Sealed US fixture by default; optional config-gated sandbox Link / sync |

See [PROTOTYPE_RECEIPT.md](PROTOTYPE_RECEIPT.md) for current verification and explicit gaps.

## Architecture

```text
Electron + React conversation/canvas
                |
        loopback HTTP + SSE
                |
        FastAPI application service
                |
   bounded planner / executor / verifier
          |                 |
 deterministic finance   model router
          |              /            \
   SQLite + artefacts  LM Studio     OpenAI
```

Important boundaries:

- SQLite is the durable source for conversations, finance events, findings, evidence, model receipts and working understanding.
- Source records and owner statements are immutable. Corrections append a replacement and supersession link instead of overwriting history.
- Local, Hybrid and Cloud change model routing, not finance truth or conversation identity.
- The renderer accepts a closed `FinanceSurfaceSpec@1` catalogue. Models cannot emit executable UI code.
- The local API binds only to loopback.

## Quick start

### Requirements

- Node.js 22 or newer
- pnpm 10.33.0
- Python 3.12
- uv 0.9.25 or compatible

### Install

```bash
pnpm install --frozen-lockfile
uv sync --project services/api --frozen
```

### Run the live local application

```bash
./run
```

This starts the FastAPI service on `127.0.0.1:8787`, the Vite renderer on `127.0.0.1:4173`, and the Folio Electron app. Stop it with `Ctrl+C`.

For separate development processes, run:

```bash
pnpm dev
pnpm dev:electron
```

No account or external model is needed for the example workspace. For the intended conversational experience, load a tool-capable model in LM Studio and enable its local server at `127.0.0.1:1234` before opening Folio.

### Open the sealed UI fixture

For UI review without the backend:

```bash
pnpm dev:browser
```

Then open `http://127.0.0.1:4173/?demo=1`. Fixture mode never calls LM Studio or a cloud provider.

## Verification

```bash
pnpm contracts:check
pnpm lint
pnpm typecheck
pnpm test
pnpm build
pnpm test:golden
pnpm eval:offline
```

Optional live local-model evaluation, only when LM Studio already has the configured synthetic-test model loaded:

```bash
FOLIO_LM_STUDIO_MODEL=folio-qwen3.5-9b pnpm eval:lmstudio:live
```

The offline evaluation compares raw model JSON acceptance with Folio's bounded repair and validation path. It is parser/harness evidence, not a claim that every local model performs equally well. The live runner reports model-authored plan accuracy separately from effective accuracy after deterministic fallback.

## Model modes and privacy

- **Local** uses LM Studio on `127.0.0.1`. If the server or selected model is unavailable, Folio reports that state and uses a bounded deterministic fallback; it does not silently call the cloud.
- **Hybrid** keeps finance computation local and permits only a typed projection to the configured cloud adapter.
- **Cloud** may use the OpenAI Responses API for planning/explanation, but deterministic finance services still own amounts and effects.

Model selection is in the quiet Privacy & Models drawer, not in the ordinary conversation flow. No telemetry is included. Do not place credentials, real financial exports, bot updates or customer documents in this repository.

## Synthetic data

All committed data under `fixtures/` is fictional. The example business, its people, accounts, transactions, documents, dates and identifiers are demo material created for this project. Reset with:

```bash
pnpm demo:reset
pnpm demo:daily-close
pnpm demo:golden
```

Repeated ingestion and Daily Close runs use digests and idempotency keys so the same source does not silently create duplicate finance effects.

## Build Week provenance

Folio is a new standalone repository created during OpenAI Build Week. The bootstrap and every implementation commit are dated after the submission period opened. It independently reimplements useful architectural principles learned from public documentation and clean-room inspection; it does not contain Hermes or Bionic branding, proprietary prompts, minified bundles or UI assets.

The key implementation commits are recorded in [BUILD_WEEK.md](BUILD_WEEK.md). Clean-room and reference boundaries are in [CLEAN_ROOM.md](CLEAN_ROOM.md), [SOURCE_REUSE_MAP.md](SOURCE_REUSE_MAP.md), [ATTRIBUTION.md](ATTRIBUTION.md) and [REFERENCE_UI_DECISION.md](REFERENCE_UI_DECISION.md).

Codex with GPT-5.6 was used for research, architecture, implementation, test generation, debugging, integration and adversarial review. The application also contains an optional GPT-5.6 Responses API adapter, but a live cloud runtime result must not be claimed unless separately recorded.

## Current limits

- The default data source is a synthetic fixture, local CSV, or sealed Akahu/Plaid connector feed; live bank sync is config-gated and off by default.
- Telegram support is fixture-backed. No real bot round trip is part of the proof.
- The cash forecast is a deterministic scenario over known fixture commitments, not predictive certainty.
- A local-model transport smoke is recorded; the four-case live model benchmark is optional and may not have been run on the current machine state.
- No public deployment, packaged judge build, public video or final Devpost submission is created by this repository.
- Folio is free and open-source software licensed under the Apache License 2.0. Provider credentials and third-party services remain subject to their own terms.

## Repository map

```text
apps/desktop/                 Electron, React and the closed finance canvas
services/api/src/finance_agent/
  agent/                      bounded planning and context assembly
  api/                        loopback routes and working-understanding bridge
  finance/                    deterministic finance services
  storage/                    SQLite events, claims, facts and retrieval receipts
  jobs/                       Daily Close
  models/                     LM Studio, OpenAI and narrative guard
  connectors/                 Akahu boundary and Telegram fixture adapter
  artifacts/                  owner-pack HTML/PDF generation
contracts/                    JSON Schema contracts and examples
fixtures/                     fictional business data and UI snapshots
evals/                        offline and optional live harness evaluations
scripts/                      reset, contract and golden-flow commands
```

## Licence

Folio is licensed under the [Apache License 2.0](LICENSE).
