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
