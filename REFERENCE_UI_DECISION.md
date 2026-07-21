# Folio interface lock

**Final product correction:** 18 July 2026
**Paper file:** [Folio prototype](https://app.paper.design/file/01KXQWFD5CAYZWBV88B3WNFZCA/1-0)
**Decision:** light, chat-first Folio; dynamic finance/document canvas; evidence on demand.

## Experience thesis

Folio should initially feel like an exceptionally clean conversation product that already understands the business. The owner asks in ordinary language. Folio answers with the practical meaning and quietly brings forward the right number, chart, table, transaction, source document or prepared owner pack only when it helps.

The deterministic engine, retrieval receipts, run stages, model routing, tool calls and rule internals remain real but hidden by default.

## Default desktop composition

```text
┌──────┬──────────────────────────────────────────────────────────────┐
│ rail │                                                              │
│      │                 continuing Folio conversation                │
│  F   │              centred reading column up to 760px              │
│      │                                                              │
│      │                 one useful question at a time                │
│      │                                                              │
│      │                    fixed calm composer                       │
└──────┴──────────────────────────────────────────────────────────────┘
```

- The 68px rail holds only infrequent Sources, Activity and Privacy entry points.
- The default view has no permanent dashboard and no permanently open evidence browser.
- The reading column is calm, left-aligned and suitable for long owner answers.
- Finance controls do not compete with the conversation.

## Dynamic canvas composition

When a question benefits from a visual or document, the canvas opens beside the same thread:

```text
┌──────┬────────────────────────┬──────────────────────────────────────┐
│ rail │ same conversation      │ current finance or document surface │
│      │                        │                                      │
│      │ question and answer    │ chart / transaction / invoice /      │
│      │ compact source link    │ records / scenario / owner pack      │
│      │ fixed composer         │ contextual evidence on demand        │
└──────┴────────────────────────┴──────────────────────────────────────┘
```

- The canvas opens automatically from a relevant answer or explicit request.
- It may resize or take focus, and closes without navigating away from the thread.
- On narrow screens, Thread and Finance become two clear panes/tabs instead of squeezing data.
- Evidence opens as a focused drawer and returns the owner to the same point.

## Light product tokens

```css
:root {
  color-scheme: light;
  --bg: #f4f4f1;
  --rail: #efefeb;
  --thread: #fbfbf9;
  --canvas-bg: #f1f1ee;
  --panel: #ffffff;
  --text: #181816;
  --text-soft: #41403b;
  --muted: #68665f;
  --accent: #c96642;
  --positive: #397756;
  --warning: #9b6b27;
  --negative: #a5483d;
}
```

- Instrument Sans/Aptos/system sans for prose and financial numerals.
- One copper accent for brand/focus/primary action; semantic colours appear only with meaning.
- Soft warm-neutral surfaces, fine dividers and restrained elevation.
- No bright dashboard default, indigo AI gradients, KPI-card wall or decorative dashboard chrome. Folio now uses the dark Paper direction as the authoritative default.

## Evidence interaction

- Ordinary answers show a small “Based on N linked sources” affordance.
- Selecting it opens a legible evidence drawer with source name, human description and relevant excerpt/preview.
- Raw IDs, model receipts, tool traces and technical audit details are nested under deliberate disclosure.
- Undo is visible after a meaningful correction, without turning the whole response into an approval form.

## Required states

- one-step onboarding with Koru demo or local CSV;
- first useful answer and single highest-value question;
- long answer and Stop;
- Daily Close preparation, completion and truthful failure;
- chat-only, canvas opening, canvas focus and canvas close;
- transaction detail, scenario, records and owner-pack document;
- compact source link and full evidence drawer;
- correction, receipt and Undo;
- Local/Hybrid/Cloud settings, offline local model and deterministic fallback;
- responsive desktop/mobile layouts, keyboard focus and reduced motion.

## Superseded reference decision

The earlier Manus-led permanent three-zone layout remains useful research for split interaction, but it is not the final default composition. Daniel explicitly rejected an interface that felt like a workflow engine, permanent dashboard or dark operations console. ChatGPT's proven conversation hierarchy and the Paper Folio interaction system are the final structural authority; the canvas recedes when it is not useful.

No ChatGPT, Paper, Manus, Refero or Brex screenshot, asset, trademark or source code is shipped in Folio.
