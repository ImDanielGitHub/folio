# Folio audit implementation programme

The repository audit contains 100 research-backed product and engineering improvements and 100 concrete fixes or build-outs. Folio ships them as dependency-ordered pull requests so every merge has its own proof boundary. A later stack may depend on an earlier merge, but no PR is described as complete until its own CI and review evidence is green.

## Ordered stack

1. **Correctness and security foundation — merged in PR #2.** Request and CSV bounds, safe artifact headers, session authentication, Electron origin and IPC controls, secure production protocol, cross-platform verification, foreign-currency Plaid quarantine, material-source Daily Close identity, real timestamps/counts, per-turn model provenance, and initial workspace-ownership guards.
2. **Provider and run semantics — merged in PR #55.** Audit items B003, B006–B012, B014, B017, B018, B040, B078, and B079: owner claims/policy/date in the Daily Close state vector, complete Plaid added/modified/removed history, cursor-loop rejection, strict provider payload validation, resolved database paths, truthful environment configuration, synchronous turn status, mutation-origin checks, production route gating, typed provider failures, and regression coverage.
3a. **Client protocol and failure truth — this pull request.** The reviewable subset of B031–B045 and B053–B057: RFC 9457 failures, typed client errors, bounded GET retry, complete runtime validators, incremental SSE, authenticated event/CSV requests, and explicit fixture selection.
3b. **Cancellation and concurrency — next.** B013, B015, and the remaining run-lifecycle parts of B031–B045 and B053–B057: persisted cancellation, durable event replay, narrower locks, request IDs, health/readiness, and authoritative idempotency/mode state.
4. **Model, egress, and evaluation evidence.** B029 and B046–B060: projection privacy scanning, measured capability cards, bounded retries, provider usage metadata, repair accounting, failed-run receipts, egress hashes, and adversarial evaluation sets.
5. **Storage durability and workspace isolation.** B030 and B091–B099: encryption/key lifecycle, smaller service boundaries, migration checksums, backup/integrity/restore, complete multi-workspace ownership, indexes, retention, export, and legal-hold behaviour.
6. **Desktop resilience, accessibility, and interaction tests.** B062, B065–B077, B080–B084: user-data storage, window/session recovery, real progress and reconciliation, URL/local-storage validation, WCAG runtime checks, accessible documents, React and Electron end-to-end tests, property tests, coverage gates, and contract mutation tests.
7. **Supply-chain and release engineering.** B085, B086, B089, B090, and B100: dependency review, Dependabot/CodeQL/SBOM, packaged sidecar lifecycle, signing/notarisation configuration, verified updates, safe environment loading, PID ownership checks, and generated proof receipts.
8. **Owner finance workflow.** I001–I025: attention brief, evidence ladder, classification/duplicate centres, recurring and receivable intelligence, scenarios/reserves, GST/tax preparation, reconciliation, period close, and multi-business foundations.
9. **Documents, accounting bridges, and communication.** I026–I045 and I093–I100: document ingestion/quarantine, accountant exports, Xero/MYOB/Peppol seams, authenticated messaging, encrypted backups, privacy controls, release evidence, and longitudinal research evaluation.
10. **Forecasting, explanations, local-model resilience, and product polish.** I046–I092: uncertainty-aware forecasts, anomaly and merchant intelligence, source-level explanations, model evaluation/degradation, undo/redo history, accessibility, performance, and packaging quality.

## Proof rules

- Finance amounts, classification effects, forecasts, evidence, and generated records remain deterministic.
- Provider changes are append-only. Modifications supersede prior events and removals are tombstones; source history is not rewritten.
- A green unit test does not prove a packaged runtime, signed release, real provider, or external delivery.
- Work requiring credentials, accreditation, legal judgement, signing identities, or live-provider acceptance is implemented up to the code/configuration boundary and remains explicitly unverified until that external evidence exists.


## Stack 3: client protocol and failure truth

This stack implements the reviewable protocol subset before cancellable background execution:

- RFC 9457 problem details for HTTP, validation and missing-resource failures;
- typed client errors with safe GET retry policy and bounded backoff;
- complete workspace snapshot validation rather than surface-only validation;
- incremental SSE parsing across arbitrary network chunk boundaries;
- session-authenticated event and CSV requests;
- honest degraded/offline states with no automatic fixture substitution;
- pure TypeScript protocol tests included in the desktop verification gate.

Persistent event replay and committed cancellation receipts remain in the next stack because they change the run lifecycle and storage authority together.
