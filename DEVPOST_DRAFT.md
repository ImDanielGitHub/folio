# Devpost working draft — not submitted

**Project ID:** `1328264`
**Category:** Work & Productivity
**State at preparation:** `submission_pre_draft`

## Name

Folio

## Tagline

An always-ready local finance operator that closes the books, remembers the business, and prepares evidence-backed work on local or cloud models.

## Description

Folio is a local-first finance operator for sole traders and small businesses. Instead of making the owner navigate another accounting dashboard, it begins as one calm conversation. Folio continuously maintains a source-linked picture of who and what matters to the business, then opens a chart, transaction, evidence view or prepared document only when the current question needs it.

The Build Week prototype runs a complete synthetic workflow for Koru Studio. It ingests a bank CSV, performs an idempotent Daily Close, detects a likely duplicate and an unsupported expense, calculates a 30-day cash scenario, and prepares an evidence-linked owner pack. The owner can explain a transaction naturally, see Folio update its understanding, and undo the resulting classification change without losing the audit history.

Folio is designed to stay useful with smaller local models. A bounded harness discovers model capabilities, exposes only relevant closed-schema finance actions, repairs common structured-output failures, validates every action and falls back to deterministic planning when a model fails. Finance amounts, affected transactions, forecasts and generated documents are always produced by deterministic local services—not model prose. LM Studio runs entirely over loopback; optional Hybrid and Cloud modes use the same provider-independent conversation and memory, and Folio never silently changes route.

Its “working understanding” is model-independent. Full owner statements are immutable, structured facts keep source provenance and temporal state, corrections supersede rather than erase history, and task-specific retrieval assembles the right current picture even after restarts, long conversations and model switches.

The interface hides that complexity. The default dark desktop view is a continuing conversation with a calm, low-glare finance palette. A finance/document canvas appears only for a scenario, table, transaction, source or owner pack, then recedes. Evidence is available from compact source links; raw tool and model diagnostics stay in an audit surface.

Codex with GPT-5.6 was used for product research, architecture, parallel implementation, test generation, integration recovery, debugging and adversarial review. The repository's dated commits separate the initial contracts from the new deterministic finance core, agent runtime, durable memory and chat-first desktop. A thin GPT-5.6 Responses API adapter is included as an optional cloud route; the local demo needs no cloud credential.

## Built with

- Codex
- GPT-5.6
- Electron
- React
- TypeScript
- Vite
- Python
- FastAPI
- Pydantic
- SQLite
- LM Studio

## Do not add until proven

- repository URL and effective licence;
- judge test link or packaged build;
- public video URL;
- `/feedback` Session ID;
- legal submitter type/country;
- claims of a real Telegram bot, live bank connector, deployment or live GPT-5.6 runtime response.
