# Build contract: standalone local-first finance agent

**Status:** architecture contract for implementation. The target was empty when research began; a separate coordinator bootstrap (root manifests, policy docs and empty lane placeholders) appeared concurrently while this brief was being written. It is builder-owned and was not modified here. No implemented product runtime was present at the final audit.  
**Working directory:** `/Users/dananeke/Documents/Finace App`  
**Date locked:** 17 July 2026, Pacific/Auckland  
**Primary mode:** execute a post-start, demonstrably new vertical slice for OpenAI Build Week.  
**Product identity:** standalone product. It may adapt attributed, licence-compatible patterns from Hermes Finance, but it must not inherit Hermes branding, routes, generic-agent information architecture, or repository identity.

## 1. Outcome contract

Build one credible loop in which a small-business owner discovers that the finance agent has already completed useful internal work, corrects it naturally, sees the correction become a durable but reversible rule, explores the resulting cash risk in a dynamic canvas, receives an evidence-linked owner pack, and can demonstrate a Telegram expense/alert path.

The proof is not “a dashboard with chat”. The proof is this state transition:

```text
new source arrives
  -> idempotent Daily Close runs in the background
  -> deterministic finance services create findings and a 30-day cash projection
  -> the continuing thread reports only the material change
  -> the user corrects one classification in natural language
  -> a narrow rule is saved and applied automatically under the standing mandate
  -> the canvas and forecast update from the same committed event
  -> Activity records before/after evidence and offers Undo
  -> an owner pack and Telegram brief reference the same run
```

### Required proof levels

| Deliverable | Minimum proof |
|---|---|
| Deterministic finance core | Unit and fixture tests with exact minor-unit amounts, evidence IDs and idempotency assertions |
| Agent harness | Local-model and cloud-adapter contract tests; malformed plans fail closed; model prose never supplies ledger totals |
| Desktop experience | Live Electron interaction at 1440×900 and 1280×800, including thread, canvas, drawers, loading, offline and Undo states |
| Golden vertical slice | Five consecutive clean runs from `demo:reset`, with the same expected receipt, forecast and artefact hashes except timestamps/IDs explicitly normalised |
| Telegram | Fixture-driven path is mandatory; one real bot round trip is optional and must use a test bot/account, never real financial data |
| OpenAI Build Week | Baseline/post-start boundary and the exact new commits are documented; GPT-5.6 cloud run is recorded if credentials and model access are available |

## 2. Frozen product surface

There are only two permanent primary panes:

1. **Continuing thread** — one thread per business; no thread picker in the P0 interface. It contains owner messages, short agent updates, adaptive questions, source chips and lightweight work receipts.
2. **Financial canvas** — one replaceable, stateful surface. It shows exactly one of: `living_brief`, `transaction_detail`, `cash_scenario`, `records_table`, `owner_pack`, or `work_receipt`.

Three secondary surfaces are drawers/sheets, not destinations:

- **Sources** — imported files/messages, freshness, provenance and evidence rows.
- **Activity & Undo** — background runs, committed events, before/after diffs, failures and rollback.
- **Connections & Privacy** — Local/Hybrid/Cloud selection, LM Studio status, optional Telegram state, egress policy and data-use receipts.

No P0 route may introduce a conventional dashboard, generic chat home, queue-management screen, agent-plan view, widget editor, bank-link wizard, full chart of accounts, invoice suite or admin console.

## 3. Frozen vertical slice

### Included

- Seeded New Zealand sole-trader workspace and deterministic reset.
- CSV transaction ingestion with source digest, row number, import mapping version and duplicate prevention.
- An always-on local job loop plus a manual “Run close now” demo trigger.
- Daily Close stages: ingest pending sources, normalise, detect duplicates/transfers, apply user rules, classify or propose classifications, compute material findings, build 30-day cash projection, prepare owner pack, write run receipt, optionally enqueue Telegram brief.
- Three visible findings: one missing-document/unresolved expense, one anomalous or duplicate row, and one reserve-risk cash trough.
- One open-ended conversation that can ask a single adaptive question at a time, accept a long answer, and synthesise immediately when the owner stops or changes topic.
- One natural-language correction that creates a narrowly scoped rule and automatically reprocesses affected transactions.
- One-click Undo of that correction event and all derived projections.
- Local mode through LM Studio; Cloud mode through OpenAI; Hybrid mode that computes locally and sends only a typed projection allowed by policy.
- A generated HTML preview and PDF owner pack from the same deterministic DTO.
- Telegram fixture ingestion for an expense message/photo reference and a reserve-risk outgoing brief. Real Bot API polling/sending is optional behind configuration.
- Source-linked answers, activity receipts, data-through timestamp, forecast assumptions and uncertainty language.

### Explicitly excluded

- Live bank connections, Open Banking/CDR, Akahu/Plaid onboarding or production webhooks.
- Payments, bill execution, card controls, journal posting to an external ledger, tax filing or financial advice.
- Receipt OCR as a gating dependency. P0 stores the image and parses an explicit caption/fixture; Docling/OCR is a P1 adapter.
- A general double-entry ledger. P0 is a source-linked bookkeeping-preparation system. Beancount/accounting-system export is P1.
- Multi-entity, multi-currency conversion, payroll, inventory, accounts payable/receivable and accountant collaboration.
- Arbitrary model-generated HTML, JavaScript, SQL, Python or component trees.
- Multi-agent delegation. P0 uses one controller and deterministic services.
- Hosted sync, authentication, deployment, publishing or telemetry.

## 4. Technical spine

### 4.1 Repository shape

```text
Finace App/
├── apps/
│   └── desktop/                 # Electron + React renderer
├── services/
│   └── api/                     # Python local service
│       ├── src/finance_agent/
│       │   ├── agent/
│       │   ├── api/
│       │   ├── artifacts/
│       │   ├── connectors/
│       │   ├── finance/
│       │   ├── jobs/
│       │   ├── models/
│       │   └── storage/
│       └── tests/
├── contracts/                   # canonical JSON Schemas and example envelopes
├── fixtures/                    # shared synthetic demo inputs and expected outputs
├── scripts/                     # reset, run, smoke and golden-demo commands
├── evals/                       # model-independent and model-specific fixture runners
├── package.json
├── pnpm-workspace.yaml
└── README.md
```

### 4.2 Runtime decision

- **Desktop:** Electron, Vite, React and TypeScript. Electron owns process lifecycle, local file selection and the typed preload boundary. The renderer owns only interaction and presentation.
- **Local service:** Python 3.12, FastAPI, Pydantic v2, SQLite and `uv`. Python is selected because the existing finance evidence is Python, deterministic numeric/document work is strong there, and future document/forecast adapters are materially easier.
- **Transport:** loopback HTTP plus server-sent events. A turn is submitted with HTTP POST; ordered run events stream over SSE. Do not add WebSockets until bidirectional mid-run steering is genuinely required.
- **Canonical store:** one local SQLite database per workspace. Integer minor units plus ISO currency are mandatory. Source rows are immutable; meaning changes are append-only events; materialised projections may be rebuilt.
- **Model clients:** direct, thin adapters. Local uses LM Studio’s OpenAI-compatible endpoint and JSON Schema structured output. Cloud uses the OpenAI Responses API. Do not make OpenAI Agents SDK, LangGraph, PydanticAI, AG-UI or A2UI a required runtime for P0.
- **UI state:** React Query for server snapshots, a small ordered event reducer for SSE, and `@ai-sdk/react` only if its custom transport reduces work without forcing the backend into a foreign message format. The backend event contract remains ours.
- **Canvas:** a versioned `FinanceSurfaceSpec`, validated at both producer and renderer. The catalogue is application-owned and A2UI-inspired; it is not arbitrary generative UI.

### 4.3 Finance truth model

The required SQLite concepts are:

- `workspaces`, `accounts`, `source_items`, `source_rows`, `transactions`
- `classification_rules`, `finance_events`, `event_effects`
- `job_definitions`, `job_runs`, `job_stage_runs`, `outbox_messages`
- `conversation_turns`, `dialogue_frames`, `claims`
- `findings`, `forecast_points`, `artifacts`, `evidence_links`
- `model_runs`, `egress_receipts`

Every semantic mutation has an event with:

```text
event_id, workspace_id, event_type, actor, occurred_at,
source_turn_id, reason, before_json, after_json,
scope_json, evidence_ids[], inverse_event_json, correlation_id
```

Undo appends and applies the inverse event. It never deletes history. Derived tables are recomputed within the same transaction or by an idempotent follow-up job keyed by the event ID.

### 4.4 Bounded agent harness

The controller is a state machine, not an open-ended ReAct loop:

```text
LOAD_CONTEXT
  -> COMPILE_PLAN
  -> VALIDATE_PLAN
  -> EXECUTE_READS
  -> [ASK_ONE_QUESTION | EXECUTE_REVERSIBLE_WRITE]
  -> RECOMPUTE
  -> SELECT_SURFACE
  -> EXPLAIN
  -> COMMIT_RECEIPT
```

`FinancePlan` has a maximum of five actions and only these P0 action kinds:

- `query_summary`
- `query_transactions`
- `run_cash_scenario`
- `record_business_claim`
- `create_classification_rule`
- `undo_event`
- `prepare_owner_pack`
- `show_surface`

Rules for small local models:

- Expose at most the tools required for the current state.
- Use flat, closed JSON Schemas: enums, required fields, `additionalProperties: false`, short descriptions and bounded arrays.
- Generate one plan, validate, and permit one schema-repair attempt. Then fall back to a read-only answer or one natural clarification.
- Never let the model calculate totals, balances, tax, forecast values or the set of affected transaction IDs.
- Never execute a tool name, argument, surface type or mutation outside the validated catalogue.
- Never send raw ledger history when a deterministic projection will answer the question.
- Store the owner’s explicit claims separately from model inferences, with source turn, scope, effective date and supersession.

### 4.5 Model modes

| Mode | Computation | Model data | Required receipt |
|---|---|---|---|
| Local | All deterministic work and model inference local | Bounded local projections | Model/capability/latency only; no egress |
| Hybrid | Finance computation local; selected language task cloud | Projection fields explicitly allowed by policy | Provider, model, field classes, counts, purpose, time |
| Cloud | Finance computation still local; orchestration/explanation cloud | Same projection compiler, broader allowed policy | Same egress receipt; raw source files remain excluded by default |

Mode switching must not fork the conversation, memory or canvas. A `DialogueFrame` and finance event store are provider-independent.

### 4.6 UI event envelope

Every stream item is JSONL/SSE data matching one of:

```text
run.started
message.delta
message.completed
stage.started
stage.completed
tool.started
tool.completed
state.snapshot
state.patch
surface.replace
surface.patch
receipt.committed
run.failed
run.completed
```

All carry `eventId`, `threadId`, `runId`, `sequence`, `occurredAt`, `type` and typed `payload`. The renderer rejects duplicates and gaps; after a gap it requests a full snapshot. This adopts AG-UI’s lifecycle and snapshot/delta strengths without importing the entire protocol in P0.

### 4.7 Canvas contract

`FinanceSurfaceSpec@1` contains:

- `surfaceId`, `surfaceType`, `title`, `subtitle`, `freshness`, `blocks[]`, `actions[]`
- block catalogue: `narrative`, `metric`, `cash_series`, `scenario_compare`, `transaction_rows`, `finding`, `source_list`, `change_diff`, `artifact_preview`
- action catalogue: `focus_source`, `open_drawer`, `run_scenario`, `undo_event`, `download_artifact`

The model may request a `surfaceType` and supply narrative labels. Deterministic code supplies block data and allowable actions. The renderer maps the closed catalogue to native React components. It never renders model HTML.

## 5. Canonical API boundary

The implementation must provide:

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/health` | service, database and model-adapter readiness |
| `POST` | `/v1/demo/reset` | recreate the synthetic workspace deterministically |
| `POST` | `/v1/ingest/csv` | ingest a selected CSV source |
| `POST` | `/v1/ingest/telegram-fixture` | ingest one fixture Update and attachment reference |
| `POST` | `/v1/jobs/daily-close` | enqueue an idempotent manual close |
| `GET` | `/v1/jobs/{run_id}/events` | ordered SSE stream |
| `POST` | `/v1/threads/{thread_id}/turns` | submit an owner turn and return `runId` |
| `GET` | `/v1/workspaces/{workspace_id}/snapshot` | thread, current surface, findings, activity and sources |
| `POST` | `/v1/events/{event_id}/undo` | append/apply inverse event |
| `GET` | `/v1/artifacts/{artifact_id}` | serve local HTML/PDF artefact |
| `GET` | `/v1/models/capabilities` | Local/Cloud adapter readiness without exposing secrets |

Contracts live in `/contracts`; fixtures are the mock server for parallel UI development. No task may silently change a schema after the bootstrap lock.

## 6. Three parallel implementation tasks

Before dispatch, the coordinator creates one bootstrap commit containing root workspace files, the frozen `/contracts`, shared `/fixtures`, empty module entrypoints and commands that fail with explicit “not implemented” messages. The three tasks branch from that exact commit.

### Task 1 — deterministic finance core and background work

**Owns:**

- `services/api/src/finance_agent/finance/**`
- `services/api/src/finance_agent/storage/**`
- `services/api/src/finance_agent/jobs/**`
- `services/api/src/finance_agent/artifacts/**`
- `services/api/tests/finance/**`
- `services/api/tests/jobs/**`
- `services/api/tests/artifacts/**`
- `fixtures/demo/**` except UI snapshots

**Must deliver:** migrations; exact-money domain types; CSV importer; source/evidence model; rule engine; append/invert event service; idempotent staged Daily Close; simple deterministic cash projection; three findings; owner-pack HTML/PDF; snapshot/query service; fixture and hash tests.

**Must not touch:** root lockfiles/scripts, `/contracts`, agent/model/connectors, API composition, Electron/UI.

### Task 2 — agent harness, model modes, API and Telegram adapter

**Owns:**

- `services/api/src/finance_agent/agent/**`
- `services/api/src/finance_agent/models/**`
- `services/api/src/finance_agent/connectors/**`
- `services/api/src/finance_agent/api/routes/**`
- `services/api/tests/agent/**`
- `services/api/tests/models/**`
- `services/api/tests/connectors/**`
- `evals/**`

**Must deliver:** bounded `FinancePlan` compiler/validator/executor; dialogue frame and typed claims; LM Studio adapter; OpenAI adapter; Local/Hybrid/Cloud projection and egress receipts; one-question inquiry policy; SSE event translation; Telegram fixture plus optional long-poll/send adapter; harness evaluations including malformed output and prompt-injection cases.

**Must not touch:** finance calculations/migrations, `/contracts`, root lockfiles/scripts, Electron/UI, real credentials or external account setup.

### Task 3 — standalone Electron workspace and financial canvas

**Owns:**

- `apps/desktop/**`
- `fixtures/ui/**`
- `apps/desktop/tests/**`

**Must deliver:** hardened Electron main/preload boundary; backend lifecycle/readiness UI; exact Refero-led split workspace; continuous thread; closed canvas renderer; Sources, Activity & Undo, Connections & Privacy drawers; model-mode control; loading/offline/partial/error states; keyboard/focus/accessibility; mock-fixture transport; live API transport; desktop screenshots and interaction tests.

**Must not touch:** backend, `/contracts`, shared demo data, root lockfiles/scripts, Hermes assets/branding or copied Refero assets.

### Coordinator-only files

- `/contracts/**`
- root `package.json`, `pnpm-lock.yaml`, `pnpm-workspace.yaml`
- root `README.md`, licence/attribution and Build Week boundary records
- `scripts/**`
- `services/api/pyproject.toml`, `services/api/uv.lock`
- `services/api/src/finance_agent/api/app.py`
- cross-lane integration tests and final demo evidence

Dependency additions are proposed in a task note and applied by the coordinator to avoid lockfile conflicts.

## 7. Required commands

These commands are part of the contract. The concurrent bootstrap added root command names, mostly as explicit `not implemented` stubs; each lane and the coordinator must replace the relevant stub with a real command and proof.

```bash
pnpm install
uv sync --project services/api --all-groups

pnpm contracts:check
pnpm lint
pnpm typecheck
pnpm test

pnpm dev
pnpm demo:reset
pnpm demo:daily-close
pnpm test:golden

pnpm test:local-model     # skips with an explicit reason when LM Studio/model is unavailable
pnpm test:cloud-model     # skips without OPENAI_API_KEY; never prints the key
pnpm test:telegram-live   # opt-in only; fixture test remains mandatory
```

`pnpm dev` must start the Python service and Electron app, wait for readiness, print loopback ports, and shut down both children on exit. Default ports may be `4317` for the API and Vite-selected renderer port; the API must bind to loopback only.

## 8. Golden demo

### Synthetic story

The workspace is “Koru Studio”, a New Zealand sole trader with mixed personal/business transactions, a NZD 2,000 protected cash reserve, recurring rent/software obligations, a duplicate pending card row, a Mitre 10 transaction lacking a receipt, and a forecasted low point after a planned laptop purchase.

### Three-minute run

| Time | Demonstration | Required system evidence |
|---|---|---|
| 0:00–0:20 | Open directly to the continuing thread; it says the morning close already ran | completed `job_run`, data-through time, three concise findings; no manual prompt needed |
| 0:20–0:45 | Select the reserve-risk finding | canvas changes from living brief to cash scenario; exact points and assumptions cite deterministic evidence |
| 0:45–1:15 | Owner explains at length that Mitre 10 was a client fit-out and this merchant rule should apply only below NZD 500 | one adaptive acknowledgement/question at most; explicit claim preserved with source turn |
| 1:15–1:40 | Owner finishes; agent applies the narrow rule automatically | committed event, affected IDs chosen by rule engine, forecast recomputed, change receipt appears |
| 1:40–2:00 | Open Activity, inspect before/after, then Undo and redo once | append-only inverse event; canvas and totals return exactly, then reapply exactly |
| 2:00–2:25 | Show Telegram expense fixture and reserve warning | inbound source receipt and outbound outbox item share correlation IDs; live send optional |
| 2:25–2:45 | Open owner pack | HTML preview and PDF generated from same DTO; totals reconcile to canvas |
| 2:45–3:00 | Switch Local → Cloud/Hybrid without changing the thread | provider-independent dialogue/canvas state persists; egress receipt shows only allowed projection fields |

### Pass/fail assertions

- A second Daily Close with no new source is a no-op, not a duplicate set of messages/artifacts.
- Every displayed amount is obtained from finance-core output and represented in integer minor units at the boundary.
- The model cannot manufacture or select affected transaction IDs for a write.
- Undo restores the exact prior materialised snapshot and writes a new audit event.
- The owner pack totals equal the snapshot totals.
- A malformed `FinancePlan`, unknown surface component, missing sequence event or prompt injection fails closed.
- Offline Local mode still supports reset, close, correction, Undo, canvas and pack.
- Cloud/Telegram absence degrades visibly and does not break the core demo.

## 9. Integration order

1. **Bootstrap lock:** coordinator lands contracts, fixture IDs, schemas, ports and scripts.
2. **Parallel build:** Task 1 uses contracts to produce deterministic snapshots; Task 2 uses fixture snapshots to build harness/API; Task 3 uses canonical JSON fixtures to build the UI.
3. **Core merge:** integrate Task 1 and run exact-money/idempotency/artefact tests.
4. **Harness merge:** integrate Task 2, wire it only to public finance-core services, run Local stub and malformed-plan tests.
5. **UI merge:** integrate Task 3, replace fixture transport with live transport while retaining fixtures for visual tests.
6. **Coordinator wiring:** compose FastAPI app, dependency locks and root scripts; resolve schema drift in producer/consumer code, never by silently altering `/contracts`.
7. **Second-pass audit:** stale branding/imports, source provenance, Undo, mode switches, process shutdown, offline degradation, focus and error states.
8. **Golden proof:** five clean resets/runs, then one recorded local-model run and one recorded GPT-5.6 run if access exists.

## 10. Kill gates and fallbacks

| Gate | Deadline within build | Fallback |
|---|---|---|
| Daily Close is not idempotent after first integration | Immediate | stop all feature work; repair the event/job spine |
| Local model cannot produce valid P0 plans reliably | After fixed eval set | use local model only for bounded classification/explanation; deterministic command router handles demo intents |
| Owner-pack PDF packaging is unstable | Before UI polish | keep deterministic HTML preview and generate PDF with ReportLab only |
| Real Telegram setup is unreliable | Before rehearsal | demonstrate the fully real connector code against recorded Bot API Updates and outbox payloads; do not fake a sent receipt |
| Long conversation loses/supersedes facts | Before recording | cap the demo conversation and fix typed Dialogue Frame retrieval; do not rely on raw transcript stuffing |
| Canvas protocol becomes a framework project | Immediately | reduce to the six frozen surfaces and closed block catalogue |

## 11. Definition of done

The vertical slice is done only when the repository is independently installable, the core demo works offline with synthetic data, the model is replaceable without changing finance truth, the UI shows one continuous conversation and one dynamic canvas, a correction creates a source-linked reversible event, and the golden run evidence is recorded. A polished static surface, passing worker tests, or code existing on disk is not sufficient runtime proof.
