# Contributing to Folio

Folio is a local-first finance operator. Changes must preserve the boundary between model-authored language and deterministic finance truth.

## Setup

```bash
pnpm install --frozen-lockfile
uv sync --project services/api --frozen
```

Run the complete gate before review:

```bash
pnpm verify
```

## Engineering rules

1. Amounts, transaction selection, effects, forecasts, evidence and generated documents remain deterministic.
2. Source records and owner statements are immutable. Corrections append replacement state and supersession links.
3. Model output must pass closed schemas and bounded validation before execution.
4. Local mode must never silently call a cloud provider.
5. External connectors are disabled by default and require fixture-backed tests.
6. Fixtures must remain fictional and contain no real banking, customer or credential data.
7. A finance mutation needs an idempotency rule, an evidence trail and a reversible event or an explicit reason it cannot be reversed.
8. Do not describe source, test, build, runtime, release or provider proof as interchangeable.
