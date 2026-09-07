# Stage: local-model recovery

## Scope

Recover usable local inference without relaxing finance or privacy authority.
Prefer a loaded language model, never an embedding model; respect an explicit
selection; budget against the selected instance, not its theoretical capacity.
Use the compatible inventory endpoint only when the native route is unsupported.
Read the configured token and timeout, but never expose either in diagnostics.
Never treat free-form reasoning or truncated output as a final answer.

## Verification sequence

1. Reproduce loaded-model selection, active-context, authentication, compatibility
   inventory, truncated-response and reasoning-output failures with MockTransport.
2. Correct only the local adapter and retain the existing validated JSON
   compatibility case for models returning a structured object in reasoning_content.
3. Run all Python tests, types, desktop tests/build and offline evaluations.
4. Compare exact Git trees, open a focused PR, and merge only after CI and review.

## Reference boundary

LM Studio. (n.d.). *List your models*. Retrieved September 7, 2026, from
https://lmstudio.ai/docs/developer/rest/list

LM Studio. (n.d.). *Structured output*. Retrieved September 7, 2026, from
https://lmstudio.ai/docs/developer/openai-compat/structured-output

Independent implementation based on documented protocol semantics. No reference
repository code is copied. Mock provider tests do not prove inference on Daniel's
Mac or the behavioural accuracy of any model.
