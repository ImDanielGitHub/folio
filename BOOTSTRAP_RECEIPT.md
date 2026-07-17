# Bootstrap receipt

**State:** coordinator bootstrap lock, ready for isolated builders  
**Branch:** `main`  
**Date:** 17 July 2026, Pacific/Auckland  
**Runtime proof:** contracts/fixtures only; no finance engine, agent harness, API runtime or Electron runtime is claimed

## Canonical ports and toolchain

- API: `127.0.0.1:4317` (loopback only)
- renderer: Vite-selected development port
- LM Studio default: `127.0.0.1:1234/v1`
- Python: `3.12`; Node.js: `>=22`; pnpm: `10.33.0`

## Canonical golden-path IDs

- workspace: `ws_koru_studio`
- continuing thread: `thr_koru_studio_main`
- account: `acct_koru_business`
- CSV source: `src_koru_bank_csv_20260717`
- Telegram fixture source: `src_koru_telegram_910001`
- Daily Close run: `run_koru_daily_close_20260717`
- run receipt: `rcpt_koru_daily_close_20260717`
- correction event: `evt_koru_rule_mitre10`
- Undo event: `evt_koru_rule_mitre10_undo`
- classification rule: `rule_koru_mitre10_under_500`
- findings: `finding_koru_missing_receipt`, `finding_koru_duplicate_pending`, `finding_koru_reserve_risk`
- six surface IDs and all row/transaction/evidence/artifact IDs: `fixtures/demo/canonical-ids.json`

## Locked schemas

All schemas use JSON Schema Draft 2020-12. Contract objects and nested catalogue objects are closed with `additionalProperties: false`; monetary fields use integer minor units plus ISO currency.

- `common.schema.json` — IDs, hashes, money, evidence, transactions and forecast points
- `api-snapshot.schema.json` — workspace/thread/canvas/findings/activity/sources/totals/artifacts
- `run-event.schema.json` and `run-event-stream.schema.json` — ordered HTTP/SSE lifecycle envelopes
- `finance-plan.schema.json` — maximum five closed actions; write plans cannot supply affected transaction IDs
- `dialogue-frame.schema.json` — provider-independent frame, one active question and source-turn claims
- `finance-event.schema.json`, `undo-request.schema.json`, `undo-response.schema.json` — append/invert audit boundary
- `model-receipt.schema.json` and `egress-receipt.schema.json` — capability, privacy and egress proof
- `finance-surface-spec.schema.json` — `FinanceSurfaceSpec@1`, six surfaces, nine blocks and five actions

The validation manifest is `contracts/manifest.json`. The desktop lane should consume `fixtures/ui/workspace-snapshot.json`, the two canonical surface fixtures and `daily-close-events.json` without requiring a backend.

## Synthetic finance lock

- cleared balance: `504576` NZD minor units
- business income: `725000`; business expenses: `139499`; personal expenses: `62450`; unresolved: `18475`
- protected reserve: `200000`
- 30-day low after the planned laptop: `190077`; reserve shortfall: `9923`
- duplicate pending Figma row excluded: `3000`
- Mitre 10 correction affects only `txn_koru_006` under a `50000` maximum; unresolved becomes `0`, business expenses become `157974`
- classification changes forecast provenance/uncertainty but not bank cash; low point remains `190077`
- second Daily Close expectation: `no_op`, with zero new findings, artefacts or owner messages

## SHA-256 lock

Bundle hashes are SHA-256 over each sorted relative path, a NUL byte, its bytes, and a trailing NUL byte.

| Surface | SHA-256 |
|---|---|
| `contracts/schemas/*.json` | `cdff34ab2ca4637f0db2c534298b33139ce6ea759de50c37adda7a2873848556` |
| `contracts/examples/*.json` | `7e0b02b3eb04ccf1447d793b19da71777f72dee6ac457a30df6f40c0e6e9835c` |
| `contracts/manifest.json` | `efafc89af9d10683903148022678c1c29bcd051ed35e6fd4edde506458b36dff` |
| `fixtures/demo/*` | `4ca58658af3f1107875cb524353650cc5c9015ad64e1df46d2cd8bb072aaa3de` |
| `fixtures/ui/*.json` | `4752b1b0a7efb9d5eeb4a4ddc23622ecec375ad40346be0eb1a5acde739f2781` |
| canonical CSV bytes | `c2c07beeca632f4e09700837cc4b199653ce9b68f65b804b7c30e9838ef94eac` |
| canonical Telegram Update bytes | `58ec491e7cc4fcec630614b3db24da72986da10d0c52f813dfddfc101bc7e4a6` |
| four research documents | `f1cb0b6e4c86bd7eeeb2e7083ce811ae61aa7cddb454b1d323340503126517bc` |
| `pnpm-lock.yaml` | `17c814b167307942d3609c7b9d916ceddb85839573ab39baa114e30edb132a1a` |
| `services/api/uv.lock` | `84fcee0436de81aa84b9326afa2550388843dc2b648b2631e48365ec0822342c` |

## Lane ownership from this commit

- Task 1: `finance/**`, `storage/**`, `jobs/**`, `artifacts/**`, matching tests and `fixtures/demo/**`
- Task 2: `agent/**`, `models/**`, `connectors/**`, `api/routes/**`, matching tests and `evals/**`
- Task 3: `apps/desktop/**`, its tests and `fixtures/ui/**`
- Coordinator only: `/contracts`, root workspace/locks/docs/scripts, Python manifest/lock, `api/app.py`, integration and golden proof

## Validation receipt

- `pnpm install` — PASS; root lock generated with no JavaScript runtime dependencies
- `uv sync --project services/api --all-groups` — PASS; Python 3.12 environment and `uv.lock` generated
- `pnpm contracts:check` — PASS; 12 schema cases plus demo arithmetic, IDs, source digests, integer minor units and event ordering
- `ruff check scripts services/api/src` — PASS
- Python bytecode compilation for `scripts` and `services/api/src` — PASS
- placeholder command proof — `dev` exits non-zero with `not implemented by lane yet: dev (coordinator-integration)`

The remaining root commands are intentionally explicit bootstrap placeholders until their named lane or coordinator integration replaces them.
