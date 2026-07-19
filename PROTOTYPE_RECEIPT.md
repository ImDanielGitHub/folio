# Folio prototype receipt

Stamped against the local submission buildout (Paper dark shell + Akahu fixture path). Proof separates source, tests, runtime, model and external-system claims.

## Current source

- Recovery source: `folio-recovery-48984ee.bundle`
- Verified recovery head: `48984eecbe7fb8c46042b2e8fff0e6ceb25c189a`
- Bundle SHA-256: `120e96f5c01c2eb2791270b9bd9ceda5a193e3bcac4fbfb830b06de0b2f57f7f`
- Active workspace: `~/Documents/Finace App` (Folio dark Paper UI + Akahu fixture ingest)

## Proof ledger

| Area | Status | Evidence |
|---|---|---|
| Contract validation | Ready to re-run | `pnpm contracts:check` |
| Akahu fixture unit | Pass | `tests/connectors/test_akahu_fixture.py` — 3 passed offline |
| Demo reset hashes (5×) | Pass | `pnpm demo:reset-hashes` → canonical `5aed4a21f3929143816407ca915a536f858c3ceea764325bff81d766ac98744c` |
| Golden HTTP flow | Pass | `pnpm demo:golden` — Akahu fixture (6 rows), Daily Close, Mitre correction, undo, owner pack, Telegram fixture, cloud mode without credentials |
| Live API health | Pass | `GET /health` → `ready`, `loopback: true`, `externalCalls: disabled_by_default` |
| Akahu ingest (live) | Pass | `POST /v1/ingest/akahu-fixture` → `rowCount: 6`, `liveSyncAttempted: false` |
| Desktop typecheck | Pass | `pnpm --filter @folio/desktop typecheck` |
| Internal-browser UI | Pass (dark) | Onboarding 06/06b Akahu → privacy → workspace; Cash canvas; Sources drawer; fallback banner when LM Studio absent; evidence under `evidence/ui/folio-dark-brief-fallback-1440.png` |
| Offline harness | Wired | `pnpm eval:offline` / `pnpm test:local-model` → `evals/run_offline_harness.py` |
| LM Studio | Optional | Fallback banner verified when local model unavailable; no silent cloud |
| OpenAI cloud | Not live-verified | Adapter + capability path only; golden confirms `externalCallsMade: false` with absent credentials |
| Devpost | Draft ready | `DEVPOST_DRAFT.md` — project `1328264`; not submitted |

## Explicit non-proof

Passing local checks do not prove a packaged judge build, public deployment, public repository, effective licence, live Telegram bot, live Akahu OAuth marketplace connection, YouTube video or submitted Devpost entry.
