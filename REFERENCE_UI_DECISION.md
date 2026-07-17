# Reference UI decision

**Decision date:** 17 July 2026  
**Reference source:** Refero, inspected live in this research task  
**Decision:** use one Manus workspace screen as the structural north star and one Brex style reference as token discipline. Do not create a moodboard and do not copy either product’s visual identity.

## 1. Exact reference lock

### Primary screen composition — Manus

- **Refero screen ID:** `ede70d77-f1cb-4902-a407-c608023df5a9`
- **Site:** Manus (`manus.im`), Refero site ID `843`
- **Captured product context:** “Adding more slides using AI”, step 7 of 10, Refero flow ID `10447`
- **Refero page:** [screen metadata](https://refero.design/pages/ede70d77-f1cb-4902-a407-c608023df5a9)
- **Source page recorded by Refero:** `https://manus.im/app/knbBNoLNBqugUB9eWoKTQ4?vnc=1`
- **Full preview:** [Refero-hosted preview image](https://images.refero.design/screenshots/manus.im/desktop/b786a64c-9a76-4d19-8952-f26b9b2ba33e_preview.jpg)
- **Thumbnail:** [Refero-hosted thumbnail](https://images.refero.design/screenshots/manus.im/desktop/b786a64c-9a76-4d19-8952-f26b9b2ba33e_thumb.jpg)
- **Refero metadata:** web; AI Assistant, Chat & Messages, Loading & Connecting, Article & Text; vertical rail, split view, fixed composer, content preview, side-sheet, progress card; San Francisco and UI Monospace; sampled colours include `#10223C`, `#97A0AD`, `#FAFAF7`, `#576985`, `#5F615E`.

![Manus Refero reference](https://images.refero.design/screenshots/manus.im/desktop/b786a64c-9a76-4d19-8952-f26b9b2ba33e_preview.jpg)

### Visual-system discipline — Brex

- **Refero style ID:** `7471a4ea-ab61-4281-8f19-2d65352efc44`
- **Title/source:** Brex, `https://brex.com`
- **Refero style thesis:** precision-engineered, high-contrast financial product; light surfaces; strong typographic hierarchy; restrained neutral palette; vivid orange reserved for primary actions; minimal shadow; rectangular, lightly rounded panels.
- **Preview discovered in Refero:** `https://images.refero.design/styles/brex.com/7471a4ea-ab61-4281-8f19-2d65352efc44/preview_0.jpg`

Brex is not a second composition reference. It is a guardrail against generic pastel “AI finance” styling and over-carded dashboards.

## 2. Why this screen won

The Manus screen already solves the closest structural problem:

- a persistent assistant interaction surface and a large generated artefact coexist without route changes;
- the artefact is visually primary, while the composer remains permanently available;
- background work is visible as calm progress rather than an exposed agent plan;
- a right side-sheet supports inspection without turning the whole product into admin chrome;
- the narrow rail keeps infrequent controls accessible without a full application sidebar;
- the composition can absorb a preview, code, document or structured result without changing the shell.

Claude Artifacts is the obvious conceptual analogy, but the Refero search did not surface a useful authenticated Artifacts screen. Manus provides an exact, inspectable reference with the required metadata and image. The decision is therefore evidence-led rather than brand-led.

## 3. Structural translation into the finance workspace

Use the Manus composition, with finance-specific roles:

```text
┌──────┬───────────────────────────┬─────────────────────────────────────────────┐
│ rail │ continuing thread         │ dynamic financial canvas                    │
│ 48px │ 360–440px / 34–40%        │ remaining width / 60–66%                    │
│      │                            │                                             │
│ logo │ morning close update       │ title + freshness + canvas-specific action  │
│ src  │ owner’s long answer        │                                             │
│ act  │ adaptive question          │ living brief / transaction / forecast /     │
│ priv │ compact source chips       │ records / owner pack / work receipt         │
│      │                            │                                             │
│      │ fixed multiline composer   │ one contextual drawer may overlay this pane │
└──────┴───────────────────────────┴─────────────────────────────────────────────┘
```

### Exact desktop measurements

- Target first: 1440×900; verify 1280×800.
- App rail: 48px, no labels until hover/focus tooltip.
- Thread: default 400px, resizable between 340px and 520px.
- Split divider: 1px, 12px invisible hit area; preserve the user’s width locally.
- Canvas: minimum 680px. Below a 1080px window, switch to thread-first tabs rather than compressing data.
- Top utility strip: 44px inside both panes, not a global marketing header.
- Composer: anchored to the thread bottom, auto-grows to 220px, remains usable while work streams, and always exposes Stop.
- Canvas content width: fluid, with 24px outer padding and a maximum readable narrative width of 760px.
- Drawers: 420px or 38% of canvas, whichever is smaller; one open at a time; focus trapped; Escape closes and restores focus.

### Role mapping from the reference

| Manus reference element | Finance product adaptation |
|---|---|
| Left icon rail | Sources, Activity & Undo, Connections & Privacy; no generic navigation |
| Conversation/progress column | The single continuing business thread and fixed composer |
| Slide preview | The current financial canvas surface |
| “Manus’s Computer” side-sheet | Contextual evidence/detail drawer over the canvas |
| Task progress card | Compact work receipt/activity entry; never expose hidden reasoning |
| Code editor | Not copied; finance evidence, change diff or document preview takes its place |
| Slide count | Optional source/record count only when it communicates real scope |

## 4. What may be copied structurally

- The three-zone hierarchy: narrow rail, persistent conversational pane, larger work/artefact pane.
- A bottom-fixed multiline composer in the conversation pane.
- A large, replaceable artefact region that updates in place.
- A contextual inspector/drawer instead of a route change.
- Quiet progress/status rows that can collapse after completion.
- Fine dividers, restrained rounding, one primary accent and comfortable density.
- The idea that the output remains visible while the owner continues talking.

## 5. What must be original

- Product name, logo, icon set, copy, data, illustrations and all finance components.
- The canvas catalogue, chart grammar, table structure, receipt design and source/evidence interactions.
- Colour values, typography choices and component implementation.
- Layout proportions and responsive behaviour described above.
- Motion, empty/offline/error states, onboarding, privacy copy and Telegram UI.
- No Manus underwater slide, code styling, task labels, assets or microcopy.
- No Brex wordmark, Flecha typeface, exact `#ff5900` action colour, device mockups or branded component shapes.

## 6. Original design tokens for implementation

These values translate the reference principles without cloning either source:

```css
:root {
  --app-bg: #f4f5f1;
  --surface: #ffffff;
  --surface-subtle: #f8f8f5;
  --ink: #171a18;
  --ink-muted: #66706a;
  --border: #dfe3de;
  --border-strong: #c9cec8;
  --action: #c4512f;          /* original burnt-copper action colour */
  --action-hover: #a94328;
  --positive: #2f6b50;       /* data/status only */
  --warning: #a26422;        /* data/status only */
  --negative: #a84135;       /* data/status only */
  --focus: #315f9c;
  --radius-control: 7px;
  --radius-panel: 11px;
  --shadow-float: 0 10px 30px rgb(23 26 24 / 0.08);
}
```

- Font: Inter (SIL Open Font Licence) with `system-ui` fallback; IBM Plex Mono (SIL OFL) only for IDs/raw evidence.
- Base text: 14px/1.5. Thread prose: 15px/1.55. Canvas title: 24px/1.2, weight 600. Large metric: 32px/1.05, tabular numerals.
- Use tabular numbers and right alignment for monetary columns.
- Use Action Copper only for primary actions, active focus or a selected canvas state. Semantic red/green/amber appear only where meaning requires them.
- Panels use a 1px border and no shadow. Drawers/menus may use the single float shadow.
- Do not render a grid of KPI cards. The living brief is an editorial sequence with one headline, one material change, a compact forecast and the next prepared action.

## 7. Component and generative-UI stack

### P0 stack

- Electron + Vite + React + TypeScript.
- Radix UI primitives for Dialog, Sheet, Tooltip, Popover and accessible focus management.
- `react-resizable-panels` for the locked desktop split.
- TanStack Table for the records surface.
- Recharts for the P0 cash series and scenario comparison; wrap it behind finance-owned components.
- Lucide icons, using a consistent 16px/1.75px stroke treatment.
- React Query for snapshots and invalidation.
- A small ordered SSE reducer for run/activity/surface events.
- A finance-owned `FinanceSurfaceSpec@1` validated with Zod at the renderer boundary.
- `@ai-sdk/react` only if a custom transport cleanly consumes the application’s stream; do not bend the backend contract to fit it.

### Generative-UI decision

Use **catalogue-constrained generative UI**, not model-generated frontend code. The available surface and block types are compiled into the product. The agent selects a surface intent; deterministic services populate money, records, evidence and actions; the renderer validates and mounts native React components.

This borrows A2UI’s separation of surface structure, data and client-controlled component catalogues. It does not require A2UI in P0 because the product has six known finance surfaces, the application already controls both ends, and adding a protocol renderer would create more integration work than user value. Keep the schema close enough to map to A2UI v0.9.1 later if ecosystem interoperability becomes material.

Likewise:

- shape the ordered event envelope after AG-UI’s lifecycle and snapshot/delta patterns, but do not import the full protocol in P0;
- reserve MCP Apps for exporting finance tools/canvases into external conversational hosts, not for rendering the app’s own trusted canvas in an iframe;
- do not add CopilotKit, assistant-ui and Vercel AI SDK simultaneously. One thread state abstraction is enough.

## 8. Required states for the UI builder

The builder must implement and capture:

- first useful launch with seeded data;
- no sources yet;
- Daily Close running, with stage progress but no raw chain of thought;
- partial result when the model is unavailable but deterministic close succeeded;
- Local model loading/unloaded;
- Cloud disabled or missing credentials;
- new source receipt;
- long owner answer with Stop available;
- canvas transition from living brief to cash scenario;
- reversible rule receipt and Undo hover/focus/pressed states;
- offline mode;
- drawer focus, close and return-focus behaviour;
- narrow window thread/canvas tabs;
- high-contrast keyboard focus and reduced-motion behaviour.

## 9. Provenance and licence boundary

Refero supplied research metadata and screenshots; it did not grant permission to redistribute or derive branded assets. The screenshot links belong in this internal build brief for reference. Do not download them into the product repository, ship them, use them in marketing or include them in an open-source release without confirming Refero and source-site permissions.

Manus and Brex are proprietary products and their trade dress, brands, assets and private implementation are not reusable code. Functional layout ideas and general design principles may be studied; implementation must be independently authored. Use only openly licensed dependencies and fonts, preserve their notices, and document any Hermes-derived MIT code separately in the reuse map.

## 10. Acceptance test for reference fidelity

A cold viewer should recognise the Manus-derived **workspace behaviour**—conversation beside a live artefact with a contextual inspector—without mistaking the result for Manus or Brex. If the interface looks like a finance dashboard with a chat sidebar, the reference translation has failed. If it looks like copied Manus with financial labels, the provenance boundary has failed.
