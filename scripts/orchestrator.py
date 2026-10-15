#!/usr/bin/env python3
# Local single-user task registry and atomic path-lock manager.
# This tool manages governance state only; it does not launch agents or merge Git branches.
from __future__ import annotations
import argparse, json, re, sqlite3, sys
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

ACTIVE = {'DISPATCHED','IN_PROGRESS','REVIEW_READY','MASTER_REVIEW','APPROVED','MERGED','BLOCKED','NEEDS_HUMAN_DECISION','FAILED'}
TERMINAL = {'VERIFIED','CANCELED'}
TRANSITIONS = {
 'PLANNED': {'READY','BLOCKED','NEEDS_HUMAN_DECISION','CANCELED'},
 'READY': {'DISPATCHED','BLOCKED','NEEDS_HUMAN_DECISION','CANCELED'},
 'DISPATCHED': {'IN_PROGRESS','FAILED','BLOCKED','NEEDS_HUMAN_DECISION','CANCELED'},
 'IN_PROGRESS': {'REVIEW_READY','FAILED','BLOCKED','NEEDS_HUMAN_DECISION','CANCELED'},
 'REVIEW_READY': {'MASTER_REVIEW','IN_PROGRESS','BLOCKED','NEEDS_HUMAN_DECISION'},
 'MASTER_REVIEW': {'APPROVED','IN_PROGRESS','BLOCKED','NEEDS_HUMAN_DECISION','CANCELED'},
 'APPROVED': {'MERGED','MASTER_REVIEW','BLOCKED'},
 'MERGED': {'VERIFIED','BLOCKED'},
 'VERIFIED': set(), 'BLOCKED': {'READY','IN_PROGRESS','MASTER_REVIEW','NEEDS_HUMAN_DECISION','FAILED','CANCELED'},
 'NEEDS_HUMAN_DECISION': {'READY','IN_PROGRESS','MASTER_REVIEW','CANCELED'},
 'FAILED': {'READY','CANCELED'}, 'CANCELED': set(),
}

def now(): return datetime.now(timezone.utc).isoformat(timespec='seconds')
def root_path(raw): return Path(raw).resolve()
def db_path(root): return root/'.orchestration'/'state.sqlite3'
def connect(root):
    p=db_path(root)
    if not p.exists(): raise RuntimeError(f'task database not initialized: {p}; run `init` first')
    con=sqlite3.connect(p, timeout=15, isolation_level=None)
    con.row_factory=sqlite3.Row
    con.execute('PRAGMA foreign_keys=ON')
    con.execute('PRAGMA busy_timeout=15000')
    return con

# Review-fix retry cap precedence (see also read_retry_cap):
#   1. <root>/.orchestration/config.json key "retry_cap" when it is an integer >= 1.
#   2. Otherwise the built-in default RETRY_CAP_DEFAULT = 3.
# A missing file, a missing key, or an invalid value falls back to the default so
# the historical hard-coded behaviour (3 rounds) is preserved for existing roots.
RETRY_CAP_DEFAULT = 3
def config_path(root): return root/'.orchestration'/'config.json'
def read_retry_cap(root):
    """Return the effective review-fix retry cap (integer >= 1, default 3).

    Precedence: config.json key "retry_cap" wins when present and valid;
    anything else (missing file/key, non-integer, bool, < 1, malformed JSON)
    silently yields RETRY_CAP_DEFAULT.
    """
    try:
        data=json.loads(config_path(root).read_text(encoding='utf-8'))
    except Exception:
        return RETRY_CAP_DEFAULT
    value=data.get('retry_cap') if isinstance(data,dict) else None
    if isinstance(value,bool) or not isinstance(value,int) or value<1:
        return RETRY_CAP_DEFAULT
    return value

def safe_rollback(con):
    """Roll back only when a transaction is actually open; never mask the real error.

    If BEGIN IMMEDIATE itself failed (for example because the database is locked)
    there is no transaction to roll back, and issuing ROLLBACK would raise
    'cannot rollback - no transaction is active', hiding the original failure.
    """
    try:
        if con.in_transaction:
            con.execute('ROLLBACK')
    except sqlite3.Error:
        pass

def event(con, task_id, action, from_status=None, to_status=None, note='', data=None):
    con.execute('INSERT INTO events(task_id,at,action,from_status,to_status,note,data_json) VALUES(?,?,?,?,?,?,?)',
        (task_id,now(),action,from_status,to_status,note,json.dumps(data or {},ensure_ascii=False)))

def init(root):
    d=root/'.orchestration'; d.mkdir(parents=True,exist_ok=True)
    (d/'backups').mkdir(exist_ok=True)
    cfg=config_path(root)
    if not cfg.exists():
        cfg.write_text(json.dumps({'retry_cap':RETRY_CAP_DEFAULT})+'\n',encoding='utf-8')
    con=sqlite3.connect(db_path(root))
    con.executescript('''
    PRAGMA journal_mode=WAL;
    PRAGMA foreign_keys=ON;
    CREATE TABLE IF NOT EXISTS tasks(
      id TEXT PRIMARY KEY, title TEXT NOT NULL, description TEXT NOT NULL DEFAULT '',
      paths_json TEXT NOT NULL, depends_json TEXT NOT NULL, acceptance_json TEXT NOT NULL,
      status TEXT NOT NULL DEFAULT 'PLANNED', branch TEXT NOT NULL DEFAULT '', worktree TEXT NOT NULL DEFAULT '',
      head_sha TEXT NOT NULL DEFAULT '', approved_sha TEXT NOT NULL DEFAULT '', merged_sha TEXT NOT NULL DEFAULT '', retries INTEGER NOT NULL DEFAULT 0,
      created_at TEXT NOT NULL, updated_at TEXT NOT NULL, last_note TEXT NOT NULL DEFAULT ''
    );
    CREATE TABLE IF NOT EXISTS locks(
      path TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
      created_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS locks_task_idx ON locks(task_id);
    CREATE TABLE IF NOT EXISTS events(
      id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL, at TEXT NOT NULL,
      action TEXT NOT NULL, from_status TEXT, to_status TEXT, note TEXT NOT NULL DEFAULT '', data_json TEXT NOT NULL DEFAULT '{}'
    );
    CREATE INDEX IF NOT EXISTS events_task_idx ON events(task_id,id);
    ''')
    con.close()
    # Keep private, machine-local state out of the worktree and commits.
    gi=root/'.git'/'info'/'exclude'
    if (root/'.git').is_dir():
        gi.parent.mkdir(parents=True,exist_ok=True)
        current=gi.read_text(encoding='utf-8',errors='ignore') if gi.exists() else ''
        if '.orchestration/' not in current.splitlines():
            with gi.open('a',encoding='utf-8') as f: f.write('\n# Agent-Orchestration machine-local state\n.orchestration/\n')
    print(f'Initialized local task registry: {db_path(root)}')
    print(f'Review-fix retry cap: {read_retry_cap(root)} (edit key "retry_cap" in {cfg} to change it)')
    print('No remote platform is used. Back up .orchestration/ with your project backups.')

def normalize_path(raw):
    s=raw.replace('\\','/').strip()
    p=PurePosixPath(s)
    if not s or p.is_absolute() or re.match(r'^[A-Za-z]:/', s) or any(part in ('..','.') for part in p.parts) or any(ch in s for ch in '*?[]'):
        raise ValueError(f'invalid relative path: {raw!r}')
    return str(p).rstrip('/') or '.'

def overlaps(a,b):
    if a==b or a=='.' or b=='.': return True
    return a.startswith(b.rstrip('/')+'/') or b.startswith(a.rstrip('/')+'/')

def get_task(con, task_id):
    row=con.execute('SELECT * FROM tasks WHERE id=?',(task_id,)).fetchone()
    if not row: raise RuntimeError(f'unknown task: {task_id}')
    return row

def task_add(root, args):
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{1,63}',args.id): raise ValueError('task ID must be 2-64 chars: letters, digits, dot, underscore, hyphen')
    paths=sorted(set(normalize_path(p) for p in args.paths))
    if not paths: raise ValueError('at least one allowed path is required')
    if not args.acceptance: raise ValueError('at least one acceptance criterion is required')
    depends=sorted(set(args.depends or []))
    if args.id in depends: raise ValueError('task cannot depend on itself')
    con=connect(root)
    try:
        con.execute('BEGIN IMMEDIATE')
        for dep in depends:
            if not con.execute('SELECT 1 FROM tasks WHERE id=?',(dep,)).fetchone(): raise RuntimeError(f'dependency not found: {dep}')
        con.execute('INSERT INTO tasks(id,title,description,paths_json,depends_json,acceptance_json,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)',
            (args.id,args.title,args.description or '',json.dumps(paths),json.dumps(depends),json.dumps(args.acceptance,ensure_ascii=False),'PLANNED',now(),now()))
        # Detect cycles after insertion. Rollback avoids leaving a bad task behind.
        seen=set(); stack=set()
        def visit(tid):
            if tid in stack: raise RuntimeError('dependency cycle detected')
            if tid in seen: return
            stack.add(tid)
            row=con.execute('SELECT depends_json FROM tasks WHERE id=?',(tid,)).fetchone()
            for d in json.loads(row['depends_json']): visit(d)
            stack.remove(tid); seen.add(tid)
        visit(args.id)
        event(con,args.id,'TASK_CREATED',None,'PLANNED','Task created',{'paths':paths,'depends':depends})
        con.execute('COMMIT')
    except Exception:
        safe_rollback(con); raise
    finally: con.close()
    print(f'{args.id}: created in PLANNED')

def task_list(root, args):
    con=connect(root)
    try:
        rows=con.execute('SELECT * FROM tasks ORDER BY created_at,id').fetchall()
        for r in rows:
            if args.active_only and r['status'] in TERMINAL: continue
            paths=', '.join(json.loads(r['paths_json']))
            locks=[x['path'] for x in con.execute('SELECT path FROM locks WHERE task_id=? ORDER BY path',(r['id'],))]
            print(f"{r['id']}\t{r['status']}\t{r['title']}\n  paths: {paths}\n  locks: {', '.join(locks) if locks else '(none)'}\n  retries: {r['retries']}")
    finally: con.close()

def task_show(root, task_id):
    con=connect(root)
    try:
        r=get_task(con,task_id)
        print(f"# {r['id']} — {r['title']}\n\nStatus: {r['status']}\nDescription: {r['description']}\nAllowed paths:\n"+'\n'.join(f'- {p}' for p in json.loads(r['paths_json']))+'\n\nDependencies:\n'+('\n'.join(f'- {d}' for d in json.loads(r['depends_json'])) or '- None')+'\n\nAcceptance criteria:\n'+'\n'.join(f'- [ ] {x}' for x in json.loads(r['acceptance_json']))+f"\n\nBranch: {r['branch'] or '(unset)'}\nWorktree: {r['worktree'] or '(unset)'}\nHead SHA: {r['head_sha'] or '(unset)'}\nApproved SHA: {r['approved_sha'] or '(unset)'}\nMerge SHA: {r['merged_sha'] or '(unset)'}\nRetries: {r['retries']}\nLast note: {r['last_note'] or '(none)'}")
        print('\nEvents:')
        for e in con.execute('SELECT * FROM events WHERE task_id=? ORDER BY id',(task_id,)):
            print(f"- {e['at']} {e['action']} {e['from_status'] or '-'}→{e['to_status'] or '-'} {e['note']}")
    finally: con.close()

def dependency_check(con, row):
    for dep in json.loads(row['depends_json']):
        d=get_task(con,dep)
        if d['status']!='VERIFIED': raise RuntimeError(f'dependency {dep} is {d["status"]}, expected VERIFIED')

def task_ready(root, task_id):
    con=connect(root)
    try:
        con.execute('BEGIN IMMEDIATE'); r=get_task(con,task_id)
        if r['status'] not in {'PLANNED','BLOCKED','FAILED','NEEDS_HUMAN_DECISION'}: raise RuntimeError(f'cannot set READY from {r["status"]}')
        dependency_check(con,r)
        # Current locks must not overlap declared paths. It is OK that lock is not acquired until dispatch; this is a preflight only.
        paths=json.loads(r['paths_json'])
        for lock in con.execute('SELECT path,task_id FROM locks'):
            if lock['task_id']==task_id: continue
            for p in paths:
                if overlaps(p,lock['path']): raise RuntimeError(f'path conflict: {p} overlaps {lock["path"]}, held by {lock["task_id"]}')
        old=r['status']; con.execute("UPDATE tasks SET status='READY',updated_at=?,last_note=? WHERE id=?",(now(),'Dependencies and paths checked',task_id))
        event(con,task_id,'STATUS_CHANGED',old,'READY','Dependencies and paths checked')
        con.execute('COMMIT')
    except Exception:
        safe_rollback(con); raise
    finally: con.close()
    print(f'{task_id}: READY')

def task_dispatch(root, task_id):
    con=connect(root)
    try:
        con.execute('BEGIN IMMEDIATE'); r=get_task(con,task_id)
        if r['status']!='READY': raise RuntimeError(f'only READY tasks can be dispatched; current status is {r["status"]}')
        dependency_check(con,r)
        paths=json.loads(r['paths_json'])
        conflicts=[]
        locks=con.execute('SELECT path,task_id FROM locks WHERE task_id<>?',(task_id,)).fetchall()
        for p in paths:
            for lock in locks:
                if overlaps(p,lock['path']): conflicts.append((p,lock['path'],lock['task_id']))
        if conflicts:
            detail='; '.join(f'{p} overlaps {held} (task {tid})' for p,held,tid in conflicts)
            raise RuntimeError('dispatch refused; path conflict: '+detail)
        for p in paths:
            con.execute('INSERT OR REPLACE INTO locks(path,task_id,created_at) VALUES(?,?,?)',(p,task_id,now()))
        old=r['status']; con.execute("UPDATE tasks SET status='DISPATCHED',updated_at=?,last_note=? WHERE id=?",(now(),'Atomic path locks acquired before dispatch',task_id))
        event(con,task_id,'TASK_DISPATCHED',old,'DISPATCHED','Atomic path locks acquired',{'paths':paths})
        con.execute('COMMIT')
    except Exception:
        safe_rollback(con); raise
    finally: con.close()
    print(f'{task_id}: DISPATCHED; locks acquired for {", ".join(paths)}')
    print('Now pass the self-contained task packet to a real Execution Agent. This command does not launch agents.')

def set_workspace(root, args):
    con=connect(root)
    try:
        con.execute('BEGIN IMMEDIATE'); r=get_task(con,args.id)
        if r['status'] in TERMINAL: raise RuntimeError('cannot update workspace for terminal task')
        approved=r['approved_sha']
        status=r['status']; oldsha=r['head_sha']
        newsha=args.head_sha or oldsha
        if status=='APPROVED' and newsha and newsha!=approved:
            con.execute("UPDATE tasks SET status='MASTER_REVIEW',approved_sha='',head_sha=?,branch=COALESCE(NULLIF(?,''),branch),worktree=COALESCE(NULLIF(?,''),worktree),updated_at=?,last_note=? WHERE id=?",
                (newsha,args.branch or '',args.worktree or '',now(),'head changed; previous approval invalidated',args.id))
            event(con,args.id,'APPROVAL_INVALIDATED','APPROVED','MASTER_REVIEW','head changed',{'old_head':oldsha,'new_head':newsha})
        else:
            con.execute('UPDATE tasks SET head_sha=?,branch=COALESCE(NULLIF(?,\'\'),branch),worktree=COALESCE(NULLIF(?,\'\'),worktree),updated_at=?,last_note=? WHERE id=?',
                (newsha,args.branch or '',args.worktree or '',now(),args.note or 'workspace updated',args.id))
            event(con,args.id,'WORKSPACE_UPDATED',status,status,args.note or 'workspace updated',{'branch':args.branch,'worktree':args.worktree,'head_sha':newsha})
        con.execute('COMMIT')
    except Exception:
        safe_rollback(con); raise
    finally: con.close()
    print(f'{args.id}: workspace updated')

def task_status(root, args):
    dest=args.status.upper()
    if dest not in TRANSITIONS: raise ValueError(f'unknown status: {dest}')
    con=connect(root)
    try:
        con.execute('BEGIN IMMEDIATE'); r=get_task(con,args.id); old=r['status']
        if dest in {'APPROVED','MERGED','VERIFIED','BLOCKED','NEEDS_HUMAN_DECISION','FAILED','CANCELED'} and not (args.note or '').strip():
            raise RuntimeError(f'{dest} requires an audit note via --note (include decision / evidence / recovery reason)')
        if dest not in TRANSITIONS[old]: raise RuntimeError(f'illegal transition {old} -> {dest}; allowed: {", ".join(sorted(TRANSITIONS[old])) or "none"}')
        if dest=='READY': raise RuntimeError('use `task ready <ID>` so dependency and path-conflict checks cannot be bypassed')
        if dest=='APPROVED':
            sha=args.head_sha or r['head_sha']
            if not sha: raise RuntimeError('APPROVED requires a current --head-sha')
            if r['head_sha'] and sha!=r['head_sha']: raise RuntimeError(f'approval SHA {sha} does not match current task head {r["head_sha"]}')
            con.execute('UPDATE tasks SET approved_sha=? WHERE id=?',(sha,args.id))
        elif old=='APPROVED' and dest=='MERGED':
            if not r['approved_sha'] or r['approved_sha'] != r['head_sha']:
                raise RuntimeError('approved SHA does not match current task head; return to MASTER_REVIEW')
            if not getattr(args, 'merge_sha', None):
                raise RuntimeError('MERGED requires --merge-sha for the resulting local merge commit')
            if not re.fullmatch(r'[0-9a-fA-F]{40}', args.merge_sha):
                raise RuntimeError('MERGED requires a full 40-character hex --merge-sha (abbreviated SHAs are refused)')
            args.merge_sha=args.merge_sha.lower()
        if dest=='IN_PROGRESS' and old=='MASTER_REVIEW':
            retry_cap=read_retry_cap(root)
            if r['retries'] >= retry_cap:
                dest='BLOCKED'
                retries=r['retries']
                args.note=(args.note + '; ' if args.note else '') + f'Retry cap reached after {retry_cap} review-fix rounds; manual decision required.'
            else:
                retries=r['retries']+1
                con.execute('UPDATE tasks SET retries=? WHERE id=?',(retries,args.id))
        else: retries=r['retries']
        new_head=args.head_sha or r['head_sha']
        if old=='APPROVED' and args.head_sha and dest!='MERGED' and args.head_sha!=r['approved_sha']:
            raise RuntimeError('head changed after approval; approval is invalid')
        note=args.note or ''
        if old=='APPROVED' and dest=='MASTER_REVIEW':
            con.execute("UPDATE tasks SET approved_sha='' WHERE id=?",(args.id,))
        if dest=='MERGED':
            con.execute('UPDATE tasks SET status=?,merged_sha=?,updated_at=?,last_note=? WHERE id=?',(dest,getattr(args, 'merge_sha', None),now(),note,args.id))
        else:
            con.execute('UPDATE tasks SET status=?,head_sha=?,updated_at=?,last_note=? WHERE id=?',(dest,new_head,now(),note,args.id))
        event(con,args.id,'STATUS_CHANGED',old,dest,note,{'head_sha':new_head,'merge_sha':getattr(args, 'merge_sha', None),'retries':retries})
        if dest in TERMINAL:
            con.execute('DELETE FROM locks WHERE task_id=?',(args.id,))
            event(con,args.id,'LOCKS_RELEASED',dest,dest,'terminal task status; path locks released')
        con.execute('COMMIT')
    except Exception:
        safe_rollback(con); raise
    finally: con.close()
    print(f'{args.id}: {old} -> {dest}' + (f' (head {args.head_sha})' if args.head_sha and dest!='MERGED' else f' (merge {args.merge_sha})' if getattr(args, 'merge_sha', None) else ''))

def task_amend(root, args):
    """Correct an already-recorded merge SHA on a MERGED / VERIFIED task.

    The only audited entry point for changing merged_sha after MERGED: requires a
    full 40-hex SHA and a --note reason, and records a RECORD_AMENDED event with
    the old/new values. No other command mutates merged_sha outside MERGED set-up.
    """
    sha=(args.merge_sha or '').strip()
    if not re.fullmatch(r'[0-9a-fA-F]{40}', sha):
        raise RuntimeError('amend requires a full 40-character hex --merge-sha')
    if not (args.note or '').strip():
        raise RuntimeError('amend requires --note explaining the merge SHA correction')
    new=sha.lower()
    con=connect(root)
    try:
        con.execute('BEGIN IMMEDIATE'); r=get_task(con,args.id)
        if r['status'] not in {'MERGED','VERIFIED'}:
            raise RuntimeError(f'merge SHA can only be amended on MERGED or VERIFIED tasks; current status is {r["status"]}')
        old=r['merged_sha']
        con.execute('UPDATE tasks SET merged_sha=?,updated_at=?,last_note=? WHERE id=?',(new,now(),args.note,args.id))
        event(con,args.id,'RECORD_AMENDED',r['status'],r['status'],args.note,{'field':'merged_sha','old':old,'new':new})
        con.execute('COMMIT')
    except Exception:
        safe_rollback(con); raise
    finally: con.close()
    print(f'{args.id}: merge SHA amended {old or "(unset)"} -> {new}')

def unlock(root, args):
    con=connect(root)
    try:
        con.execute('BEGIN IMMEDIATE'); r=get_task(con,args.id)
        if r['status'] not in TERMINAL and not args.force:
            raise RuntimeError('task is not terminal; verify that its execution has stopped, then use --force with a reason if unlock is truly required')
        rows=con.execute('SELECT path FROM locks WHERE task_id=?',(args.id,)).fetchall()
        con.execute('DELETE FROM locks WHERE task_id=?',(args.id,))
        event(con,args.id,'MANUAL_UNLOCK',r['status'],r['status'],args.reason,{'released_paths':[x['path'] for x in rows],'force':args.force})
        con.execute('COMMIT')
    except Exception:
        safe_rollback(con); raise
    finally: con.close()
    print(f'{args.id}: released {len(rows)} path lock(s); reason recorded')

def backup(root):
    src=db_path(root)
    if not src.exists(): raise RuntimeError('task database is not initialized')
    outdir=root/'.orchestration'/'backups'; outdir.mkdir(parents=True,exist_ok=True)
    name='state-'+datetime.now().strftime('%Y%m%d-%H%M%S')+'.sqlite3'; dest=outdir/name
    a=sqlite3.connect(src); b=sqlite3.connect(dest)
    try: a.backup(b)
    finally: b.close(); a.close()
    print(f'Backup created: {dest}')

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',default='.',help='project root (default: current directory)')
    sub=parser.add_subparsers(dest='cmd',required=True)
    sub.add_parser('init',help='initialize local task database and .orchestration/config.json (retry_cap, default 3)')
    sub.add_parser('backup',help='create a consistent SQLite backup')
    t=sub.add_parser('task',help='task operations'); ts=t.add_subparsers(dest='taskcmd',required=True)
    a=ts.add_parser('add'); a.add_argument('--id',required=True); a.add_argument('--title',required=True); a.add_argument('--description',default=''); a.add_argument('--paths',nargs='+',required=True); a.add_argument('--depends',nargs='*',default=[]); a.add_argument('--acceptance',nargs='+',required=True)
    l=ts.add_parser('list'); l.add_argument('--active-only',action='store_true')
    sh=ts.add_parser('show'); sh.add_argument('id')
    rd=ts.add_parser('ready'); rd.add_argument('id')
    dp=ts.add_parser('dispatch'); dp.add_argument('id')
    st=ts.add_parser('status'); st.add_argument('id'); st.add_argument('status',choices=sorted(TRANSITIONS)); st.add_argument('--head-sha'); st.add_argument('--merge-sha',help='full 40-hex merge commit SHA (required for MERGED)'); st.add_argument('--note',default='')
    am=ts.add_parser('amend',help='correct the merge SHA of a MERGED/VERIFIED task (audited; requires --note)'); am.add_argument('id'); am.add_argument('--merge-sha',required=True); am.add_argument('--note',required=True)
    ws=ts.add_parser('workspace'); ws.add_argument('id'); ws.add_argument('--branch',default=''); ws.add_argument('--worktree',default=''); ws.add_argument('--head-sha',default=''); ws.add_argument('--note',default='')
    ul=ts.add_parser('unlock'); ul.add_argument('id'); ul.add_argument('--reason',required=True); ul.add_argument('--force',action='store_true')
    args=parser.parse_args(); root=root_path(args.root)
    try:
        if args.cmd=='init': init(root)
        elif args.cmd=='backup': backup(root)
        elif args.taskcmd=='add': task_add(root,args)
        elif args.taskcmd=='list': task_list(root,args)
        elif args.taskcmd=='show': task_show(root,args.id)
        elif args.taskcmd=='ready': task_ready(root,args.id)
        elif args.taskcmd=='dispatch': task_dispatch(root,args.id)
        elif args.taskcmd=='status': task_status(root,args)
        elif args.taskcmd=='amend': task_amend(root,args)
        elif args.taskcmd=='workspace': set_workspace(root,args)
        elif args.taskcmd=='unlock': unlock(root,args)
    except Exception as e:
        print(f'ERROR: {e}',file=sys.stderr); return 1
    return 0
if __name__=='__main__': raise SystemExit(main())
