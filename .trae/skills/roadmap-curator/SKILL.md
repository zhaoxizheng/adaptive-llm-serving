---
name: roadmap-curator
description: Use this skill whenever editing or auditing the adaptive-llm-serving learning plan or another repository's multi-week roadmap. Trigger on 学习计划 or roadmap requests to add/remove technologies or weeks, narrow or redesign the final architecture, propagate deployment/resource invariants, deduplicate or renumber references, repair README/index/handoffs after deletions, validate cross-file consistency, or push roadmap changes to GitHub. Use it even when the user names only one component or one week, because that change may affect downstream plans and references. Do not use for conceptual architecture Q&A unless the user asks to change roadmap files.
---

# Roadmap Curator

Maintain the roadmap as one coherent curriculum and experiment system. Do not edit one
weekly file in isolation when the decision changes the final architecture.

## Repository contract

- Treat `README.md` and `docs/learning-roadmap.md` as the two entrypoints.
- Treat `docs/week-NN-plan.md` and `docs/week-NN-references.md` as a required pair.
- Preserve a contiguous week sequence ending at the actual capstone.
- Preserve each affected plan Day 1-7 schedule and exact 11-hour total unless the user
  explicitly changes the format.
- Keep reference URLs first-use-only. Later weeks link back to the original week and
  reference number instead of duplicating the URL.
- Distinguish planned artifacts from files or deployments that actually exist.
- Preserve user terminology when it defines a technical ownership boundary.

## Workflow

### 1. Establish the target architecture

Turn the request into three lists before editing:

1. **Required** — components, deployment shapes, and learning outcomes that must remain.
2. **Optional** — comparisons or control planes that may remain without defining the main
   architecture.
3. **Excluded** — components and architecture branches that must disappear.

Write explicit invariants for the unit being routed, scheduled, and scaled. Examples:

- `1 replica = 1 Pod / 1 node / G GPUs / TP=G`.
- Routing selects a complete replica endpoint, not an internal GPU or rank.
- Autoscaling changes replica count, not the GPU count or TP degree inside a replica.

If the target makes a whole late-stage branch irrelevant, remove that branch instead of
leaving a large negative appendix.

Interpret scope at the correct unit. "One replica must be single-host" does not mean all
replicas must share one host: independent replicas may run on different nodes, but every
GPU/rank inside one replica remains co-located. Preserve this distinction in examples and
validation regexes.

### 2. Build the impact graph before editing

Inspect the full repository, not only the named week:

```bash
rg --files README.md docs .trae/skills
rg -ni REMOVED_TERM_OR_WEEK README.md docs
```

Map entrypoint summaries, week indexes, adjacent prerequisites/handoffs, downstream
reference-number ranges, hardware/cost/deployment contracts, directory trees, and
portfolio wording. Preserve unrelated dirty-worktree changes.

Read every user correction in the active chain as cumulative unless it explicitly replaces
an earlier constraint. For example, removing one autoscaler must not silently preserve a
different branch that a later request also excluded.

### 3. Decide whether to delete, rewrite, or retain

- **Delete** a week pair when its purpose is entirely outside the target architecture.
- **Rewrite** a week when its learning objective remains useful but its implementation
  path changes.
- **Retain** foundational material that still supports the target, even if its title uses
  a broad term.

When deleting weeks:

- update roadmap duration and phase names;
- remove index/table rows and every link to deleted files;
- remove downstream handoffs, deliverables, layout entries, resource guidance, and
  resume claims;
- keep remaining week numbers contiguous; do not create empty placeholder weeks.

### 4. Propagate architecture invariants

Carry the chosen resource and ownership model through the runtime/benchmark week,
Kubernetes workload week, routing weeks, autoscaling week, cloud portability week, and
capstone failure/rollout/cost/runbook sections.

For each layer, state what it owns and what it does not own. A formula such as
`total GPUs = replicas × GPUs per replica` should appear wherever capacity or cost is
interpreted.

### 5. Maintain references

Before removing a source, search all references to its week and number. After removal:

- renumber only when needed;
- update every downstream number range and reading-order reference;
- remove a URL completely if its topic is excluded and no retained lesson needs it;
- do not keep a deprecated-topic warning merely to preserve the old link.

### 6. Validate before reporting or pushing

Run the bundled audit first:

```bash
python .trae/skills/roadmap-curator/scripts/audit_roadmap.py --repo . --expected-last-week N --forbid REMOVED_TERM --strict-week NN
```

Then run repository-specific tests and `git diff --check`. Confirm plan/reference parity,
contiguous weeks, local links, deleted-week references, removed terms, duplicate URLs,
affected week hours, entrypoint consistency, and unrelated dirty changes.

If GitHub delivery is requested, commit atomically, push the requested branch, and verify
local `HEAD`, the remote-tracking ref, and `git ls-remote` all match.

After a successful edit, run one independent read-only review focused on semantic residue;
keyword checks alone cannot prove that resource ownership or handoffs are coherent.

## Output

Lead with the resulting architecture and duration. Summarize retained and removed scope,
the key experiment location, validation results, and the commit/remote link when pushed.
Do not claim planned experiments were executed or call a partial keyword cleanup complete.
