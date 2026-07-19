# Worker F — Folio state screens

**Date:** 2026-07-20  
**Paper file:** Folio — Conversation-first Product System (`01KXQWFD5CAYZWBV88B3WNFZCA`)  
**Theme:** Dark only · Folio tokens · Instrument Sans + IBM Plex Mono  
**Locked:** 01–05 untouched (`1-0`, `2-0`, `3-0`, `88-0`, `9Q-0`)

---

## Built

| # | Artboard | Node ID | Source clone | World position | Status |
|---|---|---|---|---|---|
| 22 | Working — streaming Stop | `19P-0` | 14 Long conversation (`ME-0`) | x=-720, y=4800 | **Done** |
| 23 | Local unavailable — fallback | `1C7-0` | 01 Conversation (`1-0`) | x=800, y=4800 | **Done** |
| 24 | After Undo — restored | `1DL-0` | 08 Long answer (`CS-0`) | x=2320, y=4800 | **Done** |
| 25 | CSV import — ingesting | `1EZ-0` | 07 First look (`BE-0`) | x=3840, y=4800 | **Done** |

Row leaves y≈2800–3800 free for other workers. Step 1520 on x from -720.

---

## Screen notes

### 22 · Working — streaming Stop
- Thread: Daily Close context → GST/payroll question → Folio mid-draft (trailing em dash)
- Header status: `Planning locally · Qwen 3.5 9B · no cloud` (accent pulse, IBM Plex Mono)
- Inline cue: `Drafting · not sending to cloud`
- Composer replaced with paused state + accent **Stop** control
- No stage theatre / progress N/M

### 23 · Local unavailable — fallback
- Header chip: `Local · unavailable · fallback` (warning pulse)
- Folio honesty: deterministic fallback; will not silently call cloud
- Provenance line: `Local model unavailable · using deterministic fallback`
- Soft CTAs: Open LM Studio · Retry local
- Composer still usable with fallback placeholder

### 24 · After Undo — restored
- Owner asks to undo Mitre correction
- Compact receipt: `Undone · Mitre 10 · previous guess restored`
- Folio confirms classification + owner pack totals restored
- Calm pill: `Classification restored · totals matched` (positive, not error UI)

### 25 · CSV import — ingesting
- Between onboarding and first look
- Progress: `Reading ANZ export · 48 rows · dedupe checks`
- Calm progress card (bar, not stage N/M): Deduping near Mitre 10 · local only
- Composer quiet: `Folio is importing…` (attach/send dimmed)
- No bank OAuth / Akahu

---

## QA

- [x] Dark Folio tokens only
- [x] 01–05 not edited
- [x] Screenshots taken for 22–25
- [x] `finish_working_on_nodes` called
- [x] No stage theatre on 22 / 25
- [x] Privacy honesty on 23
- [x] Calm Undo restore on 24
