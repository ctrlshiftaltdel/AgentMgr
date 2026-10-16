#!/usr/bin/env python3
"""Static validation for the GitHub Agent-Orchestration kit."""
from pathlib import Path
import argparse, re, sys
ROOT=Path(__file__).resolve().parents[1]
parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--project-ready',action='store_true',help='fail while ORCHESTRATION-PROJECT.md has FILL_IN placeholders')
args=parser.parse_args()
REQUIRED=['AGENTS.md','MASTER.md','AGENT-ORCHESTRATION.md','AGENT-ORCHESTRATION-ACCEPTANCE.md','ORCHESTRATION-PROJECT.md','prompts/START-MASTER-NEW-PROJECT.md','prompts/START-MASTER-EXISTING-PROJECT.md','prompts/START-EXECUTION.md','prompts/START-REVIEWER.md','.github/ISSUE_TEMPLATE/agent-task.md','.github/pull_request_template.md','scripts/github_preflight.py','templates/ACTIVE-TASK-REGISTRY.md']
errors=[]
for p in REQUIRED:
    if not (ROOT/p).is_file(): errors.append(f'missing required file: {p}')
all_text='\n'.join(p.read_text(encoding='utf-8',errors='replace') for p in ROOT.rglob('*') if p.is_file() and '.git' not in p.parts and '__pycache__' not in p.parts)
for word in ['PLANNED','DISPATCHED','MASTER_REVIEW','APPROVED','VERIFIED','NEEDS_HUMAN_DECISION','locked_paths','head SHA','C1','C2','C3','C4']:
    if word not in all_text: errors.append(f'missing governance concept: {word}')
for md in ROOT.rglob('*.md'):
    for target in re.findall(r'\[[^\]]*\]\(([^)]+)\)',md.read_text(encoding='utf-8')):
        if target.startswith(('http://','https://','mailto:','#')): continue
        target=target.split('#',1)[0]
        if target and not (md.parent/target).exists(): errors.append(f'{md.relative_to(ROOT)}: broken link {target}')
# This is a reusable template. Source-project identifiers or domain-specific terms should not occur.
if args.project_ready:
    cfg=(ROOT/'ORCHESTRATION-PROJECT.md').read_text(encoding='utf-8')
    missing=cfg.count('FILL_IN')
    if missing: errors.append(f'ORCHESTRATION-PROJECT.md has {missing} FILL_IN placeholders')
if errors:
    print('VALIDATION FAILED'); print('\n'.join('- '+x for x in errors)); sys.exit(1)
print(f'GitHub kit validation passed ({len(REQUIRED)} required files checked).')
