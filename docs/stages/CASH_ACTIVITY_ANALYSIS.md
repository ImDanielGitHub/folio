# Stage: source-linked cash activity analysis

## Delivered

The real application composition now enriches model context with observed cash
activity over the latest 90 calendar days in the workspace timezone. The model
receives dated aggregate references, business/personal/unresolved outflows,
recorded monthly spending, the largest category outflows and category changes
between the last two observed months. Model language remains checked by the
existing narrative guard. Missing records are not invented as zero-spend months.

A session-authenticated read-only route exposes an explicit inclusive period:

`GET /v1/workspaces/{workspace_id}/analysis?start=2026-07-01&end=2026-07-31`

It returns `cash.analysis@1`, integer NZD minor-unit totals, category and monthly
breakdowns, source evidence IDs, a content-derived input hash and coverage limits.
A request is bounded to 366 days and 20,000 records. Unknown workspaces fail with
404; invalid or oversized periods fail with 422. Inputs are read within one SQLite
snapshot. No finance event, source, classification or job is changed by the read.

Transfers are reported separately from operating inflows/outflows. Pending,
duplicate and ignored records do not enter those totals. Unsupported currency,
non-integer money and duplicate transaction identities fail closed.

## Interpretation boundaries

These are recorded cash movements, not accrual profit, tax estimates or a verified
bank balance. Source completeness remains unknown. Month/category differences
are differences between observed records, not proof of complete-month growth or
causal explanation. Categories reflect current stored classifications.

The API provides the full bounded analysis; the model receives a compact aggregate
projection, not raw bank history. The content hash describes the current analysis
input and is not an independently timestamped immutable archive. User-selected
arbitrary date ranges in chat, specialised analytics surfaces and broader source
coverage/reconciliation remain separate work.

## Verification

Tests cover integer arithmetic above floating-point precision, inclusive dates,
foreign currencies, transfers, pending/duplicate/ignored rows, missing months,
empty periods, category attribution and comparison, input hashes, limits,
workspace isolation, authentication and the actual model explanation request
through the turn endpoint. Synthetic model tests do not prove live inference
quality or acceptance on Daniel's Mac.

## Primary reference

SQLite. (n.d.). *Built-in aggregate functions*. Retrieved September 7, 2026, from
https://www.sqlite.org/lang_aggfunc.html

The implementation deliberately accumulates integer minor units in Python rather
than using floating-point totals. No third-party implementation is copied.
