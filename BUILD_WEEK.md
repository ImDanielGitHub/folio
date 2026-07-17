# OpenAI Build Week boundary record

**Boundary date:** 17 July 2026, Pacific/Auckland  
**Repository at bootstrap start:** new Git repository on `main`, no commits, with only `BUILD_CONTRACT.md` untracked  
**Product identity:** Standalone Finance Agent (working product name), a new repository and user-facing product

## Baseline

`BUILD_CONTRACT.md` is product and architecture planning. The coordinator bootstrap commit creates contracts, synthetic fixtures, empty lane boundaries, manifests and validation tooling. Neither document is runtime proof that the product loop works.

No earlier product code, branding, proprietary prompts, assets or compiled output is part of this repository baseline. The new implementation must remain auditable as commits made after this boundary.

## Post-boundary evidence to record

For every implementation lane and the final integration, record:

- exact commit SHA and parent bootstrap SHA;
- files owned and changed;
- test/build command and full pass/fail result;
- local runtime evidence, separately from source and test evidence;
- normalised golden-demo hashes and any intentionally variable fields;
- local-model capability/model identifier and result;
- cloud provider/model and egress receipt if credentials and access are available;
- Telegram fixture proof, and separately any optional real test-bot round trip.

The final submission must distinguish planning, bootstrap/config, implementation, tests, local runtime and any external provider state. Five clean deterministic demo runs and the recorded vertical slice remain later integration gates.

## Eligibility gate

Build Week rules and submission eligibility are time-sensitive external facts. They must be rechecked from the current official source before submission. This file records provenance and proof boundaries; it does not assert eligibility or submission acceptance.
