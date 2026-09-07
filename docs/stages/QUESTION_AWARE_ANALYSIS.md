# Stage: question-aware model explanations

## Implemented boundary

Analytical questions grant read authority, not correction or Undo authority.
Explicit commands retain their bounded action catalogue. Stop means an explicit
stop command, not a question about stopping overspending. Unrelated transaction
reads do not inherit the last unresolved merchant.

A local explanation receives the current question, recent turns, active claims
and retrieved working understanding. They are marked untrusted context, separate
from checked financial references. Long questions retain the opening and material
tail. Source statements remain stored in full by the existing conversation layer.

A valid read plan can receive a model explanation even when the model failed the
structured planning step. Writes still use deterministic commit receipts. The
existing number, provenance and action-claim validation remains in force.

Cloud mode includes a bounded owner question only through the existing allowed
owner-claims projection. Hybrid explanation policy still excludes owner claims;
no local transcript or retrieved memory is silently added to either cloud prompt.
No new provider, credential, payment, tax filing or accounting write is enabled.

## Evidence and limits

Tests cover question-versus-command authority, unrelated merchant filters,
head/tail preservation, local context delivery, cloud isolation and an actual
SQLite-backed turn whose invalid plan is followed by a validated model answer.
A restart test proves that older retrieved owner context reaches the explanation
request after it has left the recent-turn window.

These are protocol and integration tests with a synthetic model. They do not
measure real model quality. Richer period/category analysis, actual token-based
context budgeting, durable run cancellation and native Mac acceptance remain
separate work. This stage does not mark the complete 200-item audit as done.

## Primary references (APA style)

Nous Research. (n.d.). *Context compression and caching*. Retrieved September 7,
2026, from https://hermes-agent.nousresearch.com/docs/developer-guide/context-compression-and-caching/

T3 Code contributors. (n.d.). *Architecture*. Retrieved September 7, 2026, from
https://github.com/pingdotgg/t3code/blob/main/docs/internals/overview.md

The relevant patterns are protected recent user context, durable retrievable
history, provider-specific adapters and server-owned state. Implementation is
original Folio code; no third-party source code is copied.
