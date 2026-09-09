# Connector boundary

Telegram is the only P0 mobile doorway. Tests ingest recorded Update/photo
references and produce an outbox payload; real `getUpdates`/`sendMessage` calls are
disabled unless `TELEGRAM_LIVE_ENABLED=true`, a bot token exists and one chat is
allowlisted. The adapter never sends in tests.

Akahu is an optional read-only provider seam informed by the MIT-licensed Hermes
provider boundary. It is dormant unless `FINANCE_AKAHU_ENABLED=true` and both
tokens are process-injected. Credentials are accepted only for
`https://api.akahu.io`; the adapter returns typed pages and does not calculate
money, normalise rows, persist tokens, write provider state or participate in the
golden path. Task 1 owns exact-money normalisation, source commits and cursor
transactions. A production Akahu connection still requires provider-approved
OAuth/consent, scope, token-storage, retention, privacy and monitoring decisions.
