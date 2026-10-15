# Master Review Checklist (Two-Stage)

Use this checklist when reviewing an execution task's delivery, independently of the executor.

## Stage 1 — Spec Compliance (do first; decides whether to continue)

- [ ] Delivered scope matches allowed paths; no out-of-scope changes
- [ ] Every acceptance criterion checked individually against real evidence (diff, command output, exit codes)
- [ ] Stated head SHA matches the delivered commit
- [ ] No self-approval or self-merge happened

If any Stage 1 item fails, stop: do not proceed to Stage 2; return the task with findings.

## Stage 2 — Quality (only after all Stage 1 items pass)

- [ ] Changes follow existing conventions (language, structure, style)
- [ ] No secrets, personal paths, or unreviewed generated content introduced
- [ ] Documentation stays consistent with the rest of the kit
- [ ] Reusability preserved (no project-specific coupling)

## Severity Levels (tag each finding when reporting)

- **critical**: violates acceptance, contract, or security → blocks merge
- **major**: must be fixed before approval, but scope is contained → return to execution with fix guidance
- **minor**: recorded, does not block approval

## Mapping to Review Record

Output the review verdict in the four-part format defined by AGENTS.md
(verdict / user-visible changes / acceptance results / risks and next steps).
The highest unresolved severity determines the verdict: any unresolved
**critical** → returned or blocked; only **major** → returned with fix guidance;
**minor** or clean → approved.
