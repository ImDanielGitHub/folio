# Clean-room boundary

## Scope

This repository is a standalone product created from the written behaviour and architecture in `BUILD_CONTRACT.md`. The bootstrap contains no imported product source, routes, brand assets, proprietary prompts, database files, generated bundles or minified code from any prior finance product.

The demo business, people, accounts, messages, identifiers and financial data are synthetic. They must never be replaced with Daniel's or a customer's real financial data for tests, screenshots or recordings.

## Rules for later implementation

1. Implement from the canonical contracts and synthetic fixtures in this repository.
2. Do not copy branding, assets, prompts, minified code, private data or repository identity from Hermes, Bionic or any other product.
3. Licence-compatible third-party patterns may be adapted only when the implementation records the source, licence, files and nature of the adaptation in `ATTRIBUTION.md`.
4. Design references may guide layout principles, but reference screenshots and assets are not production assets unless their licence and intended use are verified.
5. Do not place credentials, real bot updates, real account identifiers, customer documents or source-system exports in Git.
6. Preserve the proof boundary: copied or inherited foundations cannot be described as new Build Week implementation.

Any uncertainty about ownership or licence compatibility is a stop gate for copying. Reimplementing a general idea from the written contract is the default.
