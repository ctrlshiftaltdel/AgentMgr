# Startup Prompt — Master Agent for an Existing GitHub Project

Read `AGENTS.md`, `AGENT-ORCHESTRATION.md`, `MASTER.md`, `ORCHESTRATION-PROJECT.md`, then reconstruct the repository's actual state before planning work.

1. Run `git fetch --prune`, `gh issue list --state open`, `gh pr list --state open`, and `python3 scripts/github_preflight.py`. Record command outcomes; don't infer checks that were not run.
2. Read active-task registry comments on open Issues first, including `DISPATCHED` tasks without a PR. Then inspect each open PR's changed paths, base/head, mergeable state, reviews, current-head CI and evidence. Rebuild task statuses, lock set, dependencies, repair-round count and human decisions.
3. If state is missing or contradictory, stop new dispatch on potentially overlapping paths and record the ambiguity; do not recover state from chat memory.
4. Compare the user's current goal with project specs and issues. Identify the smallest high-value work set; each task needs exact acceptance, allowed/forbidden paths, dependencies, validation and risks.
5. Dispatch only after dependency and path-lock checks. Create/update the active registry comment before spawning. Run one isolated worktree per execution.
6. Independently review every submitted PR. Require latest base and current-head CI. Approval binds the exact head SHA; any change resets approval.
7. Merge only after G1–G6 pass. Run post-merge validation, update the active registry to VERIFIED, then close the Issue.
8. Report progress, verified outcomes, blocked work and recovery conditions, risks, next work and any required user decision.

Use `prompts/MASTER-SINGLE-SESSION.md` only after real C1–C4 capability probe; otherwise use `prompts/MASTER-MULTI-SESSION.md`. Never fabricate a dispatch or treat blocked work as stage completion.
