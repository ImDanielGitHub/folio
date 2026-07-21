# OpenAI Build Week provenance

**Submission period:** 13–21 July 2026 (PDT)
**Repository boundary:** new standalone Git history; first commit 17 July 2026 NZST
**Product:** Folio
**Track:** Work & Productivity (draft; not submitted)

## Baseline

Folio did not exist as a working product before this repository. The first two commits contain the written build contract, contracts, synthetic fixtures, package manifests and bootstrap validation. They do not claim a running finance product.

No Hermes or Bionic product source, branding, prompts, assets, database, minified bundles or repository history was imported. Research into those systems informed independently authored architectural decisions recorded in `SOURCE_REUSE_MAP.md` and `CLEAN_ROOM.md`.

## Dated implementation history

| Commit | Time (NZST) | New work |
|---|---|---|
| `c138aa1` | 17 Jul 19:11 | Build contract and clean standalone boundary |
| `77fc2b8` | 17 Jul 19:12 | Contracts, synthetic fixtures and bootstrap scaffold |
| `b85c90c` | 17 Jul 20:11 | Deterministic Koru finance core, events, Daily Close and artefacts |
| `00e1ee0` | 17 Jul 20:16 | Bounded finance agent harness, adapters, API and connector boundaries |
| `3022be1` | 18 Jul 00:47 | Durable working understanding, local-model resilience, evals and continuity |
| `48984ee` | 18 Jul 00:48 | Chat-first Folio desktop, dynamic canvas, drawers, onboarding and live transport |

The next integration commit records the light Paper-aligned product surface, production-relative Vite assets, temporal/retrieval correction, current documentation and fresh acceptance proof.

## What Codex and GPT-5.6 did

The primary Codex task used GPT-5.6 for:

- broad product and related-work research;
- clean-room analysis of Hermes Finance, LM Studio/Bionic principles and current local-model techniques;
- product specification and adversarial review;
- parallel implementation of the deterministic finance core, agent/API runtime and desktop experience;
- integration recovery after a worker/worktree failure;
- test generation, long-conversation retrieval debugging and local-model adapter repair;
- browser-based visual QA and Devpost draft preparation.

Daniel set the product direction, corrected the scope and interface repeatedly, rejected the early bright/control-panel and dark/permanent-split interpretations, and required the final chat-first light experience with hidden deterministic machinery.

The repository also contains an optional OpenAI Responses API adapter configured for `gpt-5.6`. A live cloud response must be recorded separately before the runtime itself is described as live-verified.

## Proof levels

| Level | Evidence |
|---|---|
| Source | Dated commits and clean standalone history |
| Contract | Twelve JSON Schema examples plus exact fixture arithmetic, IDs, digests and event order |
| Tests | Python suite, Ruff, MyPy and TypeScript checks |
| Build | Electron main/preload TypeScript and Vite production renderer |
| Deterministic runtime | Reset, Daily Close, conversation correction, cash scenario, Undo and owner-pack API flow |
| Harness | Offline malformed-plan and narrative-guard evals |
| Local model | Real LM Studio/Qwen transport smoke; full live four-case run remains a separate optional measurement |
| Visual runtime | Internal-browser desktop/mobile and interaction captures, recorded in `PROTOTYPE_RECEIPT.md` |
| External state | Devpost draft only; no final submission, deployment or public video |

## Submission gates still outside repository proof

- effective public licence or a private repository shared with the judging accounts;
- stable repository URL;
- judge-ready package or hosted test surface;
- public YouTube video under three minutes with voiceover;
- the actual primary `/feedback` Session ID;
- Daniel's legal submitter type and country fields;
- final Devpost review and submit action.

Do not infer any of these from local source or passing tests.
