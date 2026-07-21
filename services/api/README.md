# Folio local API

The Python 3.12 service is Folio's loopback finance and agent runtime. It owns deterministic finance truth, SQLite persistence, Daily Close, document preparation, the working-understanding index and bounded model routing.

From the repository root:

```bash
uv sync --project services/api --frozen
pnpm api
```

The service binds to `127.0.0.1:8787`. It does not require a model for deterministic or fixture workflows. LM Studio is optional on `127.0.0.1:1234`; OpenAI is optional and remains unconfigured unless `OPENAI_API_KEY` is explicitly supplied.

Run its tests with:

```bash
pnpm test:python
```

All committed test data is synthetic. Do not use real owner, customer or banking data in repository fixtures.

## Optional read-only Akahu sync

The sealed Akahu fixture remains available without credentials. A personal or
authorised enduring Akahu connection can also be supplied to one API process:

```bash
FINANCE_AKAHU_ENABLED=true \
AKAHU_APP_TOKEN='<app token>' \
AKAHU_USER_TOKEN='<user access token>' \
pnpm api
```

Folio does not save those tokens. It pins requests to `https://api.akahu.io`,
uses only the accounts and settled-transactions read endpoints, and reports
configuration without making a provider request:

```bash
curl http://127.0.0.1:8787/v1/connections/capabilities
curl -X POST http://127.0.0.1:8787/v1/connectors/akahu/sync \
  -H 'Content-Type: application/json' \
  -d '{"start":"2026-07-01","end":"2026-07-21"}'
```

The optional dates are inclusive New Zealand calendar dates and are limited to
a 366-day window. Synced transactions enter the same exact-cents, immutable
source-evidence path as local bank CSV imports.
