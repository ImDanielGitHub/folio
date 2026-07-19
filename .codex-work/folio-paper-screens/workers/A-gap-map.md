# Worker A — Folio Paper gap map

**Date:** 2026-07-19  
**Paper theme note:** Existing artboards 01–05 are dark. Target product lock is **light** (`REFERENCE_UI_DECISION.md`). Coordinator must convert 01–05 to light tokens when integrating; do not redesign those compositions here.  
**Stop:** This file is research only. No Paper edits.

---

## Existing coverage (do not rebuild)

| # | Artboard | Size (assumed) | Vision beat covered | Gaps / notes |
|---|---|---|---|---|
| 01 | Conversation — default | Desktop | Chat-first home; one Mitre 10 question | Mid-story question, not “first look after import.” Dark → light. |
| 02 | Owner pack — canvas open | Desktop | Owner pack summoned beside thread | Keep; light conversion. |
| 03 | Evidence — inspection | Desktop | Source-linked evidence; correction partially | Not a full “correction teaches Folio + Undo” beat. |
| 04 | Mobile — dynamic canvas (cash outlook) | Mobile | Mobile canvas; cash outlook | Not the GST + payroll question; not desktop scenario. |
| 05 | Interaction and state contract | Desktop | Spec / contract meta | Not a golden-demo narrative screen. |

---

## Vision beat → status

| Golden / vision beat | Status | Evidence |
|---|---|---|
| First look after import: useful opening + one valuable question | **Missing** | 01 is Mitre follow-up, not post-import open |
| Natural long owner explanation absorbed | **Missing** | No long-answer / Stop / absorbed receipt |
| Live Telegram-style: work phone → Hamilton expense | **Missing** | No Telegram intake surface |
| Daily Close: overdue/upcoming calm (no stage theatre) | **Missing** | No calm close/outlook screen |
| Dynamic scenario: GST and still make payroll? | **Missing** | 04 is generic cash outlook, not this Q |
| Owner pack | **Exists** | 02 |
| Correction teaches Folio | **Partial** | 03 evidence; need Undo + teaching moment |
| Standing policy / autonomy (no approval cards) | **Missing** | — |
| Local / Hybrid / Cloud honesty (no model picker theatre) | **Missing** | — |
| Onboarding | **Missing** | — |

---

## Minimum new set (7 screens)

Prioritized for a continuous golden demo. Numbering continues from 05.

### Priority order for coordinator build

1. **06 · Onboarding — Koru or CSV** — demo entry  
2. **07 · First look — after import** — opens the story  
3. **08 · Long answer — absorbed** — Mitre 10 continuity from 01  
4. **09 · Telegram — work phone** — live intake beat  
5. **10 · Daily Close — calm outlook** — no stage theatre  
6. **11 · Scenario — GST and payroll** — desktop canvas proof  
7. **12 · Privacy — Local Hybrid Cloud** — honesty without picker theatre  

**Deferred (covered enough or secondary):**  
- Standing policy → fold into **07** or **10** as one calm sentence + optional follow-up (see brief below); do not ship a separate approval UI.  
- Correction + Undo teaching → extend **03** or add a thin variant later; not required if 03 already shows supersession + Undo.  
- Mobile GST scenario → 04 already proves mobile canvas; desktop 11 is the story beat.

---

## Screen briefs (build-ready)

### 06 · Onboarding — Koru or CSV

- **Size:** Desktop 1440×900  
- **Purpose:** One-step start: choose the Koru Studio synthetic demo or import a local CSV—no account wall, no model tour.  
- **Canvas:** Closed (full conversation/onboarding column).  
- **Copy blocks:**
  - **Headline:** Welcome to Folio  
  - **Body:** Folio is a local-first finance operator. Start with the Koru Studio demo, or bring your own bank CSV. Your numbers stay on this machine unless you choose otherwise.  
  - **Primary CTA:** Open Koru Studio demo  
  - **Secondary CTA:** Import a local CSV  
  - **Quiet footer:** No account required · Privacy & Models later  
- **Must NOT appear:** Stage progress (3/7), model picker, evidence wall, dark ops chrome, bank OAuth, Telegram connect, KPI dashboard.

---

### 07 · First look — after import

- **Size:** Desktop 1440×900  
- **Purpose:** First useful answer after ingest/Daily Close: practical meaning of the books, then exactly one highest-value question.  
- **Canvas:** Closed.  
- **Copy blocks:**
  - **Folio opening (assistant):** Koru Studio is loaded. Cash looks steady for the next few weeks, with two items worth a quiet look: a likely duplicate near Mitre 10, and GST due before the next big payroll.  
  - **Soft source line:** Based on 1 linked import · Daily Close complete  
  - **One question:** Was the Mitre 10 charge for studio materials, or something else?  
  - **Composer placeholder:** Tell Folio in your own words…  
- **Standing-policy seed (one line in body or a second short bubble):** I’ll keep treating hardware-store purchases as studio materials unless you say otherwise—no approval cards, just a note you can change.  
- **Must NOT appear:** “Stage 7/10”, workflow engine, open evidence browser, model name badges, approval queue, dark theme, permanent split canvas.

---

### 08 · Long answer — absorbed

- **Size:** Desktop 1440×900  
- **Purpose:** Owner explains Mitre 10 at natural length; Folio absorbs the full statement, applies a narrow correction, and shows Undo—proving working understanding without forms.  
- **Canvas:** Closed (optional compact “Updated · Mitre 10” chip only).  
- **Copy blocks:**
  - **Owner (long, left-aligned):** Yeah that Mitre 10 one was materials for the Hamilton fit-out—timber, fixings, a bit of paint. Not personal. We’ve been using that store for job stock for months, so treat those as studio costs unless I say it’s tools for home. The duplicate looking one is probably the card catching twice; the real spend is the materials line.  
  - **Folio:** Got it. I’ve kept your full explanation, marked the Mitre 10 line as studio materials for the Hamilton fit-out, and parked the likely duplicate so it won’t keep asking. Previous guess is still in history if you need it.  
  - **Receipt line:** Updated · Mitre 10 · Undo  
  - **Composer:** continues empty / calm  
- **Must NOT appear:** Multi-field correction form, approval cards, stage theatre, evidence wall by default, dark theme, “AI reclassified with 94% confidence” theatre.

---

### 09 · Telegram — work phone

- **Size:** Mobile 390×844 *(or desktop 1440×900 with a phone-framed intake in the thread—prefer true mobile artboard)*  
- **Purpose:** Telegram-shaped live intake: owner messages a real expense; Folio acknowledges in chat language and files it to Hamilton without a bot settings tour.  
- **Canvas:** Closed.  
- **Copy blocks:**
  - **Context chip:** Via Telegram · just now  
  - **Owner message:** bought a work phone, expense to Hamilton  
  - **Folio:** Logged as a Hamilton job expense—work phone. I’ll keep it with that job unless you correct it.  
  - **Quiet detail (one line):** $1,299 · awaiting bank match if needed  
  - **Composer / reply field:** Message Folio…  
- **Must NOT appear:** Real Telegram branding lockup as product chrome, bot token UI, approval cards, model picker, evidence wall, dark ops console, stage counter.

---

### 10 · Daily Close — calm outlook

- **Size:** Desktop 1440×900  
- **Purpose:** Daily Close results as a calm conversation: what’s overdue and what’s coming up—no run-stage theatre.  
- **Canvas:** Closed (optional one “Show cash outlook” text action that implies 11/04).  
- **Copy blocks:**
  - **Headline / opening:** Daily Close is done.  
  - **Body:** Nothing urgent is broken. GST is due in 12 days, and payroll hits before that—so the next fortnight is the real constraint, not today. One unsupported expense is still waiting on a short note from you; everything else reconciled cleanly.  
  - **Overdue / upcoming (plain list in prose or two soft lines):**  
    - Upcoming — GST · 12 days  
    - Upcoming — Payroll · before GST  
    - Waiting on you — unsupported expense (one line)  
  - **One question:** Want the cash view for GST and payroll together?  
  - **Composer:** Ask Folio…  
- **Must NOT appear:** “Stage 7/10”, progress stepper, agent swarm, tool-call logs, model picker, evidence wall, dark theme, celebratory confetti dashboard.

---

### 11 · Scenario — GST and payroll

- **Size:** Desktop 1440×900  
- **Purpose:** Owner asks whether they can afford GST and still make payroll; canvas opens with a living cash scenario beside the same thread.  
- **Canvas:** Open (scenario surface: timeline/bars + key dates; not a KPI card wall).  
- **Copy blocks:**
  - **Owner:** Can we afford GST and still make payroll?  
  - **Folio:** Yes—if nothing large and unexpected lands first. After payroll and GST, the 30-day outlook still clears with a modest buffer. The tight week is the one where both hit close together.  
  - **Soft source:** Based on N linked commitments · scenario, not a promise  
  - **Canvas title:** Cash outlook · next 30 days  
  - **Canvas anchors (labels only):** Today · Payroll · GST · Buffer remaining  
  - **Composer:** stays under the thread  
- **Must NOT appear:** Dark theme, permanent three-zone Manus dashboard, model picker, evidence wall by default, stage theatre, “predictive AI forecast” claims, approval cards.

---

### 12 · Privacy — Local Hybrid Cloud

- **Size:** Desktop 1440×900  
- **Purpose:** Quiet Privacy & Models drawer: Local / Hybrid / Cloud honesty—routing modes, not a model marketplace.  
- **Canvas:** Closed; drawer over conversation (rail “Privacy” active).  
- **Copy blocks:**
  - **Drawer title:** Privacy & Models  
  - **Intro:** Finance truth stays local. These modes only change where planning and explanation may run.  
  - **Local:** On this machine via LM Studio. If the local model isn’t available, Folio says so and uses a bounded fallback—it will not silently call the cloud.  
  - **Hybrid:** Numbers and effects stay local. Only a typed projection may go to the cloud for planning help.  
  - **Cloud:** Cloud may draft plans and explanations; deterministic finance still owns amounts and effects.  
  - **Status example (Local selected):** Local · ready — or Local · unavailable · using deterministic fallback  
  - **Close:** Done  
- **Must NOT appear:** Model picker theatre (lists of GPT/Claude badges as the hero), API key form in the main path, telemetry toggles as primary, dark ops console, evidence wall, approval cards, stage progress.

---

## Golden demo path (using new + existing)

```text
06 Onboarding
  → 07 First look (import + one question + policy seed)
  → 01 Conversation default (Mitre question — existing; lighten)
  → 08 Long answer absorbed (+ Undo)
  → 09 Telegram work phone
  → 10 Daily Close calm
  → 11 Scenario GST + payroll (canvas open)
  → 02 Owner pack (existing; lighten)
  → 03 Evidence (existing; lighten) / optional Undo beat
  → 12 Privacy Local · Hybrid · Cloud
04 Mobile cash outlook = responsive proof (existing; lighten)
05 Interaction contract = internal spec (keep; not demo path)
```

---

## Coordinator checklist

- [ ] Build 06–12 in light tokens only  
- [ ] Convert 01–05 from dark → light without changing accepted layouts  
- [ ] Sort artboards non-overlapping; names exact as above  
- [ ] No stage N/M, no model picker hero, no permanent evidence wall, no dark default  
- [ ] Standing policy appears as calm prose in 07 (and optionally restated in 10)—never as approval cards  

---

## Return summary

**File:** `/Users/dananeke/Documents/Finace App/.codex-work/folio-paper-screens/workers/A-gap-map.md`

**Prioritized missing screens:**
1. 06 · Onboarding — Koru or CSV (1440×900)  
2. 07 · First look — after import (1440×900)  
3. 08 · Long answer — absorbed (1440×900)  
4. 09 · Telegram — work phone (390×844)  
5. 10 · Daily Close — calm outlook (1440×900)  
6. 11 · Scenario — GST and payroll (1440×900, canvas open)  
7. 12 · Privacy — Local Hybrid Cloud (1440×900, drawer)  
