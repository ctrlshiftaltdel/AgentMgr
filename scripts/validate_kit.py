#!/usr/bin/env python3
"""Static validation for the offline Agent-Orchestration kit."""
from pathlib import Path
import argparse, re, sys

ROOT = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--project-ready', action='store_true', help='fail while ORCHESTRATION-PROJECT.md still has FILL_IN placeholders')
args = parser.parse_args()
# Single source of truth for the shippable kit. This list has two jobs at once:
#   1. the structurally required files of this repository, and
#   2. the file manifest of the release package built by scripts/build_release.py.
# scripts/build_release.py parses THIS list to decide what to package, so there is
# no second manifest to maintain. Keep it in sync whenever a versioned artifact is
# added or removed. Files that must not ship (docs/, release/, the release builder
# itself, .orchestration/, .worktrees/, .workbuddy/) are simply not listed here.
REQUIRED = [
    'AGENT-BOOTSTRAP.md', 'AGENT-ORCHESTRATION-ACCEPTANCE.md', 'AGENT-ORCHESTRATION.md',
    'AGENTS.md', 'LICENSE.txt', 'MASTER.md', 'ORCHESTRATION-PROJECT.md', 'README.md', 'VERSION.txt',
    'prompts/MASTER-MULTI-SESSION.md', 'prompts/MASTER-SINGLE-SESSION.md', 'prompts/START-CHARTER.md',
    'prompts/START-EXECUTION.md', 'prompts/START-MASTER-EXISTING-PROJECT.md',
    'prompts/START-MASTER-NEW-PROJECT.md', 'prompts/START-REVIEWER.md',
    'scripts/orchestrator.py', 'scripts/validate_kit.py',
    'templates/DESIGN.md', 'templates/ORCHESTRATION-PROJECT.template.md', 'templates/PLAN.md',
    'templates/PROJECT-CHARTER.md', 'templates/REVIEW-CHECKLIST.md', 'templates/TASK-PACKET.md',
    'templates/gitignore.snippet',
    'tests/test_orchestrator.py',
]
errors=[]
for rel in REQUIRED:
    if not (ROOT/rel).is_file(): errors.append(f'missing required file: {rel}')
# Governance concepts are only meaningful in documentation. Scan document-class
# files (*.md / *.txt) and skip code dirs and binaries so the check cannot be
# satisfied by the validator's own source or by non-document assets.
DOC_SUFFIXES = ('.md', '.txt')
EXCLUDED_DIRS = {'scripts', '.orchestration', '.git', '__pycache__'}
doc_texts=[]
for p in ROOT.rglob('*'):
    if not p.is_file(): continue
    rel=p.relative_to(ROOT)
    if rel.suffix.lower() not in DOC_SUFFIXES: continue
    if EXCLUDED_DIRS & set(rel.parts): continue
    raw=p.read_text(encoding='utf-8', errors='replace')
    if '\x00' in raw: continue  # skip binary payloads
    doc_texts.append(raw)
text_all='\n'.join(doc_texts)
for needle in ['PLANNED', 'DISPATCHED', 'MASTER_REVIEW', 'APPROVED', 'VERIFIED', 'NEEDS_HUMAN_DECISION']:
    if needle not in text_all: errors.append(f'missing expected governance concept: {needle}')
# Relative Markdown links must resolve. Ignore anchors and external URLs.
for md in ROOT.rglob('*.md'):
    content=md.read_text(encoding='utf-8')
    for target in re.findall(r'\[[^\]]*\]\(([^)]+)\)', content):
        if target.startswith(('http://','https://','mailto:','#')): continue
        target=target.split('#',1)[0]
        if target and not (md.parent/target).exists(): errors.append(f'{md.relative_to(ROOT)}: broken relative link: {target}')
if args.project_ready:
    cfg=(ROOT/'ORCHESTRATION-PROJECT.md').read_text(encoding='utf-8')
    missing=cfg.count('FILL_IN')
    if missing: errors.append(f'ORCHESTRATION-PROJECT.md has {missing} FILL_IN placeholders')
if errors:
    print('VALIDATION FAILED')
    print('\n'.join(f'- {e}' for e in errors))
    sys.exit(1)
print(f'Offline kit validation passed ({len(REQUIRED)} required files checked).')
