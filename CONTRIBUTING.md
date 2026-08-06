# Contributing to Folio

Folio is a local-first finance operator. Changes must preserve the boundary between model-authored language and deterministic finance truth.

## Development setup

From the repository root:

```bash
pnpm install --frozen-lockfile
uv sync --project services/api --frozen
pnpm dev
```

Run the full verification gate before opening a pull request:

```bash
pnpm verify
```

The gate checks contracts, Python linting and types, desktop TypeScript, Python tests, the production desktop build, and the offline model harness.

## Engineering rules

1. Amounts, transaction selection, effects, forecasts, evidence, and generated documents stay deterministic.
2. Source records and owner statements remain immutable. Corrections append replacement state and supersession links.
3. Model output must pass closed schemas and bounded validation before execution.
4. Local mode must never silently fall back to a cloud provider.
5. New external connectors must be disabled by default, use explicit configuration, and have fixture-backed tests.
6. Committed fixtures must remain fictional and contain no real banking, customer, owner, or credential data.
7. API changes require tests for validation, failure behaviour, and privacy-sensitive response handling.

## Pull requests

Keep each pull request focused. Explain the root cause or product gap, the chosen boundary, and the checks run. Do not mix broad formatting changes with behavioural work.

For user-visible changes, include the affected flow and any fixture or screenshot updates. For migrations, include forward compatibility and idempotency tests. For connector work, document the permissions requested and the exact information that can leave the device.

## Security reports

Do not open a public issue for a suspected vulnerability. Follow [SECURITY.md](SECURITY.md) instead.
