# Master Prompt — Single-Session Agent Dispatch (GitHub)

Use only if the current runtime actually supports all four capabilities: C1 spawn with addressable handle; C2 isolated task context; C3 wait / collect result and evidence; C4 distinction between success / failure / cancellation. Probe them before dispatch. If any are missing, explicitly downgrade to `prompts/MASTER-MULTI-SESSION.md`.

1. Recover GitHub Issues, active registry comments, open PRs, changed paths, dependencies and current-head CI.
2. Select READY tasks whose dependencies are VERIFIED and whose paths do not overlap any active lock or open PR.
3. Update the Issue registry comment with `DISPATCHED`, mode, locked paths, runner / handle, timestamp and branch/PR placeholders **before** invoking spawn.
4. Spawn isolated Execution Subagents with complete Issue text, permitted/forbidden paths, dependency proof, validation commands and delivery requirements. One writable worktree per task.
5. Wait and collect actual status, artifacts and evidence. Unknown or missing result means failure / incomplete, not success.
6. Independently review the PR. Reviewer Subagents are optional second opinions and cannot approve / merge.
7. Merge only if current head matches approved SHA and G1–G6 pass. Verify the merged default branch, then update the registry and close the Issue.
