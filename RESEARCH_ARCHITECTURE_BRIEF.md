# Research and architecture brief: proactive local-first finance agent

**Prepared:** 17 July 2026, Pacific/Auckland  
**Target:** `/Users/dananeke/Documents/Finace App`  
**Purpose:** implementation-facing decision brief, not product code  
**Research boundary:** live local-source inspection plus time-boxed current web checks. Hermes and the Bionic/LM Studio application bundles were read-only. No financial account, secret, external message, deployment or publication was accessed or changed.

## 1. Executive decision

Build a standalone Electron + React desktop application backed by a local Python/FastAPI service and one SQLite database per business workspace.

The model is not the finance engine. The model converts natural conversation into a small validated `FinancePlan`, asks at most one adaptive question at a time, selects a known financial surface and explains deterministic results. Exact money, affected transaction IDs, rule scope, forecast points, evidence, document totals, event commits and Undo belong to ordinary code.

The first slice is:

> **Autonomous Daily Close → material morning update → open-ended owner context → automatic narrow reversible learning → reserve-risk cash scenario → source-linked owner pack → Telegram expense/alert receipt → Activity and Undo.**

This is the strongest working-demo wedge because it proves proactivity, continuity, trustworthy automation, generative UI, local/cloud portability and a real artefact in one causal loop. A dashboard, general “chat with your books”, broad accounting suite or multi-agent theatre would dilute that proof.

### Decisive architecture choices

| Concern | Decision |
|---|---|
| Desktop | Electron + Vite + React + TypeScript; a new shell, not the Hermes renderer |
| Local backend | Python 3.12 + FastAPI + Pydantic v2 + SQLite, managed with `uv` |
| Interaction transport | HTTP commands + ordered SSE events + resynchronisable snapshots |
| Finance truth | Immutable sources + integer minor units + append-only semantic events + rebuildable projections |
| Agent runtime | Product-owned bounded state machine with direct Local/OpenAI model adapters |
| Local inference | LM Studio OpenAI-compatible API; JSON Schema output; capability/task gates |
| Cloud inference | OpenAI Responses API; same `FinancePlan` and projection compiler |
| Memory | Raw turns plus typed, temporal, source-linked claims/dialogue frames; vector recall is optional and non-canonical |
| Background work | Small SQLite job runner with leases, stage checkpoints, idempotency keys and an outbox |
| Generative UI | Closed `FinanceSurfaceSpec` catalogue rendered by native React components |
| Mobile doorway | Telegram fixture path mandatory; live test bot optional and isolated |
| Documents | Deterministic owner-pack DTO → HTML preview + ReportLab PDF |
| P0 accounting scope | Bookkeeping preparation and evidence, not a general double-entry ledger or tax system |
| Open-source posture | Apache-2.0 is the provisional recommendation for original code; preserve MIT notices for adapted Hermes portions; do not publish until ownership/licence confirmation replaces the current `UNLICENSED` state |

The implementation contract, ownership and golden demo are frozen in [`BUILD_CONTRACT.md`](./BUILD_CONTRACT.md). The exact Refero decision is in [`REFERENCE_UI_DECISION.md`](./REFERENCE_UI_DECISION.md). Exact source adaptation boundaries are in [`SOURCE_REUSE_MAP.md`](./SOURCE_REUSE_MAP.md).

## 2. Evidence and proof boundary

Labels used below:

- **Local verified:** inspected directly on this Mac in the stated path/version.
- **Primary verified:** checked against the product/project’s current official documentation or repository.
- **Secondary corroboration:** public reporting, not the authority for implementation details.
- **Inference:** a conclusion drawn from verified facts; it is not a vendor claim.
- **Recommendation:** the architecture selected for this project.

### Current repository state

- **Local verified:** `/Users/dananeke/Documents/Finace App` was an empty Git repository on `main` with no commits when research began. A separate coordinator bootstrap appeared concurrently during document authoring: root manifests/policy docs, `apps/desktop` and `services/api` placeholders, and explicit not-implemented command stubs. Those changes were preserved and not authored or modified by this research task. No implemented product runtime was present at the final audit.
- **Local verified:** Hermes was clean at `e4ea0999da6bd8c48d7aaccd8a461f3bc3a39732` on `continuity/hermes/finance-upstream-rebuild-20260713`.
- **Local verified:** `/Applications/Bionic.app` is `1.0.0+1`; `/Applications/LM Studio.app` is `0.4.19+2`.
- **Not proved here:** no new product runtime, install, build, local-model run, GPT-5.6 run, Telegram send or generated pack exists yet.

The existing Build Week review records that an existing project may enter with a meaningful extension, but only in-period work is judged; the official rules remain the submission authority. See the [OpenAI Build Week rules](https://openai.devpost.com/rules). Starting the standalone repository empty gives the implementation team a clean, auditable post-start boundary.

## 3. Local-source archaeology

### 3.1 Hermes Finance: what is genuinely strong

The local Hermes branch already demonstrates several correct trust boundaries:

- three finance tools expose narrow schemas rather than the raw database;
- money is calculated in integer minor units with explicit currencies;
- query results contain evidence IDs and compact deterministic projections;
- imported CSV rows retain digest/row/mapping provenance;
- provider rows and cursors commit atomically;
- accepted user meaning is protected from later model suggestions;
- finance recommendations retain source/evidence hashes, revisions and stale/superseded state;
- Electron owns native lifecycle while the renderer consumes typed backend state;
- scheduler jobs separate computation, output and delivery state;
- provider/model metadata recognises local endpoints and LM Studio capabilities;
- Telegram/WhatsApp code demonstrates real connector concerns: deduplication, allowlists, bounded media, redaction, delivery fallback and webhook verification.

The files and precise reuse decisions are enumerated in `SOURCE_REUSE_MAP.md`. The most valuable direct references are:

```text
plugins/finance/domain.py
plugins/finance/model_queries.py
plugins/finance/tools.py
plugins/finance/service.py
plugins/finance/store.py
agent/model_metadata.py
agent/plugin_llm.py
cron/jobs.py
cron/scheduler.py
apps/desktop/electron/{main,preload,backend-child,backend-ready,hardening}.ts
apps/desktop/src/lib/{gateway-rpc,gateway-events}.ts
plugins/platforms/telegram/adapter.py
```

### 3.2 Where Hermes is the wrong starting architecture

Hermes is a general agent whose finance capability is a plugin and whose current desktop finance surface is a large custom dashboard. A standalone finance product needs the inverse ownership model: finance domain, event store, conversation and canvas are the product; model/provider/scheduler/connector code is subordinate infrastructure.

Do not transplant:

- the generic conversation-loop monolith;
- generic chat/session/plugin navigation;
- finance dashboard/card-grid shell;
- approval/clarify queues for routine internal changes;
- general agent cron with broad tools;
- semantic memory as a source of financial truth;
- the full Telegram adapter;
- bank connectors as a demo dependency;
- Hermes identity, assets, copy or repository story.

### 3.3 Bionic/LM Studio: direct observations

**Local verified:** Bionic’s bundle is Electron-based. Its package declares `license: other`; compiled main/renderer bundles have no source maps. Bundled resources include:

- project launcher and main workspace renderers;
- `rag-v1` and `js-code-sandbox` plugin archives;
- Deno, Node, esbuild, Python/Pyodide and SQLite-vector support;
- a pinned Python artefact manifest with PDF, Office, spreadsheet, imaging and ReportLab packages;
- PDF.js/preview resources and editor resources;
- Harmony input/output processing and an LM Link connector.

The readable RAG plugin selects full-content injection or retrieval using actual context/token budgets, reports parsing/chunking/embedding progress and preserves citations. The JavaScript sandbox constrains execution to the workspace, denies network/environment/system/process/FFI access and enforces a timeout. Those are product patterns, not reusable Bionic code.

**Inference:** Bionic’s strongest lesson is not “add tools”. It treats local/open models as workers inside a persistent, scoped workspace with visible progress, file artefacts, checkpoints/rollback and the ability to move heavier work elsewhere without changing the project. Public launch coverage describes local or open-source cloud models, Code/Work projects, inline diffs, document work, sandboxing, checkpoints and in-app previews; because an official Bionic product page did not surface in the time-boxed search, treat this [9to5Mac launch report](https://9to5mac.com/2026/07/16/lm-studio-expands-beyond-chat-with-bionic-a-new-ai-agent-app-for-open-models/) as secondary corroboration only.

LM Studio’s official developer API is the stable integration surface. Its native v1 API supports model management and stateful chat, while its OpenAI-compatible endpoints support custom tools; current docs explicitly distinguish those capabilities. See [LM Studio REST API](https://lmstudio.ai/docs/developer/rest), [structured output](https://lmstudio.ai/docs/developer/openai-compat/structured-output) and [tool use](https://lmstudio.ai/docs/developer/openai-compat/tools).

The structured-output documentation warns that models below roughly 7B may not reliably support it, and the tool-use documentation distinguishes native tool-use templates from a default prompt/parse fallback whose quality varies. That directly supports per-task model capability gates rather than “works with any model”.

### 3.4 Existing product/research artefacts

The four existing July 16–17 artefacts converge on the same product lock:

- one thread per business;
- one dynamic canvas, not many dashboard destinations;
- source and activity drawers;
- Local, Hybrid and Cloud as modes of one product;
- open-ended questions that respond to the previous answer and stop naturally;
- Daily Close and automatic narrow learning with receipts/Undo;
- cash pressure, owner/accountant working papers and Telegram;
- transparent Build Week baseline and evaluation.

This brief narrows the earlier broader PRD in four ways: no full Hermes conversion, no general provider/runtime ecosystem, no production bank connection and no P0 double-entry engine. The first implementation must prove the causal vertical slice before expanding.

## 4. Product landscape: occupied claims and remaining wedge

### 4.1 What current products already cover

Current official product descriptions make a generic finance assistant indefensible as the differentiator:

| Product | Publicly described capability | Architectural lesson |
|---|---|---|
| ChatGPT Finances | Connected accounts via Plaid, finance dashboard, questions, categories, bills/subscriptions, planning, source references and finance memories; no money movement, trades, settings changes or tax filing | Conversation + dashboard + memory is occupied. The app needs small-business work completion, local mode and reversible source-level changes. [Official help](https://help.openai.com/en/articles/20001222-finances-in-chatgpt) |
| Perplexity Finance | Plaid-backed personalised financial insights and connected context | Connected financial Q&A is occupied. [Plaid announcement](https://plaid.com/blog/plaid-perplexity-ai-financial-insights-integration/) |
| QuickBooks Accounting AI | Context gathering, suggested categorisation/matching, anomaly detection and review | “AI categorises and flags anomalies” is table stakes. [QuickBooks Accounting AI](https://quickbooks.intuit.com/accounting-agent/) |
| Xero JAX | Learns how a business runs, automates routine tasks/workflows and provides insights through an agentic platform | “Learns and automates routine accounting” is occupied at incumbent scale. [Xero JAX announcement](https://www.xero.com/au/media-releases/xeros-ai-financial-superagent-jax-launches-powerful-new-features/) |
| Digits | AI-native ledger, continuous categorisation/reconciliation/review, document extraction, exception inbox and Ask Digits | Continuous automated accounting and learning from corrections are occupied. [Digits AI](https://help.digits.com/business-agentic-general-ledger/digits-ai) |
| Brex | AI accounting automation, continuous close, two-way ERP sync, coding and prepared exports | Continuous close is also an expense/ERP product claim. [Brex Accounting API](https://www.brex.com/journal/press/brex-launches-ai-native-accounting-api) |
| Puzzle | AI-native accounting and continuous books for startups | A modern automated ledger alone is not novel. [Puzzle](https://puzzle.io/) |
| Ramp | Spend/expense automation, policies and accounting integrations | Automated expense handling is occupied. [Ramp accounting automation](https://ramp.com/accounting-automation) |
| Basis | AI agents for accounting workflows | “Accounting agents” itself is not a product thesis. [Basis](https://www.basis.ai/) |

### 4.2 Concrete shortcomings that matter here

The following are **inferences from public product scope**, not claims that those products can never provide the capability:

- Most public positioning is dashboard, inbox, ledger, review-feed or workflow-suite centred. That leaves room for a calm, continuing owner relationship where work and artefacts appear in one evolving canvas.
- Cloud/account-connected products cannot be the proof of first-party offline use. A local mode that completes the core loop without an account is independently valuable.
- “Learns from corrections” often routes through confirmation/review workflows. The proposed product makes narrow internal corrections automatic under a standing mandate, then uses source-linked receipts and one-click inverse events as control.
- Incumbents optimise for accounting breadth or corporate spend. The proposed slice optimises for a sole trader’s mixed context, cash reserve and accountant-ready evidence handoff.
- Public product pages emphasise automation outcomes more than reproducible per-model capability cards. A published Local-versus-Cloud harness can be an open-source trust advantage.

### 4.3 Wedge statement

The defensible wedge is not a feature list. It is a behaviour:

> A local-first finance operator quietly prepares routine work, learns the owner’s meaning through an uninterrupted relationship, keeps exact money and evidence outside the model, and makes every internal change inspectable and reversible without turning the owner into an approval-queue manager.

## 5. Robust agent runtime for smaller local models

### 5.1 Selected pattern: deterministic controller, bounded model decisions

Small models fail disproportionately when they must choose among many tools, remember long implicit state, repair malformed nested arguments and decide when an open-ended loop is complete. The architecture reduces those degrees of freedom:

1. Deterministically assemble a compact context packet: current `DialogueFrame`, relevant claims, current surface, last material receipt and finance projection.
2. Present only the actions available in the current controller state.
3. Request one closed `FinancePlan` with at most five actions.
4. Validate names, enums, scopes, permissions and budget.
5. Permit one schema-repair attempt.
6. Resolve affected records and all numbers in finance code.
7. Execute reversible internal writes transactionally.
8. Recompute projections.
9. Select a known surface and ask the model only for concise explanation/question text.
10. Commit a work receipt with model, tools, evidence, effects and egress.

This is more reliable than handing the local model a large generic agent loop, and it makes evaluation meaningful.

### 5.2 Why not make a framework the P0 spine

The [OpenAI Agents SDK](https://openai.github.io/openai-agents-python/) provides a production-ready agent loop, function tools with schema validation, sessions, guardrails, human-in-the-loop and tracing. Its sessions can use SQLite and maintain history across runs; tracing covers generations, tools, guardrails and handoffs. Those are strong Cloud-path and evaluation references. For this product, however, the official docs also say to use the Responses API directly when the application wants to own loop, dispatch and state. We do. Finance mutation budgets, local-model degradation, egress and deterministic recomputation are core product semantics, so the first loop remains application-owned.

[LangGraph](https://docs.langchain.com/oss/python/langgraph/overview) and [PydanticAI durable execution](https://ai.pydantic.dev/durable_execution/overview/) are credible later choices if workflows become genuinely long-running or distributed. They do not remove the need for finance events/idempotency and would add another state authority now.

### 5.3 Capability contract

Each model gets measured task tiers:

- **Tier 0:** explanation only; no plan/write.
- **Tier 1:** choose one read action and valid surface.
- **Tier 2:** compile multi-action read/scenario plans.
- **Tier 3:** extract explicit owner claims and propose a bounded reversible rule.

A model’s tier is determined by fixed fixtures, not parameter count or provider label. Failure degrades the task, not the whole product. The fallback for an invalid write plan is a read-only result or one natural question—not a hidden cloud route or repeated retry loop.

### 5.4 Long conversation and memory

Use four distinct stores:

| Store | Content | Authority |
|---|---|---|
| Transcript | raw owner/agent/tool turns | Evidence of what was said; never current truth by itself |
| Dialogue Frame | current topic, known facts, open uncertainties, active scenario, user has stopped/paused | Model-independent working state |
| Claims | explicit/inferred fact, source turn, scope, effective dates, confidence, supersedes | Current human context after deterministic temporal resolution |
| Finance events | rules, corrections, undo, affected rows, before/after, evidence | Canonical meaning-changing work |

Context assembly retrieves typed current state first, then exact source turns/evidence, then optional semantic episodes. Compression may summarise prose but cannot rewrite claims, events, money or receipt IDs.

## 6. Generative UI and interaction protocols

### 6.1 Current landscape

| System | Current useful idea | P0 decision |
|---|---|---|
| A2UI | Current production family v0.9.1; streams surface/component/data messages; app-controlled component catalogues; React renderer; no arbitrary code | Adopt its separation and catalogue discipline, not the runtime. Six known finance surfaces do not justify a protocol renderer yet. [A2UI](https://a2ui.org/) and [renderer guide](https://a2ui.org/guides/renderer-development/) |
| AG-UI | Lightweight bidirectional event protocol with lifecycle, text/tool events, state snapshots/deltas and custom events | Shape the internal ordered event envelope after its lifecycle and resync patterns; no dependency in P0. [Overview](https://docs.ag-ui.com/) and [events](https://docs.ag-ui.com/concepts/events) |
| MCP Apps | Official MCP extension for tool-declared `ui://` resources rendered in sandboxed iframes with postMessage/JSON-RPC | Strong P1 interoperability path for publishing a scenario modeller or owner-pack viewer into other hosts. Wrong for the trusted internal desktop canvas. [Official overview](https://modelcontextprotocol.io/extensions/apps/overview) |
| Vercel AI SDK UI | Typed message parts, tool-result rendering, streaming, stop/resume and custom transports | Useful thread-state option. Use only if its custom transport consumes the application stream cleanly. [useChat](https://ai-sdk.dev/docs/reference/ai-sdk-ui/use-chat) and [generative UI](https://ai-sdk.dev/docs/ai-sdk-ui/generative-user-interfaces) |
| CopilotKit | Agentic frontend/AG-UI integrations and shared state | Credible for a web app already committed to AG-UI; redundant for this controlled local P0. [CopilotKit docs](https://docs.copilotkit.ai/) |
| assistant-ui | Headless/React assistant-thread primitives and runtime adapters | Possible alternative to hand-built thread primitives, but do not combine it with another chat state owner. [assistant-ui docs](https://www.assistant-ui.com/docs) |
| OpenAI Apps SDK/MCP | Conversational host integration and interactive apps | Future distribution surface, not the standalone app architecture. [OpenAI developers](https://developers.openai.com/) |

### 6.2 Selected interaction model

- One persistent thread is the relationship; one canvas is the work product.
- Routine background work posts only material updates. It does not narrate every tool call.
- Questions are adaptive, one at a time, and acknowledge the previous answer. The owner can answer at arbitrary length, skip, change topic or stop; synthesis happens without an “incomplete questionnaire” warning.
- Internal, reversible work runs automatically within the standing mandate. The control surface is the source-linked receipt and Undo.
- External, irreversible or regulated actions remain unavailable in P0; later they require explicit action-specific authority.
- Canvas transitions are deterministic consequences of the question/work state, not decorative widgets embedded after every answer.

The exact composition, Refero IDs, screenshot metadata, original tokens and licence boundary are locked in `REFERENCE_UI_DECISION.md`.

## 7. Finance and document engine research

### 7.1 P0 versus later

The fastest trustworthy slice uses small deterministic services, not a full accounting/ML platform:

| Capability | P0 implementation | Later credible upstream |
|---|---|---|
| CSV | Python `csv`, explicit mappings, source digest/row provenance, atomic import | Polars/pandas only when scale demands them |
| OFX/QFX | Adapter interface and fixtures only | [ofxtools](https://github.com/csingley/ofxtools) or [ofxparse](https://github.com/jseutter/ofxparse), after version/licence review |
| PDF/receipt | Store source/attachment; explicit Telegram caption; no OCR gate | [Docling](https://github.com/docling-project/docling) is MIT and supports structured conversion/OCR; Tesseract/PyPDF for narrower cases |
| Ledger | Source-linked transaction model and semantic events; no claim of general ledger | [Beancount](https://beancount.github.io/docs/) export/reference, GnuCash/accounting-system adapters; review copyleft boundaries before embedding |
| Reconciliation | Exact duplicate/transfer rules, statement/source checks, unresolved states | Beancount/Actual patterns and accountant-reviewed adapters |
| Categorisation | Explicit user rules → deterministic features → bounded model proposal; every result has provenance | scikit-learn or River for measured per-user incremental models |
| Forecasting | Transparent 30-day cash roll-forward using known/recurring commitments and scenarios | [StatsForecast](https://nixtlaverse.nixtla.io/statsforecast/index.html) with backtesting and intervals when history supports it |
| Anomaly detection | Exact duplicate, unusual amount/merchant relative to local history, missing document, reserve breach | [River anomaly APIs](https://riverml.xyz/latest/api/anomaly/) or PyOD after labelled evaluation |
| Reporting | One deterministic DTO rendered to HTML and ReportLab PDF | `python-docx`, openpyxl/XlsxWriter and templates for accountant exports |

Docling’s official repository describes multi-format parsing, OCR and an MIT codebase, but also notes that individual models carry their own licences. Dependency/model licences must therefore be checked at the exact locked version, not assumed from the wrapper. See [Docling repository](https://github.com/docling-project/docling).

### 7.2 Open-source product lessons

- [Actual Budget](https://actualbudget.org/docs/vision/) is a strong local-first reference: the database lives on-device, the product is MIT-licensed and the UI includes robust Undo. Its budgeting engine is not a small-business accounting engine, so borrow local ownership/sync/event ideas rather than product scope.
- [Beancount](https://beancount.github.io/docs/) is a transparent double-entry and reporting reference. It is best treated as optional export/interoperability until its packaging/licence and the product’s accounting requirements are deliberately accepted.
- [GnuCash](https://www.gnucash.org/), [Firefly III](https://www.firefly-iii.org/), [Maybe](https://github.com/maybe-finance/maybe) and Actual prove demand for self-hosted/local finance, but are not drop-in deterministic kernels for this user experience.
- [Open Accountant/Wilson](https://github.com/openaccountant/wilson) already occupies the open-source local AI bookkeeper pitch. The differentiation must be the continuous relationship, reversible event receipts, evidence-linked generative canvas, model capability contract and NZ sole-trader working flow—not merely Ollama/local support.

### 7.3 Why not double-entry in P0

A real general ledger requires balanced postings, chart-of-accounts policy, periods, accrual/reversal semantics, audit controls, opening balances and reconciliation. Adding an incomplete “double-entry” table to win architecture points would create false trust. The P0 owner pack is explicitly bookkeeping preparation/working papers. When a real ledger is required, integrate or implement it as a separately tested bounded context and preserve source-to-posting lineage.

## 8. Data and execution architecture

### 8.1 Causal pipeline

```text
CSV / Telegram fixture / future connector
            │
            ▼
immutable source item + attachment + digest
            │
            ▼
normalised transaction candidates ──► duplicate/transfer checks
            │
            ▼
current owner rules + typed claims
            │
            ▼
append-only finance events ──────────► inverse event for Undo
            │
            ▼
materialised transactions/findings/forecast
       ┌────┴─────────┬───────────────┐
       ▼              ▼               ▼
continuing thread  financial canvas  owner-pack DTO/PDF
       │                              │
       └──────────► activity receipt ◄┘
                                      │
                                      ▼
                                Telegram outbox
```

### 8.2 Daily Close

Use one job definition with resumable, idempotent stages:

1. claim run by `(workspace, source high-water mark, policy version)`;
2. ingest pending sources;
3. normalise/deduplicate;
4. apply explicit rules;
5. request bounded model classification only where needed;
6. compute findings/materiality;
7. compute scenario/forecast;
8. generate owner-pack DTO and render artefacts;
9. commit one receipt and current surface suggestion;
10. enqueue an optional notification outbox item.

Each stage stores input hash, output hash, status, attempts and error. The runner uses a lease/heartbeat so a crash can resume safely. The notification is delivered only from the outbox after the finance transaction commits.

### 8.3 Proactivity policy

Post a thread/Telegram update only for:

- a material cash risk or data-integrity condition;
- completed prepared work the owner is likely to use;
- a bounded question whose answer unlocks material work.

Never interrupt for low-confidence trivia, routine successful categorisations, unchanged forecasts or internal retry noise. The activity drawer can retain the full receipt without adding a chat message.

### 8.4 Telegram

The official Bot API supports long polling with `getUpdates` or HTTPS webhooks, and `update_id`/offset semantics allow confirmed deduplication. Webhooks may carry a secret-token header. See [Telegram Bot API](https://core.telegram.org/bots/api) and [Bots FAQ](https://core.telegram.org/bots/faq).

P0 uses:

- recorded `Update` fixtures in all tests;
- optional long polling for a local test bot, never simultaneous with a webhook;
- allowlisted chat/user;
- `update_id` and message ID deduplication;
- downloaded/stored attachment as a source item with hash;
- explicit text/caption parsing for the demo, not claimed OCR;
- one minimal outgoing alert with no full account history;
- outbox/delivery receipt that distinguishes queued, attempted, delivered and failed.

## 9. Privacy, safety and failure design

- Bind the service to loopback. Reject non-local Host/Origin by default.
- Keep raw sources, SQLite and artefacts within the workspace data directory.
- Use OS credential storage or process-injected secrets; never place tokens in prompts, events, logs, fixtures or repository files.
- Treat CSV descriptions, PDFs, captions and connector data as untrusted content, never instructions.
- Do not expose shell, SQL, Python or arbitrary HTTP tools to the finance model.
- Every cloud request passes through a projection compiler and writes an egress receipt with provider/model/purpose/field classes/counts/time.
- Local mode must complete reset, ingest, close, correction, Undo, forecast and pack without network.
- Forecasts show assumptions, range/uncertainty and data-through time; they never promise outcomes.
- The owner pack is labelled working papers, not tax/accounting advice or filed records.
- Telegram content defaults to minimal risk/next action with a local deep-link/reference, not balances/transaction history.
- Model unavailable: deterministic close succeeds and UI explains which language/classification work is pending.
- Corrupt/out-of-order UI stream: request a fresh snapshot instead of guessing.
- Connector unavailable: retain the outbox item and surface failure without rolling back finance work.

## 10. Evaluation strategy

### Deterministic fixtures

- exact totals and currency;
- duplicate/pending/internal-transfer exclusion;
- source digest/row/evidence set;
- rule scope below NZD 500;
- affected transaction IDs selected by code;
- reserve-risk date/amount;
- owner-pack reconciliation;
- idempotent second close;
- inverse-event snapshot equality.

### Harness fixtures

- natural read question;
- long contextual answer;
- correction plus future-rule intent;
- scope ambiguity requiring one question;
- stop/topic change synthesis;
- malformed JSON and unknown action;
- prompt injection inside merchant/source text;
- stale/superseded claim;
- Local → Cloud continuation;
- context-budget pressure.

Score schema validity, semantic plan correctness, tool/action count, retries, exact end state, evidence completeness, latency and whether any unauthorised mutation/egress occurred. Publish results by model/task tier; do not claim universal model support.

### UI/experience proof

- Refero-locked 1440×900 and 1280×800 screenshots;
- keyboard-only thread, canvas, drawers and Undo;
- loading, partial, model-unavailable, offline and connector-failed states;
- Stop during a streamed answer;
- canvas/state continuity across model switch;
- five consecutive golden resets/runs.

## 11. First working slice and broader roadmap

### P0 now

Everything in the frozen vertical slice and golden demo in `BUILD_CONTRACT.md`.

### P1 after the slice is reliable

- watched local inbox and OFX/QFX import;
- Docling/OCR receipt/statement extraction with human-visible source boxes;
- accountant-oriented export and optional Beancount compatibility;
- statistical forecast backtesting/intervals;
- production-grade Telegram webhook or connector service;
- Akahu/New Zealand open-banking research and explicit connection boundaries;
- 100-turn/multi-session memory soak;
- A2UI/MCP Apps adapter if external host interoperability has a real user.

### P2 only after accounting scope is chosen

- real double-entry ledger or accounting-system integration;
- reconciliation periods and opening balances;
- invoices/receivables and accountant collaboration;
- multi-entity/multi-currency;
- optional encrypted device sync.

## 12. Disconfirming evidence and blockers

This recommendation should change if:

- the deterministic Daily Close cannot be made idempotent before UI integration;
- the local model harness does not improve completed finance tasks over a deterministic command router;
- target owners prefer a conventional exception inbox and do not value continuing conversation/canvas;
- users do not notice or trust automatic changes despite immediate receipts/Undo;
- the owner pack cannot reconcile without a real ledger;
- judges cannot understand “it was already working” in the first 20 seconds;
- the exact Build Week model access or submission eligibility differs from the prior brief.

Current non-blocking unknowns for builders:

- exact local model and quantisation that passes Tier 1–3 at acceptable latency;
- whether `@ai-sdk/react` custom transport saves more work than a small direct thread reducer;
- packaged Python sidecar strategy after the demo build;
- live Telegram test-bot availability;
- GPT-5.6 account/model identifier available at demo time.

One release blocker is already explicit in the concurrent bootstrap: the intended Apache-2.0 licence is provisional and the package is still `UNLICENSED`. This does not block local implementation, but ownership/licence confirmation is required before calling the repository open source or publishing it.

None blocks implementing the offline deterministic spine. Cloud and live Telegram must degrade honestly rather than becoming demo prerequisites.

## 13. Source index

### Local/open-model and agent runtime

- [LM Studio developer docs](https://lmstudio.ai/docs/developer)
- [LM Studio REST API](https://lmstudio.ai/docs/developer/rest)
- [LM Studio structured output](https://lmstudio.ai/docs/developer/openai-compat/structured-output)
- [LM Studio tool use](https://lmstudio.ai/docs/developer/openai-compat/tools)
- [OpenAI Agents SDK](https://openai.github.io/openai-agents-python/)
- [OpenAI Agents SDK sessions](https://openai.github.io/openai-agents-python/sessions/)
- [OpenAI Agents SDK tracing](https://openai.github.io/openai-agents-python/tracing/)
- [LangGraph overview](https://docs.langchain.com/oss/python/langgraph/overview)
- [PydanticAI durable execution](https://ai.pydantic.dev/durable_execution/overview/)

### Agent UI and protocols

- [A2UI current specification hub](https://a2ui.org/)
- [A2UI v0.9 protocol](https://a2ui.org/specification/v0.9-a2ui/)
- [A2UI renderer development](https://a2ui.org/guides/renderer-development/)
- [AG-UI overview](https://docs.ag-ui.com/)
- [AG-UI architecture](https://docs.ag-ui.com/concepts/architecture)
- [AG-UI events](https://docs.ag-ui.com/concepts/events)
- [MCP Apps overview](https://modelcontextprotocol.io/extensions/apps/overview)
- [MCP Apps build guide](https://modelcontextprotocol.io/extensions/apps/build)
- [Vercel AI SDK generative UI](https://ai-sdk.dev/docs/ai-sdk-ui/generative-user-interfaces)
- [Vercel AI SDK useChat](https://ai-sdk.dev/docs/reference/ai-sdk-ui/use-chat)
- [CopilotKit documentation](https://docs.copilotkit.ai/)
- [assistant-ui documentation](https://www.assistant-ui.com/docs)

### Finance products

- [OpenAI: Finances in ChatGPT](https://help.openai.com/en/articles/20001222-finances-in-chatgpt)
- [Plaid: Perplexity personalised finance](https://plaid.com/blog/plaid-perplexity-ai-financial-insights-integration/)
- [QuickBooks Accounting AI](https://quickbooks.intuit.com/accounting-agent/)
- [Xero JAX](https://www.xero.com/au/media-releases/xeros-ai-financial-superagent-jax-launches-powerful-new-features/)
- [Digits AI](https://help.digits.com/business-agentic-general-ledger/digits-ai)
- [Puzzle](https://puzzle.io/)
- [Brex Accounting API](https://www.brex.com/journal/press/brex-launches-ai-native-accounting-api)
- [Ramp accounting automation](https://ramp.com/accounting-automation)
- [Basis](https://www.basis.ai/)

### Open finance/document engines

- [Actual Budget local-first vision](https://actualbudget.org/docs/vision/)
- [Actual Budget API](https://actualbudget.org/docs/api/)
- [Beancount documentation](https://beancount.github.io/docs/)
- [Open Accountant/Wilson](https://github.com/openaccountant/wilson)
- [Docling](https://github.com/docling-project/docling)
- [StatsForecast](https://nixtlaverse.nixtla.io/statsforecast/index.html)
- [River](https://riverml.xyz/latest/api/overview/)
- [ofxtools](https://github.com/csingley/ofxtools)
- [ofxparse](https://github.com/jseutter/ofxparse)
- [ReportLab](https://docs.reportlab.com/)
- [Telegram Bot API](https://core.telegram.org/bots/api)

## 14. Final recommendation

Start implementation from the contract, not from Hermes or Bionic. Reuse Hermes’s proven invariants selectively under MIT attribution, use LM Studio only through its public APIs, and treat Bionic/Refero as pattern evidence. The first engineering priority is the immutable-source/event/job spine and exact snapshot. The first product priority is the Refero-locked thread-and-canvas workspace. Everything else earns its place only if it makes the golden causal loop more reliable or more legible.
