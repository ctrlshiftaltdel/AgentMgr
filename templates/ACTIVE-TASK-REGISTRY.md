# Active Task Registry Comment Template

Copy this block into the GitHub Issue **before** starting the Execution Agent. Update the same record throughout the task lifecycle. Keep actual paths / runtime identifiers, do not leave example values in place.

```text
<!-- ORCH-TASK-REGISTRY v1 -->
status: DISPATCHED
mode: single-session | multi-session
locked_paths:
  - <repo-relative-path>
branch: NONE
pr: NONE
runner: <runtime + addressable handle, or HUMAN-STARTED>
dispatched_at: <ISO-8601 with timezone>
updated_at: <ISO-8601 with timezone>
head_sha: NONE
blocked_reason: NONE
<!-- /ORCH-TASK-REGISTRY -->
```

After completion, record `VERIFIED`, the merged / verified SHA, and evidence location before closing the Issue. Do not release locks while the execution instance may still be running.
