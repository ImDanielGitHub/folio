# Source reuse map: Hermes Finance, Bionic and LM Studio

**Inspection date:** 17 July 2026  
**Target repository:** `/Users/dananeke/Documents/Finace App`  
**Hermes source inspected:** `/Users/dananeke/hermes-finance-app`, clean branch `continuity/hermes/finance-upstream-rebuild-20260713`, commit `e4ea0999da6bd8c48d7aaccd8a461f3bc3a39732`  
**Bionic inspected:** `/Applications/Bionic.app`, version `1.0.0+1`, bundle `ai.elementlabs.bionic`  
**LM Studio inspected:** `/Applications/LM Studio.app`, version `0.4.19+2`, bundle `ai.elementlabs.lmstudio`

Neither Hermes nor either application bundle was modified.

## 1. Legal and provenance rule

- Hermes Agent declares MIT in `/Users/dananeke/hermes-finance-app/LICENSE`, `pyproject.toml` and `package.json`. MIT code may be adapted only with the copyright and licence notice preserved for copied or substantial portions.
- Original Folio code is licensed under Apache-2.0 through the repository's effective `LICENSE` grant and matching package metadata. Apache-2.0 original code can coexist with properly noticed third-party portions; preserve applicable notices and review provider-specific redistribution terms independently.
- The new product must document its baseline, copied/adapted files, original finance work and upstream Hermes commit. It must not imply that Daniel authored the upstream runtime.
- Bionic’s bundled `package.json` declares `"license": "other"`. The readable production bundles are minified and no source maps were found. Treat Bionic as **pattern-only research**. Do not copy, de-minify into a new source tree, redistribute assets, extract private data or infer permission from local readability.
- Bundled third-party components inside Bionic may have their own licences, but their presence does not make Bionic’s integration code reusable. Prefer the upstream public project directly when a later implementation needs a package.
- LM Studio’s public developer APIs may be integrated according to their published SDK/API terms. Do not copy code from the installed proprietary desktop bundle.

## 2. Hermes Finance: adapt, translate or reject

### A. Strong candidates to adapt under MIT

| Exact source | What is valuable | Decision for the standalone product |
|---|---|---|
| `plugins/finance/domain.py` | Exact-money types, source/evidence identifiers and finance-domain boundaries | Adapt the concepts and selected small types. Preserve integer minor units, ISO currency and source IDs. Do not carry Hermes-specific naming. |
| `plugins/finance/categories.py` | Compact category catalogue and normalisation rules | Adapt only if the seeded fixture needs the same categories; otherwise define a smaller P0 catalogue. |
| `plugins/finance/model_queries.py` | Compact deterministic DTOs for summaries, projections, transactions, evidence and reviews | Strong pattern. Recreate finance-core query DTOs so the model receives projections rather than database rows. Copy code only when it is genuinely faster and attribution is recorded. |
| `plugins/finance/tools.py` | Three narrow tool families, closed schemas, enums/limits and `additionalProperties: false` | Reuse the schema discipline, not the three-tool public API. The new controller gets state-specific actions and a maximum five-action `FinancePlan`. |
| `plugins/finance/service.py` | One service boundary joining query, ingest, evidence, recommendations and provider sync | Translate into smaller `finance`, `jobs` and `artifacts` services. Avoid a new god service. |
| `plugins/finance/store.py` | SQLite migrations; atomic imports; provider cursor/data transaction; source digests; user-accepted annotations protected from model suggestions; recommendation provenance and stale/superseded state | Most important backend reference. Adapt the invariants and targeted functions. Do not transplant the whole schema: the new product needs append-only reversible events, job runs, dialogue frames and artefacts that Hermes does not provide. |
| `plugins/finance/providers/akahu.py` | Read-only provider normalisation and stable source identifiers | P1 reference only. No live bank connector in the vertical slice. |
| `plugins/finance/providers/plaid.py` | Read-only provider normalisation and cursor semantics | P1 reference only. Do not make Plaid Link, tokens or webhooks a demo dependency. |
| `plugins/finance/secret_store.py` | OS-backed secret boundary | Adapt the interface later for optional connector credentials. P0 should read an opt-in Telegram/OpenAI setting without logging or storing raw secrets in SQLite. |
| `tests/plugins/finance/**` | Exact arithmetic, import, provider and evidence regression patterns | Translate relevant fixtures into the new repository. Tests are evidence patterns, not proof that the new product works. |

### B. Architecture patterns worth translating, not copying wholesale

| Exact source | Pattern to retain | Why not copy wholesale |
|---|---|---|
| `providers/base.py` | Declarative provider profiles: endpoint/auth/model discovery, model quirks and capability metadata | The new product needs only Local LM Studio and OpenAI in P0. A universal provider registry is unnecessary scope. |
| `providers/__init__.py` | Lazy provider discovery and override precedence | Keep a two-adapter registry now; generalise only when a third real provider arrives. |
| `plugins/model-providers/custom/__init__.py` | OpenAI-compatible local endpoint, context configuration and thinking/reasoning normalisation | Translate into `LMStudioAdapter`; do not import Hermes’s generic provider plugin surface. |
| `agent/model_metadata.py` | Detect local/private endpoints, inspect LM Studio models/capabilities and reconcile context | Strong capability-card reference. Implement the minimum LM Studio `/api/v1/models` probe and explicit task tiers. |
| `agent/plugin_llm.py` | Host-owned model facade, structured completion, schema validation and trusted override boundary | Translate to a product-owned adapter interface. The finance app must own routing/egress policy, not plugins. |
| `agent/memory_provider.py` | Lifecycle hooks for prefetch, turn commit, session boundary and pre-compression | Use as a checklist for typed finance memory. Do not introduce an interchangeable semantic memory provider in P0. |
| `agent/memory_manager.py` | Bounded background work, provenance and staged-versus-committed writes | Translate into the jobs/event store. Finance rules and claims remain canonical SQLite records, not vector memories. |
| `cron/jobs.py` | Atomic job state; schedule parsing; claims/lease TTL; catch-up; separate last-run/next-run; output pruning | Strong scheduler design reference. Implement only a SQLite job/outbox loop with leases and idempotency keys. |
| `cron/scheduler.py` | Non-blocking ticker, per-job background execution, tool restrictions, result/delivery separation | Keep these invariants. Do not import the generic agent scheduler or its tool surface. |
| `cron/scheduler_provider.py` | Scheduler abstraction | A small interface is enough; avoid making scheduler providers a plugin system. |
| `apps/desktop/electron/main.ts` | Electron owns window/process lifecycle and security policy | Inspect patterns; independently author a much smaller standalone main process. |
| `apps/desktop/electron/preload.ts` | Narrow typed renderer bridge | Adapt the allow-list approach; expose only file selection, app metadata and local-service lifecycle. |
| `apps/desktop/electron/backend-child.ts` | Child-process ownership and shutdown | Strong reference for the Python sidecar lifecycle. |
| `apps/desktop/electron/backend-command.ts` | Development/packaged backend command resolution | Translate for `uv run` in development and a packaged sidecar later. |
| `apps/desktop/electron/backend-ready.ts` and `backend-probes.ts` | Readiness probing and degraded startup | Adapt the state machine and tests; no silent blank renderer while the backend boots. |
| `apps/desktop/electron/hardening.ts` | Context isolation/navigation/permission hardening | Reuse the security checklist and small helpers when licence notices are preserved. |
| `apps/desktop/src/lib/gateway-rpc.ts` and `gateway-events.ts` | Typed request/event boundary and ordered renderer updates | Translate to HTTP+SSE rather than carrying Hermes’s gateway protocol. |
| `apps/desktop/src/store/activity.ts` | Renderer activity state | Use as a behavioural reference for a finance-native Activity & Undo drawer. |
| `apps/desktop/src/store/tool-diffs.ts` | Before/after tool change representation | Translate into event receipts; the user sees financial effects, not raw tool internals. |
| `apps/desktop/src/store/gateway-switch.ts` | Connection changes that preserve workspace state | Translate to Local/Hybrid/Cloud mode switching without forking conversation state. |
| `apps/desktop/src/app/session/hooks/use-message-stream/gateway-event.ts` | Stream event reduction | Translate to the smaller ordered SSE envelope in `BUILD_CONTRACT.md`. |

### C. Messaging patterns

| Exact source | Use | Decision |
|---|---|---|
| `plugins/platforms/telegram/plugin.yaml` | Telegram capability declaration | Use only as a feature checklist. |
| `plugins/platforms/telegram/adapter.py` | Long polling/webhook behaviour, update/media handling, allowlists, idempotency, delivery fallback and redaction | Do **not** adapt the 8k-line adapter for P0. Implement a small connector against the official Bot API: allowlisted chat, deduplicated `update_id`, caption/photo source receipt, outbox, `sendMessage`, timeouts and secret redaction. |
| `gateway/platforms/base.py` | Platform adapter boundary | Translate a tiny `InboundSourceConnector`/`OutboundNotifier` interface. |
| `gateway/platforms/whatsapp_cloud.py` | HMAC verification, message-ID dedup, bounded media and official-cloud adapter | P1 reference only. Telegram is the one P0 mobile doorway. |
| `gateway/platforms/webhook_filters.py` | Input filtering and bounded payloads | Reuse security ideas for future webhook mode. Local long polling or recorded fixtures are simpler for the demo. |

### D. Reject for the standalone product

| Exact source/surface | Rejection |
|---|---|
| `agent/conversation_loop.py` | Large generic loop and prompt/tool machinery. The new product needs a bounded FinancePlan state machine that smaller local models can complete. |
| `plugins/finance/__init__.py` and `plugins/finance/plugin.yaml` as the product boundary | Service-gated finance plugin is correct inside Hermes but wrong for a standalone finance-owned app. |
| `apps/desktop/src/app/finance/index.tsx` and the broader finance dashboard | Do not carry the dashboard/card-grid information architecture. Mine only isolated finance display behaviours and rebuild the workspace from the locked Refero composition. |
| Hermes shell/navigation, session switcher, command palette, generic tools, pet, themes and coding surfaces | They make the product look like a Hermes reskin and distract from the finance outcome. |
| Existing approval/clarify/review queue UI | Routine reversible internal work should write receipts and offer Undo. Reserve blocking confirmation for external/irreversible actions, which are outside P0. |
| Generic semantic/vector memory as finance truth | Ledger meaning, owner claims, rules and corrections require typed, temporal, source-linked records. Embeddings may become a recall aid later. |
| Full Hermes cron runtime | Too broad for the first slice; its timeouts/tool restrictions and output model are designed for general agents. |
| Akahu/Plaid runtime setup | Credential, webhook, freshness and provider failure risk with no need in the golden demo. |
| WhatsApp/other platform adapters | Telegram provides enough cross-device proof. |
| Root Hermes README, product name, launcher identity, icons and screenshots | Must not appear in the standalone product except in attribution/history. |

## 3. Bionic and LM Studio: pattern-only map

### A. Bundle identity and process architecture

| Read-only path | Observed signal | Product lesson |
|---|---|---|
| `/Applications/Bionic.app/Contents/Info.plist` | Electron application; Bionic URL scheme; local-network/LM Link permission copy; microphone permission; localhost allowance | Make local/hybrid execution a first-class visible mode and use a dedicated deep-link scheme only after the core flow is stable. |
| `/Applications/Bionic.app/Contents/Resources/app/package.json` | `productName: Bionic`, version `1.0.0+1`, Electron entry `.webpack-bionic/main/index.js`, licence `other` | Architecture evidence only; no code reuse. |
| `/Applications/Bionic.app/Contents/Resources/app/.webpack-bionic/main/index.js` | Bundled main-process application | Confirms main/renderer separation; use the public Electron model, not Bionic code. |
| `/Applications/Bionic.app/Contents/Resources/app/.webpack-bionic/renderer/main_window.js` | Large compiled primary workspace renderer | Product is workspace-led, not a collection of static dashboards. No copying or de-minification. |
| `/Applications/Bionic.app/Contents/Resources/app/.webpack-bionic/renderer/project_launcher.js` | Separate project-start surface | The finance app can have a short first-launch source choice, then one permanent workspace. Do not add a project launcher to P0. |
| `/Applications/LM Studio.app/Contents/Resources/app/package.json` | LM Studio desktop version `0.4.19+2`, licence `other` | Integrate via published APIs only. |

### B. Model, retrieval and sandbox patterns

| Read-only path | Observed signal | Product lesson |
|---|---|---|
| `.../.webpack-bionic/bundled-plugins/bundled-plugin-rag-v1.tar.gz` | Bundled RAG plugin chooses full-file injection versus retrieval from actual token/context budgets; visible parse/chunk/embed progress; source citations | Adopt context-budget-aware source selection and observable ingestion. Do not use vector retrieval as the ledger or copy plugin code without upstream licence confirmation. |
| `.../.webpack-bionic/bundled-plugins/bundled-plugin-js-code-sandbox.tar.gz` | Deno execution scoped to working directory, network/env/system/run/FFI denied, hard timeout, captured output | Good capability-sandbox pattern. P0 finance calculations stay deterministic and do not execute model-generated code. |
| `.../.webpack-bionic/bin/extensions/frameworks/harmony-mac-arm64-apple-metal-advsimd-0.3.5/backend-manifest.json` | Harmony input/output-processing framework | Keep prompt/model formatting behind adapters. No Bionic binary or framework bundle reuse. |
| `.../.webpack-bionic/bin/extensions/frameworks/lmlink-connector-mac-arm64-apple-metal-advsimd-0.1.0/backend-manifest.json` | Cross-device LM Link connector | Treat remote local inference as a later adapter under the same Local capability contract. |

### C. Workspace and artefact patterns

| Read-only path | Observed signal | Product lesson |
|---|---|---|
| `.../.webpack-bionic/pyodide-python-runtime/runtime-manifest.json` | Pinned Python artefact stack including `pypdf`, `python-docx`, `python-pptx`, `openpyxl`, `XlsxWriter`, `reportlab`, `lxml` and Pillow; URLs/hashes and release-age policy | A finance agent should prepare real documents in a constrained, reproducible runtime. For P0 use native Python and ReportLab; adopt dependency pins/hashes and deterministic artefact DTOs. Do not copy the manifest. |
| `.../.webpack-bionic/renderer/static/pdfjs/**` | In-app PDF preview capability | Owner packs should stay in the work context. Use an upstream PDF renderer with its own licence, not Bionic assets. |
| Bundled Monaco/editor and file preview resources under `.webpack-bionic/renderer/static/**` | Native previews and inspectable work products | Prefer a real document/source preview to verbose explanation. Finance does not need a code editor. |

### D. Bionic interaction ideas to adopt

- Work is organised around a local folder/project boundary rather than unrestricted machine access.
- Local and cloud models are workers behind one continuing workspace.
- Progress is visible while the user retains control.
- Files and generated artefacts preview in context.
- Changes can be checkpointed/reviewed/rolled back.
- Tool execution is capability-scoped and sandboxed.
- Context use responds to the actual model budget.

### E. Bionic ideas to reject or reinterpret

- Do not copy the Code/Work project taxonomy; the finance product has one business workspace.
- Do not expose a generic computer/code sandbox as a finance feature.
- Do not make files the only canonical memory. SQLite finance events and immutable sources remain authoritative.
- Do not equate cloud zero-retention claims with local privacy; the app must issue its own field-level egress receipts.
- Do not copy Bionic UI, assets, model catalogue, proprietary cloud integration or voice-keyboard implementation.
- Do not require LM Link, Secure Cloud, an LM Studio account or a particular model to complete the offline demo.

## 4. Existing local research artefacts to carry forward

These are planning sources, not product code:

- `/Users/dananeke/Documents/Codex/2026-07-16/new-chat/outputs/standalone-finance-agent-product-experience-lock.md` — preserve the one-thread/one-canvas product lock, autonomy contract, natural-question behaviour and Telegram doorway.
- `/Users/dananeke/Documents/Codex/2026-07-16/new-chat/outputs/hermes-finance-continuous-close-product-research-and-prd.md` — preserve continuous-close, FinancePlan, typed dialogue state, local/cloud/hybrid and evaluation ideas; use the new `BUILD_CONTRACT.md` where scope differs.
- `/Users/dananeke/Documents/Codex/2026-07-16/new-chat/outputs/bionic-reverse-engineering-and-hermes-finance-design-review.md` — preserve the evidence labels and Bionic comparison; this live inspection upgrades the installed-version/path facts.
- `/Users/dananeke/Documents/Codex/2026-07-16/new-chat/outputs/openai-build-week-decision-brief.md` — preserve the eligibility/baseline boundary; its approval-heavy workflow is superseded by reversible internal work plus Undo.

## 5. Attribution checklist for builders

1. Record the Hermes source commit before copying any code.
2. Add the upstream MIT copyright/licence notice to the repository before the first adapted code lands.
3. Keep a machine-readable list of copied/adapted files and substantial functions.
4. Independently author product identity, UI, contracts and vertical-slice orchestration.
5. Do not include Bionic bundle files, strings, icons, screenshots, model/cloud code or manifests in the repository.
6. Obtain dependencies from their public upstreams and review each licence.
7. Document Build Week baseline and post-start work separately from upstream reuse.
8. Resolve the target repository's provisional licence before any public release or open-source claim.
