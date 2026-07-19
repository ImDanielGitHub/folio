# Connector boundary

## Telegram

Telegram is the mobile doorway. Tests ingest recorded Update/photo references and
produce an outbox payload; real `getUpdates`/`sendMessage` calls are disabled
unless `TELEGRAM_LIVE_ENABLED=true`, a bot token exists and one chat is
allowlisted. The adapter never sends in tests.

## Akahu

Akahu is a first-class **read-only** bank connection path for Folio onboarding.

| Path | When | Network |
|---|---|---|
| `POST /v1/ingest/akahu-fixture` | Golden demo / CI / offline | Never |
| `AkahuReadOnlyAdapter` | Optional live pages | Only when `FINANCE_AKAHU_ENABLED=true` + tokens |

Fixture sync creates an `akahu_fixture` source item, evidence links, and
transactions for Koru Studio (ANZ Everyday). Live API credentials are accepted
only for `https://api.akahu.io`. The live adapter returns typed pages and does
not calculate money; exact-money normalisation stays in the finance engine.

Production Akahu still requires provider-approved OAuth/consent, scope,
token-storage, retention, privacy and monitoring decisions. The submission story
must not claim a live bank connection unless those credentials are configured
and verified.
