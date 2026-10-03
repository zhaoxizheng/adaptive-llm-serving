# Roadmap Curator Evaluation Results

## Behavior benchmark

Three one-run evals compared the skill with a no-skill baseline. The skill achieved a
100% assertion pass rate; the baseline achieved 88.9%. The discriminating result was an
executable local Markdown-link audit during a scope migration. Both configurations passed
the simpler component-removal and read-only audit scenarios.

Duration and model-token telemetry were not exposed by the collaboration notifications.
The benchmark workspace records them as unavailable rather than presenting output character
counts as model-token usage.

## Trigger evaluation

The Claude CLI trigger harness produced zero skill invocations for every positive and
negative query across repeated runs, even when executed from the repository root. This is
consistent with the harness not discovering this repository's `.trae/skills` location.
Treat the result as an environment limitation, not as evidence that the description can or
cannot distinguish roadmap-editing intent. Re-run when the harness supports project-level
Trae skills.

The final description was still revised to lead with user intent, explicitly name both
English and Chinese roadmap-editing requests, and exclude conceptual architecture Q&A.
