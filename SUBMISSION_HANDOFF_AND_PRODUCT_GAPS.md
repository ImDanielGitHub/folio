# Folio submission handoff and product-vision gap register

**Status:** canonical Build Week product on `main`
**Product direction:** dark, chat-first, local-first finance operator for New Zealand small businesses
**Proof rule:** fixture, test, local runtime and live-provider evidence are reported separately

This document is the operating handoff for the finished Build Week repository. It records what is implemented, how to run it, how to configure the optional providers, what has been verified, and the remaining gaps between this prototype and the broader Folio product vision.

## 0. Canonical integration record

- Canonical branch: `main` in `/Users/dananeke/folio-build-week`.
- Completed product commit: `9a33377` (`feat: complete Folio submission product`).
- Reconciled branch: `github/main` at `e9a1b7d`.
- Merge commit: `82de538` (`merge: reconcile superseded submission branch into canonical main`).
- Resolution: the earlier branch's useful Akahu fixture, golden-flow and submission work was already incorporated and extended in `9a33377`. Its superseded light-UI worker notes, duplicate source copies and old screenshots were intentionally not restored. The merge records that history while keeping the tested dark product as the canonical tree.
- Media correction: the incomplete Remotion/ElevenLabs lane was removed after Daniel chose to make the final video himself; it is not part of the product dependency graph.
- Post-merge acceptance on 21 July 2026 NZST:
  - contract validation passed all 12 cases;
  - API/desktop suite passed all 61 Python tests plus TypeScript checks;
  - Electron TypeScript and Vite production build passed;
  - golden flow returned `status: PASS`, 6 sealed Akahu rows, 25 Daily Close events, owner-pack artefact generation, reversible correction and `externalCallsMade: false`.
  - LM Studio capability discovery found the loaded `qwen/qwen3-vl-8b` model over loopback and reported structured output, tool use and a 262,144-token advertised context window. This proves runtime discovery, not behavioural finance accuracy; its measured tier remains unset until the live evaluation is run.

The local `main` branch is the verified source of truth. It is intentionally not described as remote/published proof until its commits are pushed to the repository URL used in the Devpost entry.

## 1. What Folio is now

Folio is a standalone finance product, not a Hermes Finance reskin. It starts as one calm conversation and reveals a financial canvas only when the current answer needs a chart, transaction, source, scenario or document. Under that simple surface it keeps deterministic money calculations, immutable source evidence, reversible finance events and a model-independent working understanding of the business.

The current Koru Studio workflow can:

- ingest the sealed demo, an NZ bank CSV, a sealed Akahu-shaped feed, or a configured read-only Akahu connection;
- perform an idempotent Daily Close;
- detect a likely duplicate and an unresolved business-purpose question;
- accept a natural-language owner explanation and retain its source;
- create and undo a classification rule without erasing history;
- calculate a deterministic 30-day cash scenario;
- prepare HTML and PDF owner-pack artefacts with linked evidence;
- preserve conversation and business understanding independently of the selected model;
- run with an LM Studio model, an explicitly configured OpenAI route, or a deterministic local fallback;
- expose model, source, run and provenance detail only in the deliberate audit/settings surfaces.

## 2. Fastest local start

Prerequisites:

- Node.js 22 or newer;
- pnpm 10.33.0;
- Python 3.12 or newer;
- `uv`;
- optional LM Studio for local narrative generation.

From the repository root, the preferred demo launcher is:

```bash
./run --reset
# optional local model: ./run --reset --with-lms
```

`./run` is idempotent: it reuses healthy API/UI listeners when present, waits for `/health`, prints the recording URL (`http://127.0.0.1:4173/?onboarding=1`), and can reset the Koru seed. Logs live under `var/run-logs/`. Stop Folio processes started by the launcher with `./run --stop`.

Manual equivalent:

```bash
pnpm install:all
cp .env.example .env
pnpm dev
```

The desktop uses the loopback API at `http://127.0.0.1:8787`. For a browser-only preview:

```bash
pnpm api
pnpm dev:browser -- --host 127.0.0.1 --port 4173
```

Then open `http://127.0.0.1:4173`. Use `?demo=1` for the sealed UI state or `?onboarding=1` to exercise onboarding against the running API.

## 3. Submission demo path

The most reliable judge flow is:

1. Open onboarding and choose **Open Koru Studio**.
2. Ask: `What needs my attention today?`
3. Open **Current picture** to reveal the dynamic canvas.
4. Run **Daily Close** and show the resumable progress state.
5. Open the MITRE 10 transaction and its linked evidence.
6. Explain: `That MITRE 10 purchase was materials for the client fit-out.`
7. Show the committed receipt and **Undo change**.
8. Ask: `If the Acme invoice is paid seven days late, what happens to cash?`
9. Open the deterministic scenario beside the conversation.
10. Ask Folio to prepare the owner pack, then open its HTML or PDF artefact.
11. Open **Privacy & models** briefly to show Local, Hybrid and Cloud are explicit choices and that Folio does not silently send data elsewhere.
12. If Akahu credentials are configured, return to onboarding and choose **Sync Akahu read-only**. Otherwise use **Preview an Akahu import** and state clearly that it is the sealed provider fixture.

For repeatable API proof:

```bash
pnpm demo:golden
```

The golden script resets the fixture, executes the bounded workflow and verifies the final snapshot. It is local fixture proof, not proof of a real bank or model call.

## 4. Akahu setup

Folio's live Akahu seam is deliberately read-only. It can retrieve accounts and settled transactions. It cannot create payments or modify a bank account.

Add the following values to the environment of the API process:

```bash
FINANCE_AKAHU_ENABLED=true
AKAHU_APP_TOKEN=your-app-token
AKAHU_USER_TOKEN=your-user-token
```

Restart the API. The desktop probes `GET /v1/connections/capabilities`; when the process is configured, onboarding changes from **Preview an Akahu import** to **Sync Akahu read-only**. Tokens are injected at process start, excluded from configuration representations, and are not stored in SQLite, evidence or receipts.

Direct route check:

```bash
curl -sS -X POST http://127.0.0.1:8787/v1/connectors/akahu/sync \
  -H 'Content-Type: application/json' \
  -d '{}'
```

An optional inclusive date window is supported:

```json
{"start":"2026-04-01","end":"2026-07-21"}
```

The default window is 90 days and the maximum is 366 days. The provider host is pinned to `https://api.akahu.io`; requests use Akahu's bearer user token plus `X-Akahu-Id` app-token headers. The implementation follows cursors with page/item limits, imports only settled NZD transactions, uses exact decimal-to-minor-unit conversion, deduplicates stable provider transaction IDs, and records `liveSyncAttempted` and `externalCallsMade` in its receipt.

**Current proof boundary:** the full path is MockTransport-tested, including headers, pagination, exact cents, deduplication and fail-closed behaviour. No real Daniel-owned Akahu credentials were available during Build Week, so a real provider response remains unverified.

## 4b. Plaid setup (US / Build Week judges)

Folio's live Plaid seam is deliberately read-only and sandbox-first. The **default demo path is the sealed fixture** (`fixtures/demo/plaid-sync.json`). No Plaid network call is made unless credentials are injected.

Add the following values to the environment of the API process for live sandbox:

```bash
FINANCE_PLAID_ENABLED=true
PLAID_CLIENT_ID=your-sandbox-client-id
PLAID_SECRET=your-sandbox-secret
PLAID_ENV=sandbox
```

Optional:

```bash
PLAID_ACCESS_TOKEN=access-sandbox-…   # skip Link; sync with an existing Item
PLAID_PRODUCTS=transactions
PLAID_COUNTRY_CODES=US
PLAID_CLIENT_NAME=Folio
```

Restart the API. The desktop probes `GET /v1/connections/capabilities`; when configured, onboarding changes from **Preview a Plaid import** to **Sync Plaid sandbox read-only**.

### Demo: sealed fixture (default, offline)

1. Start Folio with Plaid left disabled (`FINANCE_PLAID_ENABLED=false` or unset).
2. Onboarding → **Preview a Plaid import** → **Process sealed Plaid feed**.
3. Six Chase-shaped USD provider rows are committed locally. Receipts record `liveSyncAttempted: false`.

```bash
curl -sS -X POST http://127.0.0.1:8787/v1/ingest/plaid-fixture \
  -H 'Content-Type: application/json' \
  -d '{}'
```

### Demo: live sandbox (credentials required)

```bash
# Create a Link token for a real Plaid Link session
curl -sS -X POST http://127.0.0.1:8787/v1/connectors/plaid/link-token

# Sync without Link UI: sandbox creates a public token, exchanges it, then
# /transactions/sync. Or pass {"publicToken":"…"} after Link onSuccess.
curl -sS -X POST http://127.0.0.1:8787/v1/connectors/plaid/sync \
  -H 'Content-Type: application/json' \
  -d '{}'
```

Hosts are pinned to `sandbox.plaid.com` by default. Access tokens are ephemeral for the sync request and are not stored in SQLite, evidence or receipts. Plaid does not support New Zealand banks.

**Honesty note:** Folio's P0 ledger columns remain NZD-constrained; sealed/live Plaid amounts are exact minor units with provider currency retained in raw evidence (`providerCurrency: USD`). This is connector-path proof for Build Week, not a multi-currency production ledger.

## 5. NZ bank CSV setup

The importer accepts the canonical Folio schema and practical NZ layouts:

- signed `Amount` statements;
- separate `Debit` and `Credit` columns;
- common date, description, payee, particulars, code, memo and reference headings;
- ISO and unambiguous NZ day/month/year dates.

All amount conversion uses `Decimal`. Non-NZD rows, ambiguous mixed amount layouts, both-populated debit/credit rows, invalid dates and ambiguous multi-account imports fail closed before commit. Re-importing identical bytes is idempotent.

Use onboarding's **Choose a local CSV** action. The file is processed by the loopback service and is not sent to a model.

## 6. Local, hybrid and cloud models

LM Studio is the primary local route. Configure its OpenAI-compatible loopback endpoint and model in `.env` using the documented `LM_STUDIO_*` values. Folio discovers capabilities, gives the model only a closed, relevant finance tool set, validates structured output, repairs bounded JSON mistakes, applies retry/loop budgets, and falls back to deterministic local planning if the model cannot complete a valid plan.

Cloud access is opt-in through process configuration. Selecting Hybrid or Cloud in the interface does not make an external call unless a supported cloud credential is configured. Finance amounts, source-of-truth state, event effects, forecasts and artefacts remain deterministic local outputs regardless of narrative provider.

The working-understanding layer is model-independent: immutable owner statements and source items feed structured facts with provenance, certainty and temporal state; corrections supersede instead of destroying earlier claims; task-specific retrieval assembles the current picture for each turn. This allows a conversation to continue after restart or model switch without pretending the model itself remembers everything.

## 7. Verification commands

Run the complete local acceptance set from the repository root:

```bash
LM_STUDIO_BASE_URL=http://127.0.0.1:65530/v1 OPENAI_API_KEY= pnpm test
pnpm lint
pnpm typecheck
pnpm build
pnpm demo:golden
```

The deliberately unreachable LM Studio endpoint prevents the main acceptance suite from silently invoking a currently loaded model. Use the separate live evaluation only when you intentionally want to test LM Studio:

```bash
lms status
pnpm eval:lmstudio:live
```

Inspect LM Studio during a live run with:

```bash
lms logs
```

Do not describe the offline harness as a live-model result.

## 8. Goal completion register

| Product goal from Daniel's brief | Current implementation | Proof level |
|---|---|---|
| Standalone finance product rather than Hermes skin | New repository, architecture, name, contracts, desktop and API | Source and build |
| Dark, calm, chat-first interface | Continuing thread with task-driven secondary canvas; Paper-aligned dark tokens | Rendered desktop/mobile screenshots and build |
| Deterministic finance hidden under conversation | Exact-money services and closed finance surfaces; internals moved to audit/settings | Source, tests and rendered runtime |
| Dynamic charts, tables and documents | Current picture, records, transaction, scenario, receipt and owner-pack surfaces | Runtime fixture and golden flow |
| Evidence that “just works” | Compact claim/source affordances, full evidence drawer on demand | Runtime fixture and tests |
| Natural long-form corrections | Owner statements retained in full; structured facts and rules update broader understanding | Integration tests and golden flow |
| Continuous who/what/where/when/why understanding | Immutable sources, claims/entities, temporal supersession, business summary and task retrieval | Storage tests and diagnostics |
| Local-first and small-model resilient | LM Studio loopback adapter, closed tools, repair, validation, retry budgets, deterministic fallback | Offline evals and tests |
| Optional cloud-model support | Explicit Local/Hybrid/Cloud routing with no silent fallback | Configuration/source tests; credentialed call unverified |
| Proactive finance workflow | Idempotent Daily Close job with events, checkpoint/resume semantics and receipts | API integration tests and golden flow |
| Finance-document preparation | Evidence-linked HTML and PDF owner pack | Runtime fixture and artefact tests |
| Telegram-style expense context | Sealed message + attachment fixture through immutable source pipeline | Fixture integration proof only |
| Akahu for New Zealand | Sealed fixture plus real config-gated, read-only live seam | Mock provider proof; real account unverified |
| Plaid for US / overseas | Sealed fixture plus config-gated sandbox Link / sync | Mock provider proof; real sandbox Item optional |
| Practical NZ CSV ingestion | Canonical, ANZ-style debit/credit and ASB-style signed amount mappings | Exact-money and dedupe tests |
| Reversible actions | Event receipt, visible Undo and superseding audit history | Integration and UI proof |
| Clean-room use of Hermes/Bionic lessons | Independent implementation and explicit attribution/clean-room records | Repository documentation |

## 9. Remaining gaps against the full product vision

The following are product gaps, not hidden claims. They are ordered by the value they add to the broader vision rather than by hackathon necessity.

### P0 — required before calling Folio production-ready

1. **Real Akahu onboarding and credential lifecycle.** The live API seam exists, but the product still needs an accredited OAuth or securely managed Personal App flow, token rotation/revocation, connection expiry handling and a successful real-bank acceptance run. It should never ask an owner to paste enduring tokens into the ordinary UI.
2. **Encryption, identities and workspace isolation.** The Build Week app is a single local Koru workspace. Production needs encrypted-at-rest storage, key recovery, owner/accountant roles, multiple businesses, backup/restore, migration guarantees and destructive-data controls.
3. **Accounting-system authority.** Folio prepares finance work but is not a replacement general ledger. Xero/MYOB exports or integrations, chart-of-accounts mapping, GST periods, lock dates, reconciliation status and accountant handoff need explicit schemas and end-to-end tests.
4. **Release packaging.** The web/Electron source builds, but signing, notarisation, auto-update, installer smoke tests and a judge-friendly packaged build remain to be completed.
5. **Real-provider security review.** Threat modelling, secrets review, dependency scanning, audit-log retention, connector rate limiting, privacy policy and incident/recovery documentation are required before real financial records are trusted to it.

### P1 — core to Daniel's “always-ready finance operator” vision

1. **A real proactive worker.** Daily Close is a bounded job, not yet a durable OS service that wakes on schedule, survives restarts, monitors new sources and sends a morning brief. Add a local scheduler, leases, retry/backoff, quiet hours, notification preferences and digest deduplication.
2. **Real Telegram and WhatsApp ingestion.** Telegram is currently a sealed fixture and WhatsApp is absent. Production needs authenticated webhooks or polling, attachment download/quarantine, sender-to-business binding, consent, replay protection, encrypted storage and a conversational correction loop.
3. **Background document intelligence.** The owner pack is generated, but Folio does not yet watch an inbox/folder and reliably extract receipts, invoices and remittances. Add local OCR/layout extraction, document classification, line-item/tax extraction, transaction matching, duplicate-file detection, uncertainty/abstention and source highlighting.
4. **Learned categorisation from corrections.** Current rules are deterministic and transparent. Add an explainable local baseline trained from owner corrections, calibrated confidence, explicit abstention and per-business evaluation. The learned suggestion must never silently post a ledger effect.
5. **Calibrated forecasting.** The current 30-day scenario is deterministic and useful for explanation, but it is not a learned forecast. Add recurring-obligation detection, receivable-payment distributions, scenario intervals, backtesting and calibration reporting. Avoid presenting a point estimate as certainty.
6. **Proactive alerts outside the open app.** Cash/reserve findings exist inside Folio. The wider vision needs useful, rate-limited alerts for overdue invoices, unexpected spend, missing evidence and cash risk, with deep links back to the exact explanation and source.
7. **More complete finance-document output.** Add invoice/remittance drafting, GST working papers, reconciliation packs, debtor follow-up drafts, accountant request lists and versioned template/custom-brand support.

### P2 — quality and breadth improvements

1. **Broader local-model qualification.** The harness needs a published matrix across representative small models, quantisations and context sizes, measuring completion, valid tool use, repair rate, latency and deterministic-fallback rate on the same finance tasks.
2. **Hybrid privacy controls.** Add field-level redaction, a per-turn egress preview/policy, local embedding selection and evidence showing exactly what left the device when cloud mode is chosen.
3. **Richer contradiction and temporal reasoning.** The structured memory supports provenance and supersession, but entity merging, jurisdiction changes, retroactive corrections and competing source authority need wider adversarial fixtures.
4. **Multi-account and multi-currency support.** Current practical ingestion intentionally supports NZD and fails closed when an account cannot be identified. Production needs explicit account mapping, transfers, foreign currency, fees and exchange-rate provenance.
5. **Collaborative accountant experience.** Add a scoped review link or local export bundle, comment/resolve states, preparer/reviewer separation and a concise change ledger suitable for an external accountant.
6. **Accessibility and long-session QA.** Continue testing screen readers, keyboard-only use, high zoom, long conversations, large source sets, slow streaming, cancellation and lower-powered computers.
7. **Contributor and security model.** The repository is licensed under Apache-2.0. It still needs a contributor guide, security reporting process and an explicit review of which connector/provider modules can be redistributed.

## 10. What not to claim in the submission

- Do not claim that a real Akahu bank was connected; only the seam and provider mock are verified until a credentialed run succeeds.
- Do not call the Telegram fixture a live bot or claim WhatsApp support.
- Do not claim that the optional cloud path was exercised unless a credentialed response is captured.
- Do not claim that the deterministic cash scenario is an ML prediction.
- Describe Folio as Apache-2.0 open-source software, while keeping provider credentials and third-party service terms separate from the code licence.
- Do not call a local build a signed/notarised packaged app.
- Keep synthetic Koru Studio evidence visibly separate from any future real business data.

## 11. Recommended post-submission build order

1. Run one real read-only Akahu acceptance sync and fix any payload/identity discrepancies.
2. Package and sign the desktop app.
3. Add the durable proactive worker and notification receipts.
4. Implement local document ingestion/reconciliation with a measured fixture set.
5. Replace Telegram fixture with a secure real connector, then add WhatsApp only after the same trust boundary is proven.
6. Benchmark representative small local models and publish the harness matrix.
7. Add learned categorisation and calibrated forecasting only when each beats a simple baseline on held-out data.
8. Add contribution and security policies, and document third-party connector redistribution boundaries.

The target remains: **Folio already understands the whole business; the owner asks in ordinary language, and Folio quietly brings forward the right explanation, number, visualisation or document.** The deterministic and agentic complexity stays under the hood, while every material financial claim remains inspectable and correctable.
