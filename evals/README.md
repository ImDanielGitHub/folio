# Offline harness evaluation

Run from the repository root with the locked API environment:

```bash
uv run --project services/api python evals/run_offline_harness.py
```

The fixed corpus compares raw `json.loads` plus canonical validation against the
bounded recovery parser. It covers markdown fences, result/arguments wrappers,
thinking prefixes, trailing commas, prompt injection embedded as data and an
unknown action that must remain rejected. This is an offline parser/harness metric,
not a live-model capability claim.

## Narrative safety guard

```bash
PYTHONPATH=services/api/src \
  services/api/.venv/bin/python evals/run_narrative_guard.py
```

The fixed adversarial corpus checks that model-written finance prose cannot invent
amounts, bind a real amount to the wrong label, claim unsupported paid/sent/filed work,
invent provenance, expose internal identifiers, or issue tax, investment, or payment
directives. It also measures whether grounded conversational prose is preserved. The
result in `evals/results/narrative-guard.json` is deterministic guard evidence, not a
claim about any particular local or cloud model.
