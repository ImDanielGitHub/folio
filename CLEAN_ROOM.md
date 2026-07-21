# Clean-room record

## Boundary

Folio is a new standalone repository and product. Its implementation was authored from `BUILD_CONTRACT.md`, synthetic fixtures, public documentation and observed product principles. It is not a reskin, branch or redistributed build of Hermes Agent, Hermes Finance, Bionic, ChatGPT, Manus, Brex or any other product.

The committed demo business, people, accounts, messages, identifiers, documents and finance data are synthetic. Real owner or customer data is prohibited in tests, screenshots and recordings.

## Inspected sources and allowed use

- **Hermes Finance / Hermes Agent:** architecture and MIT-licence boundaries were inspected. General patterns such as connector isolation, model adapters and finance tools could be independently reimplemented. No repository identity, branding, UI, prompts or runtime source was copied into Folio.
- **Bionic:** the installed application bundle and official/public material were inspected clean-room. Only observed principles—model capability discovery, local routing, split conversation/document interaction and resumable agent work—were used. Proprietary code, prompts, assets and minified bundles were not copied or de-minified into Folio.
- **LM Studio:** Folio calls documented loopback HTTP interfaces through an independently authored adapter. No LM Studio application code is included.
- **ChatGPT, Manus and Brex:** used only as interaction/design references. No trademarks, screenshots, source code, private interactions or assets are shipped in the product.
- **Paper and Refero:** internal design/research surfaces. Their reference screenshots remain outside the production bundle.

## Implementation rules

1. Finance amounts and effects come from Folio's deterministic services, never copied output or model prose.
2. Models receive closed schemas and cannot emit executable frontend or backend code into the runtime.
3. Third-party packages remain ordinary declared dependencies under their own licences; no package source is vendored.
4. Reference screenshots may be retained as private research evidence but are not distributable product assets.
5. Credentials, raw bot updates, real account identifiers and customer documents never enter Git.
6. A copied or substantially adapted source file requires an entry in `ATTRIBUTION.md` before landing.
7. Unclear ownership or licence compatibility blocks copying; independent implementation is the default.

## Current result

No copied third-party product source or UI asset has been identified in the integrated Folio tree. `SOURCE_REUSE_MAP.md` records the research inspection and rejected copying boundaries in more detail.
