# Worker C — Light conversion + clone QA rubric

**Date:** 2026-07-19  
**Paper file:** Folio — Conversation-first Product System  
**Authority:** `REFERENCE_UI_DECISION.md` (light, chat-first) > Paper screenshots > running app  
**Stop:** Research / QA only. No Paper edits. No code edits.

---

## Snapshot at audit time

| Check | Result |
|---|---|
| Design tokens in Paper | Already **light** (ground `#F4F4F1`, surface `#FBFBF9`, text `#181816`, accent `#C96642`) |
| Hardcoded dark leftovers | **Fail open:** Evidence body still `#161716` (literal) |
| Artboard 05 ground | Uses literal `#F2EFE9` (old cream *text* hex as page wash — remap to `--color-ground`) |
| `localhost:4173` | **Not running** (HTTP 000) — browser QA is fixture/optional until preview is up |
| App vs Paper | Desktop app still Manus permanent-split — expect mismatch; do not fail Paper for matching app |

---

## 1. Exact CSS token replacement map

### 1.1 Core dark → light (must)

Use Paper `set_tokens` to update existing names. Use `create_tokens` only for missing light-only roles (`--color-rail`, `--color-canvas`, `--color-warning`, `--color-negative`).

| Role | Old dark (superseded) | New light (lock) | Paper token name | Recovered `:root` alias |
|---|---|---|---|---|
| Page ground | `#0D0E0E` | `#F4F4F1` | `--color-ground` | `--bg` |
| Thread / conversation surface | `#161716` | `#FBFBF9` | `--color-surface` | `--thread` |
| Raised panel / card / composer shell | *(often same as surface or `#1C1D1C`)* | `#FFFFFF` | `--color-raised` | `--panel` |
| Divider / hairline | *(dark ~`#2A2B29` / low-contrast)* | `#E3E2DC` | `--color-line` | *(none — keep Paper name)* |
| Primary ink | `#F2EFE9` | `#181816` | `--color-text` | `--text` / ink |
| Secondary / muted | *(light-muted on dark ~`#9A968C`)* | `#68665F` | `--color-muted` | `--muted` |
| Soft body (optional) | *(n/a or muted)* | `#41403B` | *create if needed:* `--color-text-soft` | `--text-soft` |
| Accent (copper) | `#D98558` | `#C96642` | `--color-accent` | `--accent` |
| Positive | *(bright green on dark)* | `#397756` | `--color-positive` | `--positive` |
| Warning | *(amber on dark)* | `#9B6B27` | `--color-warning` | `--warning` |
| Negative | *(red on dark)* | `#A5483D` | `--color-negative` | `--negative` |
| Rail | *(= ground or slightly lifted dark)* | `#EFEFEB` | `--color-rail` | `--rail` |
| Summoned canvas ground | *(dark panel)* | `#F1F1EE` | `--color-canvas` | `--canvas-bg` |

**Fonts (keep):**

| Token | Value | Notes |
|---|---|---|
| `--font-sans` | `Instrument Sans` | OK — do not switch to Inter/Roboto/system as brand |
| `--font-mono` | `IBM Plex Mono` | Captions, amounts, IDs only |

**Type scale (unchanged unless broken):**

`--text-caption` 12px · `--text-body` 14px · `--text-reading` 16px · `--text-title` 24px · `--text-display` 32px  
`--font-weight-regular` 400 · `--font-weight-medium` 500 · `--font-weight-semibold` 600  
`--tracking-tight` -0.025em · `--leading-tight` 32px · `--leading-body` 23px  
`--radius-control` 8px · `--radius-panel` 14px

### 1.2 Executable Paper MCP payload

**A. Update existing color tokens (`set_tokens`):**

```json
{
  "tokens": [
    { "name": "--color-ground", "value": "#F4F4F1", "description": "Light chalky mineral page ground" },
    { "name": "--color-surface", "value": "#FBFBF9", "description": "Conversation / thread surface" },
    { "name": "--color-raised", "value": "#FFFFFF", "description": "Raised panels and composer" },
    { "name": "--color-line", "value": "#E3E2DC", "description": "Quiet dividers" },
    { "name": "--color-text", "value": "#181816", "description": "Primary ink" },
    { "name": "--color-muted", "value": "#68665F", "description": "Secondary text" },
    { "name": "--color-positive", "value": "#397756", "description": "Healthy / complete" },
    { "name": "--color-accent", "value": "#C96642", "description": "Copper brand / primary action" }
  ]
}
```

**B. Create missing light roles if absent (`create_tokens`):**

```json
{
  "tokens": [
    { "type": "color", "name": "--color-rail", "value": "#EFEFEB", "description": "Narrow app rail" },
    { "type": "color", "name": "--color-canvas", "value": "#F1F1EE", "description": "Summoned finance canvas ground" },
    { "type": "color", "name": "--color-warning", "value": "#9B6B27", "description": "Caution / headroom warning" },
    { "type": "color", "name": "--color-negative", "value": "#A5483D", "description": "Degraded / error" },
    { "type": "color", "name": "--color-text-soft", "value": "#41403B", "description": "Soft body ink between text and muted" }
  ]
}
```

**C. Literal hex sweep after token update (must pass):**

Search with Paper `find_nodes` / style filters for each leftover. **Reject** if any remain on product UI (composer intentional dark bar is the only allowed near-black — see §4).

| Forbidden literal | Remap to |
|---|---|
| `#0D0E0E` | `var(--color-ground)` |
| `#161716` | `var(--color-surface)` or `var(--color-raised)` |
| `#1C1D1C` / `#121312` / `#0A0B0B` | light ground/surface/raised as appropriate |
| `#F2EFE9` on **text** | already wrong on light — use `var(--color-text)` |
| `#F2EFE9` as **background** | `var(--color-ground)` (`#F4F4F1`) |
| `#D98558` | `var(--color-accent)` (`#C96642`) |
| `#9A968C` (old light-muted) | `var(--color-muted)` |

**Known residual at audit:** Evidence body = literal `#161716` → must become `var(--color-raised)` or `var(--color-surface)`.

### 1.3 App CSS alias map (for later code sync — not this worker)

When desktop eventually follows Paper:

```css
:root {
  color-scheme: light;
  --bg: var(--color-ground, #f4f4f1);
  --rail: var(--color-rail, #efefeb);
  --thread: var(--color-surface, #fbfbf9);
  --canvas-bg: var(--color-canvas, #f1f1ee);
  --panel: var(--color-raised, #ffffff);
  --text: var(--color-text, #181816);
  --text-soft: var(--color-text-soft, #41403b);
  --muted: var(--color-muted, #68665f);
  --accent: var(--color-accent, #c96642);
  --positive: var(--color-positive, #397756);
  --warning: var(--color-warning, #9b6b27);
  --negative: var(--color-negative, #a5483d);
}
```

---

## 2. Per-artboard QA checklist (01–05 after light conversion)

Run after token update + literal sweep. For each artboard: `get_screenshot` → score Pass / Fail. Fix before declaring light conversion done.

### Shared gates (all five)

- [ ] **Light default:** Page reads chalky mineral / bone, not ops-console charcoal.
- [ ] **Ink contrast:** Primary text `#181816` on light surfaces; no cream-on-charcoal leftovers.
- [ ] **Copper only once:** Accent `#C96642` for brand mark, primary send, sparse links — not purple/indigo AI glow.
- [ ] **Fonts:** Instrument Sans for prose; IBM Plex Mono only for mono captions/amounts.
- [ ] **Chat-first:** Conversation is the dominant reading plane; finance chrome does not compete as a permanent dashboard.
- [ ] **No theatre:** No stage N/M, model picker hero, agent swarm, tool-call wall, approval queue cards.
- [ ] **Token hygiene:** Surfaces use `var(--color-*)`, not stray dark hex (except intentional composer — §4).
- [ ] **Spacing / alignment:** Rail icons share a vertical lane; thread column calm and left-aligned.

---

### 01 · Conversation — default (1440×900)

**Intent:** Chat-only home. Narrow rail + centred reading column. Canvas closed.

| # | Check | Pass if |
|---|---|---|
| 01.1 | Composition | ~68px rail + full-width thread; **no** permanent right canvas |
| 01.2 | Thread ground | `--color-surface` / warm off-white, not dark `#161716` |
| 01.3 | Hero answer | Large ink headline + short practical body; one Mitre 10 question card |
| 01.4 | Sources | Compact “Based on N …” line with copper spark — not evidence wall |
| 01.5 | Composer | Calm fixed bottom; Attach + send; may be dark pill on light page (allowed) |
| 01.6 | Status | “Working locally” / local honesty — no model name badges |
| 01.7 | Reject if | KPI strip, three-zone Manus split, dark full-bleed chrome, approval cards |

---

### 02 · Owner pack — canvas open (1440×900)

**Intent:** Same thread continues; canvas summoned beside it with July owner pack.

| # | Check | Pass if |
|---|---|---|
| 02.1 | Split is **dynamic** | Thread still present (~520px); canvas is the summoned pack, not a permanent nav destination |
| 02.2 | Canvas ground | `--color-canvas` `#F1F1EE` or raised white document — not dark ops panel |
| 02.3 | Document | Owner pack reads as a calm document (cash outlook, figures, obligations) |
| 02.4 | Thread continuity | Composer + Koru conversation remain; user did not “navigate away” |
| 02.5 | Accent restraint | Copper for PDF ready / F mark — not neon chart chrome |
| 02.6 | Reject if | Canvas looks like a permanent analytics dashboard; dark document body; stage progress |

---

### 03 · Evidence — inspection (1440×900)

**Intent:** Focused evidence drawer / inspection from a source link; correction + Undo without approval theatre.

| # | Check | Pass if |
|---|---|---|
| 03.1 | Light shell | Conversation context + drawer on light grounds |
| 03.2 | **Evidence body** | **Not** `#161716` — use raised/surface light panel |
| 03.3 | Evidence content | Source name, human description, excerpt/preview — IDs nested |
| 03.4 | Correction | Supersession / Undo visible as quiet action — not multi-field approval form |
| 03.5 | Hierarchy | Drawer is focused overlay/side panel, not a permanent evidence browser wall |
| 03.6 | Reject if | Dark inspection console; “Approve / Reject” cards; model confidence theatre; tool traces as hero |

---

### 04 · Mobile — dynamic canvas (390×844)

**Intent:** Narrow: Thread vs Finance as clear panes/tabs; cash outlook canvas.

| # | Check | Pass if |
|---|---|---|
| 04.1 | Light mobile chrome | Status bar + light grounds throughout |
| 04.2 | Pane clarity | Thread and Finance are distinct; no squeezed three-column desktop |
| 04.3 | Chart / figures | Readable ink on light canvas; copper sparingly |
| 04.4 | Ask about this | Contextual CTA, not a dashboard FAB cluster |
| 04.5 | Reject if | Dark mobile ops theme; permanent KPI card stack; Hermes branding |

---

### 05 · Interaction and state contract (1440×900)

**Intent:** Spec / contract meta artboard documenting states — still must be light and on-brand.

| # | Check | Pass if |
|---|---|---|
| 05.1 | Page wash | `var(--color-ground)` `#F4F4F1` — not literal `#F2EFE9` (old text cream) |
| 05.2 | Spec cards | Light raised panels; ink hierarchy clear |
| 05.3 | Content truth | Documents chat-only / canvas open / evidence / undo — not stage engine |
| 05.4 | Reject if | Reads as dark design-system dump; contradicts light product lock |

---

## 3. Browser verification steps

**Primary truth:** Paper screenshots vs vision lock.  
**Secondary:** Running app at `http://localhost:4173` **if available**.  
**At audit:** preview **down** — skip live steps or use fixture mode; do not block Paper QA.

### 3.1 Prep

1. Confirm Paper artboards 01–05 pass §2 (screenshots).
2. Probe preview: `curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:4173/`  
   - `200` → continue  
   - fail → mark “Browser QA: skipped (preview down)” and stop this section
3. **Fixture mode (preferred):** Open `http://127.0.0.1:4173` — renderer falls back to sealed Koru Studio fixtures when the local API is down (`README.md`; `apps/desktop/src/fixtures.ts`, `fixtures/ui/`). No live bank/Telegram.
4. **Expected app shell mismatch:** Desktop CSS still uses permanent Manus split — `grid-template-columns: 48px var(--thread-width) 1px minmax(0, 1fr)` in `apps/desktop/src/styles.css`. No `--bg` / `#f4f4f1` light lock found in app styles. Note honestly; do not fail Paper for matching app.

### 3.2 What to look for (vision first)

Open each Paper screenshot beside the browser window. Score:

| Dimension | Look for in browser | Paper is authority when… |
|---|---|---|
| Theme | Light chalky ground, dark ink, copper accent | App is still dark or purple → note mismatch, **do not** restyle Paper to match |
| Composition | Chat column default; canvas only when summoned | App shows permanent Manus three-zone → **expected mismatch**; file as app debt |
| Rail | Narrow infrequent entry points | App rail is a full nav/dashboard → mismatch |
| Evidence | On-demand drawer | App shows permanent evidence browser → mismatch |
| Theatre | Absent | Stage counters / model pickers / approval queues → reject **app**, not Paper |

### 3.3 Suggested browser walk (when up)

1. **Default home** ↔ Paper 01  
   - Expect: conversation-first.  
   - Honest note if app still split: “App ≠ Paper; Paper wins.”
2. **Summon owner pack / canvas** ↔ Paper 02  
   - Expect: thread remains; canvas opens. Fail app if canvas is always open with no close.
3. **Open source / evidence** ↔ Paper 03  
   - Expect: focused inspection + Undo path. Fail if approval theatre.
4. **Narrow viewport (~390)** ↔ Paper 04  
   - Expect: pane/tab switch, not crushed desktop.
5. **Ignore Paper 05 in browser** unless a dedicated contract/docs route exists — it is a design-spec artboard.

### 3.4 Clone-quality bar (Paper → vision)

Pass only if a stranger would describe the product as:

> “A clean light chat that already understands the business, and occasionally opens a finance document beside the thread.”

Fail if they say: dashboard, ops console, workflow engine, Hermes, or approval queue.

---

## 4. Failure modes — when to reject a screen

Reject (do not ship / do not mark light conversion done) when **any** apply:

### Still “dark”

- Majority of viewport is charcoal (`#0D0E0E` / `#161716` family) as **page** or **thread** ground.
- Cream text (`#F2EFE9`) used as primary ink on dark panels that were supposed to convert.
- Evidence / owner-pack bodies left on dark literals (e.g. Evidence body `#161716`).
- Exception: **one** dark composer pill on an otherwise light page is allowed if it matches Paper 01.

### Still “dashboard”

- Permanent KPI card wall, stat strips, or multi-widget home.
- Finance controls compete with conversation in the default state.
- Canvas always open with no “chat-only” resting state.
- Looks like analytics / Manus permanent three-zone as the **default**.

### Still “Hermes”

- Hermes naming, branding, or desktop-theme chrome as the Folio product surface.
- Reskin of another finance shell instead of Folio conversation hierarchy.

### Still “approval theatre”

- Approve / Reject card stacks as the primary response pattern.
- Stage N/M steppers, agent swarm, tool-call logs, or model picker as hero UI.
- Confidence percentages / “AI reclassified with 94%” as the main story.
- Multi-field correction forms that feel like a workflow engine (quiet Undo is OK).

### Token / craft failures

- Accent drift to purple, indigo glow, or neon SaaS gradients.
- Inter/Roboto/Arial as display brand (Instrument Sans required).
- Low-contrast muted-on-muted body copy after light flip.
- Artboards overlapping or unsorted on the canvas (coordinator gate).

---

## 5. Pass / fail scorecard (copy for coordinator)

| Artboard | Light tokens | No dark literals | Chat-first composition | No theatre | Browser vs Paper | Verdict |
|---|---|---|---|---|---|---|
| 01 Conversation | ☐ | ☐ | ☐ | ☐ | ☐ / skip | ☐ Pass ☐ Fail |
| 02 Owner pack | ☐ | ☐ | ☐ | ☐ | ☐ / skip | ☐ Pass ☐ Fail |
| 03 Evidence | ☐ | ☐ `#161716` body | ☐ | ☐ | ☐ / skip | ☐ Pass ☐ Fail |
| 04 Mobile | ☐ | ☐ | ☐ | ☐ | ☐ / skip | ☐ Pass ☐ Fail |
| 05 Contract | ☐ | ☐ `#F2EFE9` bg | ☐ | ☐ | n/a | ☐ Pass ☐ Fail |

**Conversion done only when:** all five Pass on Paper columns; browser column is Pass **or** explicit Skip with mismatch notes.

---

## Token map summary (quick)

| Old dark | New light | Token |
|---|---|---|
| `#0D0E0E` | `#F4F4F1` | `--color-ground` / `--bg` |
| `#161716` | `#FBFBF9` | `--color-surface` / `--thread` |
| *(raised dark)* | `#FFFFFF` | `--color-raised` / `--panel` |
| `#F2EFE9` (ink) | `#181816` | `--color-text` / `--text` |
| `#D98558` | `#C96642` | `--color-accent` / `--accent` |
| *(add)* | `#EFEFEB` | `--color-rail` / `--rail` |
| *(add)* | `#F1F1EE` | `--color-canvas` / `--canvas-bg` |
| fonts | Instrument Sans + IBM Plex Mono | keep |

---

**Deliverable path:** `.codex-work/folio-paper-screens/workers/C-light-qa-rubric.md`
