# AGENT-ORCHESTRATION-ACCEPTANCE.md — GitHub Acceptance Checklist

## A. Configuration and recovery
- [ ] `ORCHESTRATION-PROJECT.md` has no unresolved `FILL_IN` entries that block execution.
- [ ] Required CI checks are identified by exact name and branch protection / ruleset is configured.
- [ ] A new Master context can recover open tasks, active locks, PR states, reviewer rounds, head SHA, and blocked reasons from GitHub.
- [ ] Recovery includes dispatched Issues with no PR yet, not only open PRs.

## B. Dispatch and path locks
- [ ] Before starting an Execution Agent, an active registry record is added to the Issue with locked paths and runtime identity.
- [ ] Same path and parent/child path overlaps are treated as conflicts.
- [ ] Two disjoint paths with no dependency can proceed in parallel.
- [ ] The lock remains when the agent status is unknown, a PR is closed, or no PR exists.
- [ ] Registry is updated at dispatch, PR creation, review, block/failure, merge and verification.

## C. Review and merge
- [ ] Master independently checks issue acceptance, diff paths, current head SHA, current-head CI, and actual validation evidence.
- [ ] Any head change invalidates previous approval and requires new CI / tests / review.
- [ ] Only Master merges.
- [ ] Merge and branch deletion are distinct outcomes; deletion failure cannot lead to a second merge.
- [ ] Post-merge verification is recorded before issue closure and VERIFIED state.

## D. Controlled failure
Use an isolated test Issue or safe branch; never land a deliberate defect in the production default branch.
- [ ] Master catches a known acceptance defect and gives actionable review feedback.
- [ ] Execution fixes it; new head has current CI / validation evidence and is re-reviewed.
- [ ] Third failed review leads to BLOCKED; no fourth retry and no reduced acceptance criteria.
- [ ] Spawn failure, timeout, cancellation, empty / ambiguous result is not treated as success.
- [ ] Test record includes date, runtime, issue/PR, each head SHA, check results, final status and cleanup.

## E. Evidence record
```text
Date / runtime version:
Repository / default branch:
Issue / PR:
C1-C4 capability evidence:
Active lock registry checked:
Head SHA before and after repair:
Current-head CI checks and results:
Review rounds:
Was BLOCKED reached at cap:
Merge SHA / post-merge validation:
Unverified areas:
Final conclusion: PASS / PARTIAL / FAIL
```
