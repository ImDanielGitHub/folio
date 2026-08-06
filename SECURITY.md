# Security policy

Folio handles sensitive financial information even when it runs only on the owner's device. Treat confidentiality, integrity, provenance, and local process isolation as product requirements.

## Reporting a vulnerability

Use GitHub's private vulnerability reporting when it is enabled. Otherwise, contact the repository owner through a private channel. Include:

- the affected commit and operating system
- the smallest reliable reproduction
- the expected and actual security boundary
- whether real data or credentials were exposed
- any temporary mitigation already applied

Do not include live bank exports, API keys, access tokens, customer records, or owner documents in the report. Replace them with synthetic examples.

## Supported version

The latest commit on `main` is the only supported development version while Folio remains a prototype. Security fixes should be applied to `main` and called out clearly in the pull request.

## Security boundaries

Folio's local API is expected to bind only to loopback. The desktop renderer runs with context isolation, no Node.js integration, and a sandboxed preload bridge. Finance effects must come from deterministic services, not directly from model text.

Provider credentials are opt-in and process-scoped. They must not be committed, written to fixtures, included in logs, or returned through capability endpoints. Real financial data should not be used in tests or screenshots.
