# Security policy

Folio handles sensitive financial information even when it runs only on the owner's computer. Confidentiality, integrity, provenance and local process isolation are product requirements.

## Reporting

Use GitHub private vulnerability reporting when enabled. Otherwise contact the repository owner privately. Include the affected commit, operating system, smallest reproduction, expected boundary and whether real data or credentials were exposed. Never attach live bank exports, tokens or customer records.

## Current boundary

The supported development version is the latest commit on `main`. The API must bind only to loopback. Electron uses context isolation, a sandboxed preload bridge, no renderer Node integration, exact renderer-origin checks and default-deny permissions. Finance effects come from deterministic services, not model prose.

Provider credentials are opt-in and process-scoped. They must not be committed, included in fixtures or logs, or returned from capability endpoints. The optional per-launch `FOLIO_SESSION_TOKEN` protects state-changing loopback requests made by the normal launcher.
