# Attribution and reference register

Folio does not vendor or copy third-party product source, prompts, branding, data or UI assets. Declared package dependencies are installed through pnpm/uv and remain governed by their upstream licences.

## Pattern and interface references

| Source | Version/date | Use in Folio | Code/assets copied? |
|---|---|---|---|
| Hermes Agent / Hermes Finance | Inspected 17 Jul 2026; exact source commit in `SOURCE_REUSE_MAP.md` | Connector, model-adapter and finance-tool boundary research | No |
| LM Studio Bionic | App 1.0.0+1 plus public material | Local harness and split conversation/document principles | No |
| LM Studio developer API | Current public loopback API | Independently authored HTTP adapter | No |
| ChatGPT | Current conversation product | Calm chat-first composition and progressive disclosure | No |
| Manus Refero screen `ede70d77-f1cb-4902-a407-c608023df5a9` | Refero capture inspected 17 Jul 2026 | Historical split-workspace behaviour reference | No |
| Brex Refero style `7471a4ea-ab61-4281-8f19-2d65352efc44` | Refero capture inspected 17 Jul 2026 | Light finance typography and restrained accent research | No |
| Paper Folio prototype `01KXQWFD5CAYZWBV88B3WNFZCA` | Inspected during implementation | Folio-specific hierarchy, canvas and drawer interaction reference | No |

The Manus/Brex direction was subsequently narrowed by Daniel's correction: the default product is a light conversation, not a permanently visible split workspace or finance dashboard. That supersession is recorded in `REFERENCE_UI_DECISION.md`.

## Runtime dependencies

See `pnpm-lock.yaml` and `services/api/uv.lock` for exact dependency versions. React, Vite, Electron, FastAPI, Pydantic, SQLite bindings, ReportLab, httpx, pytest, Ruff and MyPy are ordinary package dependencies; none is vendored in this repository.

## Future adaptations

Before landing copied or substantially adapted third-party code or an asset, add its source, exact version/commit, licence, affected files and the nature of the adaptation here. An inspiration link alone is not a licence.
