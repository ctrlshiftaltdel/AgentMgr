# Local Task Packet

> This is the self-contained brief passed to one Execution Agent. The SQLite task record remains the state source of truth.

- **Task ID:**
- **Title:**
- **Goal / user-visible result:**
- **Context:**
- **Allowed paths:**
- **Forbidden paths:**
- **Dependencies (must be VERIFIED):**
- **Acceptance criteria:**
  - [ ]
- **Validation commands:**
- **Baseline branch / commit:**
- **Expected branch / Worktree:**
- **Risks / human decisions:**
- **Delivery evidence:** changed paths, Diff, head SHA, command output / exit codes, incomplete items and risks.
- **Hard boundaries:** no scope expansion, no self-approval, no merge.

## Task decomposition hard rules

- Each task carries exactly one independently acceptable goal.
- Allowed paths must be specified precisely down to concrete files or directories, never as vague wildcards or whole-repo access.
- Every acceptance criterion must be individually checkable, and each must be paired with a verification command and its expected output.
- Granularity ceiling: a single review must be able to walk through all acceptance criteria item by item. If a task cannot be broken down finely enough, split it further; dispatching oversized tasks is not allowed.

## Verification before completion

- Delivery evidence must include real command output and exit codes from actual runs; inferred claims such as "this should pass" are not accepted.
- Tasks involving code follow red-green-refactor: first write a failing test and actually run it to prove it fails, then make it pass with a minimal implementation. Declaring completion without test evidence is prohibited.
- Any unverified item must be explicitly marked as incomplete and must not be counted as passing.
