#!/usr/bin/env python3
"""Read-only Git/GitHub CLI preflight. Does not create or modify Issues, PRs or settings."""
import subprocess, sys, shutil

def run(args, required=True):
    try:
        p=subprocess.run(args,text=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=20)
    except (OSError,subprocess.TimeoutExpired) as e:
        print(f'FAIL: {args[0]} could not run: {e}'); return None
    if p.returncode:
        print(f'FAIL: {" ".join(args)} (exit {p.returncode})')
        msg=(p.stderr or p.stdout).strip()
        if msg: print('  '+msg[:700].replace('\n','\n  '))
        return None
    return p.stdout.strip()

def main():
    ok=True
    if not shutil.which('git'):
        print('FAIL: git is not installed'); ok=False
    else:
        root=run(['git','rev-parse','--show-toplevel'])
        if root:
            print(f'PASS: Git repository: {root}')
            branch=run(['git','branch','--show-current'])
            print(f'INFO: current branch: {branch or "(detached / unknown)"}')
            dirty=run(['git','status','--short'])
            if dirty is not None and dirty:
                print('WARN: working tree has local changes; inspect before agent dispatch')
                print('\n'.join('  '+x for x in dirty.splitlines()[:15]))
        else: ok=False
    if not shutil.which('gh'):
        print('FAIL: gh CLI is not installed'); ok=False
    else:
        auth=run(['gh','auth','status'])
        if auth is None: ok=False
        else: print('PASS: GitHub CLI is authenticated (credentials are not printed by this script)')
        repo=run(['gh','repo','view','--json','nameWithOwner,defaultBranchRef,url'])
        if repo:
            print('PASS: current repository context: '+repo)
        else:
            print('FAIL: could not resolve current GitHub repository; enter the target repo or set its context')
            ok=False
    if ok: print('Preflight passed. This script performed read-only checks only.')
    else: print('Preflight incomplete. Resolve failures before claiming GitHub dispatch / CI capability.')
    return 0 if ok else 1
if __name__=='__main__': raise SystemExit(main())
