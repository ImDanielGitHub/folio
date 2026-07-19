# Worker H — Desktop parity (lanes C+D)

**Date:** 2026-07-20  
**Package:** `apps/desktop`  
**Theme:** Dark Folio Paper tokens only  
**Typecheck:** `pnpm --filter @folio/desktop typecheck` — **pass**

## Files changed

| File | Lane | Change |
|---|---|---|
| `apps/desktop/index.html` | C | Dark theme-color; Instrument Sans + IBM Plex Mono |
| `apps/desktop/src/styles.css` | C+D | Dark tokens; tool trace / fallback / ingest calm styles |
| `apps/desktop/src/Onboarding.tsx` | C | Demo · Akahu · CSV; Akahu consent substep (ANZ Everyday) |
| `apps/desktop/src/Drawer.tsx` | C | Sources (CSV/Akahu/Telegram); Activity (Mitre undo, Close, Akahu); Privacy + model list |
| `apps/desktop/src/App.tsx` | C+D | Calm working status (no stage theatre); Stop; fallback banner; Undo toast; ingest progress; tool trace; canvas nav |
| `apps/desktop/src/ToolTrace.tsx` | D | **New** — Paper 13 tool steps panel from `tool.started` / `tool.completed` |
| `apps/desktop/src/transport.ts` | Transport | `ingestAkahuFixture` → `POST /v1/ingest/akahu-fixture` + fixture fallback |
| `apps/desktop/src/types.ts` | C | Source types: `akahu`; statuses `live` / `linked` |
| `apps/desktop/src/fixtures.ts` | C | Paper 16/17 source + activity rows |

## Product locks covered

- Dark tokens: ground `#0D0E0E`, surface `#161716`, raised `#232422`, line `#34332F`, text `#F2EFE9`, muted `#AAA7A1`, accent `#D98558`, positive `#79A88C`
- Conversation-first + dynamic canvas; no Daily Close stage N/M
- Onboarding 06/06b with Akahu; drawers 16/17/12/15; tool trace 13; Stop 22; fallback 23; Undo restored 24; ingest 25
