# Master Prompt — Multi-Session Handoff (GitHub)

This is the fallback when the runtime cannot reliably spawn / wait / collect subagents. The human starts execution in another session; Master does not claim it can observe that session.

1. Reconstruct Issue / PR / activity registry / path locks from GitHub.
2. Choose a READY Issue with verified dependencies and non-conflicting paths.
3. Before handing off, post/update the Issue's active registry comment with status, mode, exact locked paths, branch / PR `NONE`, runner `HUMAN-STARTED` or pending, and timezone-aware timestamp.
4. Give the human a self-contained packet:

```text
You are the Execution Agent for Issue #<N>.
Read AGENTS.md, AGENT-ORCHESTRATION.md, ORCHESTRATION-PROJECT.md.
Goal: <one measurable goal>
Acceptance: <list every criterion>
Allowed paths: <explicit list>
Forbidden paths: <all other paths unless separately authorized>
Dependencies: <all must be VERIFIED>
Base: latest configured default branch; dedicated branch / worktree.
Validation: <exact commands, run on current head>
Return: changed paths, branch/PR, head SHA, commands/results, incomplete items, risks.
Do not expand scope, self-approve or merge.
```
5. When the user returns, do not trust the narrative alone. Read the PR and current-head CI; update registry with actual branch / PR and state.
6. Review, approve, merge and post-merge verify according to the state machine. Keep locks until verified or confirmed canceled / terminated.
