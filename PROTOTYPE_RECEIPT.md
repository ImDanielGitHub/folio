# Folio prototype receipt

This receipt is being refreshed against the recovered persistent checkout. It separates source, tests, runtime, model and external-system proof. Final command results and browser captures are added only after the corresponding check completes.

## Current source

- Recovery source: `folio-recovery-48984ee.bundle`
- Verified recovery head: `48984eecbe7fb8c46042b2e8fff0e6ceb25c189a`
- Bundle SHA-256: `120e96f5c01c2eb2791270b9bd9ceda5a193e3bcac4fbfb830b06de0b2f57f7f`
- Active persistent checkout: `folio-recovered-workspace`

## Proof ledger

| Area | Status | Evidence |
|---|---|---|
| Contract validation | Refresh pending | `pnpm contracts:check` |
| Python tests | Refresh pending | `pnpm test:python` |
| Ruff/MyPy/TypeScript | Refresh pending | `pnpm lint`, `pnpm typecheck` |
| Production build | Refresh pending | `pnpm build` |
| Golden API flow | Refresh pending | `pnpm test:golden`, `pnpm demo:golden` |
| Offline harness | Refresh pending | `pnpm eval:offline` |
| Internal-browser UI | Refresh pending | Light desktop/mobile, dynamic canvas, evidence, onboarding and failure states |
| LM Studio | Prior smoke only | Qwen 3.5 9B loopback transport; full live four-case result not yet recorded |
| OpenAI cloud | Not live-verified | Adapter source and contract behaviour only; no credential supplied |
| Devpost | Draft only | Project `1328264`; no judging submission |

## Explicit non-proof

Passing tests do not prove a packaged judge build, public deployment, public repository, effective licence, live bot, bank connection, YouTube video or submitted Devpost entry.
