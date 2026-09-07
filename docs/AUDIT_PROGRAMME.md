# Folio implementation and verification register

Folio's audit is a 200-item programme, not a claim of 200 completed changes.
PRs containing only transformation scripts or one-shot workflows are staging
material. They do not establish that the proposed product functionality exists.
Each delivery below points to a real product diff and its verification boundary.

## Merged foundations

| PR | Delivered area | Boundary |
| --- | --- | --- |
| #2 | Initial correctness/security foundation | Earlier foundation; not proof every security issue was resolved |
| #55 | Provider/run semantics and API controls | Merged source and tests; live provider acceptance remains separate |
| #56 | Typed protocol, complete snapshot validation, safe read retries and explicit fixture choice | Supersedes staging-only #6; durable cancellation/replay remains separate |

## September implementation sequence

| PR | Delivered area | Verification before merge |
| --- | --- | --- |
| #57 | Loaded language-model selection, active context capacity, local token/timeout configuration, final-answer validation | CI 34077608768; 130 Python tests plus full repository gate |
| #58 | Read-only analytical routing, owner question and retrieved context in local explanations, model explanation after safe read-plan fallback | CI 34078973105; 151 Python tests including restart/context handoff |
| #59 | Exact cash-period analysis, recorded monthly/category drivers, explicit source coverage and authenticated analysis endpoint | CI 34080078722; 172 Python tests including actual HTTP turn/model wiring |
| #60 | Authenticated document downloads, bounded digest checks, private viewer copies and main-frame IPC | CI 34081422885; 173 Python tests, 28 desktop tests and real macOS runtime smoke |

The model can interpret checked figures and contextualise the owner's question.
Arithmetic, transaction selection and financial writes remain controlled code.
Local mode does not silently call the cloud. Hybrid/cloud context continues to
pass through the existing projection policy; local history is not silently sent.

### Startup preservation

The application composition now refuses automatic demo reset when an existing
workspace lacks its current snapshot. Regression tests cover an interrupted
initial close, a lost current-snapshot pointer, all three runtime modes, preserved
source/history, and healthy/fresh startup. This is prevention of destructive
bootstrap behaviour, not an automated corruption-repair or backup system.

The guard is in the canonical `create_app` composition. The legacy fixture-oriented
base service is not a supported alternate application entry point. Explicit demo
reset remains a separate destructive development action; it is not a recovery
procedure for valuable records.

## Native proof

The macOS CI job launches the real Electron entry point against a disposable
synthetic local API. Its receipt records `app://folio/index.html`, the Electron
preload bridge, unavailable renderer `require`, authenticated snapshot status 200
and successful PDF-opening IPC. The corresponding screenshot was inspected.

This proves that tested runtime path on the CI Mac. It does not prove a signed or
notarised installer, every UI interaction, Daniel's personal Mac, real-model
financial judgement, a real bank connection or an external delivery.

## Queue cleanup

Staging-only #6 was closed as superseded by merged #56. Duplicate Telegram draft
#29 was closed in favour of the corrected continuation #31. Their branches are
preserved. Closing a duplicate does not count as implementing its proposal.

Other staging drafts remain unmerged until their actual changes can be applied
against current main, reviewed and tested. Do not merge apply-script workflows
just to reduce the open-PR count. The source-export helper stays on its auxiliary
branch and is not part of the application or main's release workflow.

## Remaining workstreams, not delivered claims

| Workstream | Outstanding acceptance |
| --- | --- |
| Durable runs and cancellation | Persisted lifecycle/events, cancel checkpoints, safe retries and authoritative client reconciliation |
| Context and model quality | Actual token budgets, retrieval quality, measured capability tiers, adversarial tests and live local-model evaluation |
| Financial analysis | Explicit chat date/account filters, complete-period reconciliation, richer scenarios, concentration/recurrence analysis and user-facing analysis surfaces |
| Data protection | Encrypted storage and key lifecycle, verified backup/restore, migration failure recovery and complete workspace isolation |
| Product and accessibility | Actual key UI flows, keyboard/screen-reader tests, truthful progress and error recovery, document accessibility |
| Connectors and accounting | Real Akahu lifecycle, authenticated messaging, document ingestion and accountant/Xero/MYOB handoff with provider acceptance |
| Release engineering | Packaged sidecar lifecycle, installers, signing/notarisation, authenticated updates and release acceptance |

## Review rules

A merge requires the intended product diff, fresh passing checks and a pinned
expected head SHA. A test count is not an audit-item completion count. Native
runtime, model behaviour, external-system acceptance and released state remain
separate evidence levels. Preserve source records and unfinished branches. Never
replace missing financial data with reassuring demo results.
