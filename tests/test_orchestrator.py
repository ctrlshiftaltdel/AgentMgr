import json, sqlite3, tempfile, unittest
from pathlib import Path
from unittest import mock
import importlib.util

SCRIPT=Path(__file__).resolve().parents[1]/'scripts'/'orchestrator.py'
spec=importlib.util.spec_from_file_location('orch',SCRIPT)
orch=importlib.util.module_from_spec(spec); spec.loader.exec_module(orch)

class LocalOrchestratorTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.root=Path(self.tmp.name); orch.init(self.root)
    def tearDown(self): self.tmp.cleanup()
    def add(self,tid,paths,depends=None):
        class A: pass
        a=A(); a.id=tid; a.title=tid; a.description=''; a.paths=paths; a.depends=depends or []; a.acceptance=['works']
        orch.task_add(self.root,a)
    def test_parent_child_lock_conflict(self):
        self.add('TASK-0001',['src/']); self.add('TASK-0002',['src/main.py'])
        orch.task_ready(self.root,'TASK-0001')
        class A: pass
        a=A(); a.active_only=False
        orch.task_dispatch(self.root,'TASK-0001')
        with self.assertRaises(RuntimeError): orch.task_ready(self.root,'TASK-0002')
    def test_independent_paths_can_dispatch(self):
        self.add('TASK-0001',['src/a.py']); self.add('TASK-0002',['src/b.py'])
        orch.task_ready(self.root,'TASK-0001'); orch.task_dispatch(self.root,'TASK-0001')
        orch.task_ready(self.root,'TASK-0002'); orch.task_dispatch(self.root,'TASK-0002')
    def test_dependency_must_be_verified(self):
        self.add('TASK-0001',['a.py']); self.add('TASK-0002',['b.py'],['TASK-0001'])
        with self.assertRaises(RuntimeError): orch.task_ready(self.root,'TASK-0002')
    def test_retry_cap(self):
        self.add('TASK-0001',['a.py']); orch.task_ready(self.root,'TASK-0001'); orch.task_dispatch(self.root,'TASK-0001')
        class A: pass
        for dest in ['IN_PROGRESS','REVIEW_READY','MASTER_REVIEW','IN_PROGRESS','REVIEW_READY','MASTER_REVIEW','IN_PROGRESS','REVIEW_READY','MASTER_REVIEW','IN_PROGRESS','REVIEW_READY','MASTER_REVIEW']:
            x=A(); x.id='TASK-0001'; x.status=dest; x.head_sha=None; x.note=''; orch.task_status(self.root,x)
        x=A(); x.id='TASK-0001'; x.status='IN_PROGRESS'; x.head_sha=None; x.note=''; orch.task_status(self.root,x)
        con=orch.connect(self.root)
        try: self.assertEqual(con.execute('SELECT status FROM tasks WHERE id=?',('TASK-0001',)).fetchone()['status'],'BLOCKED')
        finally: con.close()
    def test_full_approval_merge_verification_and_lock_release(self):
        self.add('TASK-0001',['src/app.py'])
        orch.task_ready(self.root,'TASK-0001'); orch.task_dispatch(self.root,'TASK-0001')
        class A: pass
        def go(status, head_sha=None, merge_sha=None, note=''):
            a=A(); a.id='TASK-0001'; a.status=status; a.head_sha=head_sha; a.merge_sha=merge_sha; a.note=note or ('evidence recorded' if status in {'APPROVED','MERGED','VERIFIED','BLOCKED','NEEDS_HUMAN_DECISION','FAILED','CANCELED'} else '')
            orch.task_status(self.root,a)
        go('IN_PROGRESS')
        go('REVIEW_READY',head_sha='abc123')
        go('MASTER_REVIEW')
        go('APPROVED',head_sha='abc123')
        go('MERGED',merge_sha='d'*40)
        go('VERIFIED')
        con=orch.connect(self.root)
        try:
            row=con.execute('SELECT status,head_sha,approved_sha,merged_sha FROM tasks WHERE id=?',('TASK-0001',)).fetchone()
            self.assertEqual(row['status'],'VERIFIED')
            self.assertEqual(row['head_sha'],'abc123')
            self.assertEqual(row['approved_sha'],'abc123')
            self.assertEqual(row['merged_sha'],'d'*40)
            self.assertEqual(con.execute('SELECT COUNT(*) FROM locks WHERE task_id=?',('TASK-0001',)).fetchone()[0],0)
        finally: con.close()

    def test_normalize_rejects_parent_path(self):
        with self.assertRaises(ValueError): orch.normalize_path('../secret')
        with self.assertRaises(ValueError): orch.normalize_path('src/**/*.py')
        with self.assertRaises(ValueError): orch.normalize_path('C:\\private\\file.txt')

    # --- Shared helpers for acceptance-gate tests ---
    def advance(self, tid, dest, head_sha=None, merge_sha=None, note=''):
        class A: pass
        a=A(); a.id=tid; a.status=dest; a.head_sha=head_sha; a.merge_sha=merge_sha
        a.note=note or ('evidence recorded' if dest in {'APPROVED','MERGED','VERIFIED','BLOCKED','NEEDS_HUMAN_DECISION','FAILED','CANCELED'} else '')
        orch.task_status(self.root,a)
    def workspace(self, tid, head_sha='', branch='', worktree='', note=''):
        class A: pass
        a=A(); a.id=tid; a.head_sha=head_sha; a.branch=branch; a.worktree=worktree; a.note=note
        orch.set_workspace(self.root,a)
    def count(self, sql, params=()):
        con=orch.connect(self.root)
        try: return con.execute(sql,params).fetchone()[0]
        finally: con.close()
    def drive_to_master_review(self, tid, path, head_sha):
        self.add(tid,[path]); orch.task_ready(self.root,tid); orch.task_dispatch(self.root,tid)
        self.advance(tid,'IN_PROGRESS'); self.advance(tid,'REVIEW_READY',head_sha=head_sha)
        self.advance(tid,'MASTER_REVIEW')
    def drive_to_approved(self, tid, path, head_sha):
        self.drive_to_master_review(tid,path,head_sha); self.advance(tid,'APPROVED',head_sha=head_sha)

    # Gate B: dispatch only from a READY activity state.
    def test_non_ready_dispatch_is_refused(self):
        self.add('TASK-0101',['src/'])
        with self.assertRaises(RuntimeError) as cm: orch.task_dispatch(self.root,'TASK-0101')
        self.assertIn('only READY tasks can be dispatched',str(cm.exception))
        self.assertEqual(self.count('SELECT COUNT(*) FROM locks WHERE task_id=?',('TASK-0101',)),0)
        con=orch.connect(self.root)
        try: self.assertEqual(con.execute('SELECT status FROM tasks WHERE id=?',('TASK-0101',)).fetchone()['status'],'PLANNED')
        finally: con.close()

    # Gate C: APPROVED must be bound to the exact current head SHA.
    def test_approved_head_sha_mismatch_is_refused(self):
        head='a'*40; other='b'*40
        self.drive_to_master_review('TASK-0102','src/',head)
        with self.assertRaises(RuntimeError) as cm: self.advance('TASK-0102','APPROVED',head_sha=other)
        self.assertIn('does not match current task head',str(cm.exception))
        self.advance('TASK-0102','APPROVED',head_sha=head)
        con=orch.connect(self.root)
        try:
            row=con.execute('SELECT status,approved_sha FROM tasks WHERE id=?',('TASK-0102',)).fetchone()
            self.assertEqual(row['status'],'APPROVED')
            self.assertEqual(row['approved_sha'],head)
        finally: con.close()

    # Gate C: MERGED is not allowed without the resulting merge commit SHA.
    def test_merged_without_merge_sha_is_refused(self):
        head='c'*40
        self.drive_to_approved('TASK-0103','src/',head)
        with self.assertRaises(RuntimeError) as cm: self.advance('TASK-0103','MERGED',note='merge ready')
        self.assertIn('MERGED requires --merge-sha',str(cm.exception))
        con=orch.connect(self.root)
        try: self.assertEqual(con.execute('SELECT status FROM tasks WHERE id=?',('TASK-0103',)).fetchone()['status'],'APPROVED')
        finally: con.close()

    # Gate C: any head change after APPROVED must invalidate the approval.
    def test_head_change_invalidates_approval(self):
        head='d'*40; new='e'*40
        self.drive_to_approved('TASK-0104','src/',head)
        self.workspace('TASK-0104',head_sha=new)
        con=orch.connect(self.root)
        try:
            row=con.execute('SELECT status,approved_sha FROM tasks WHERE id=?',('TASK-0104',)).fetchone()
            self.assertEqual(row['status'],'MASTER_REVIEW')
            self.assertEqual(row['approved_sha'],'')
            hit=con.execute("SELECT COUNT(*) FROM events WHERE task_id=? AND action='APPROVAL_INVALIDATED'",('TASK-0104',)).fetchone()[0]
            self.assertEqual(hit,1)
        finally: con.close()

    # Gate B: FAILED keeps the lock; only an explicit reasoned unlock releases it.
    def test_failed_keeps_lock_and_force_unlock(self):
        self.add('TASK-0105',['src/app.py']); orch.task_ready(self.root,'TASK-0105'); orch.task_dispatch(self.root,'TASK-0105')
        self.advance('TASK-0105','FAILED',note='simulated crash; state unknown')
        self.assertEqual(self.count('SELECT COUNT(*) FROM locks WHERE task_id=?',('TASK-0105',)),1)
        class A: pass
        u=A(); u.id='TASK-0105'; u.reason='cleanup'; u.force=False
        with self.assertRaises(RuntimeError) as cm: orch.unlock(self.root,u)
        self.assertIn('use --force with a reason',str(cm.exception))
        u=A(); u.id='TASK-0105'; u.reason='execution confirmed stopped; releasing'; u.force=True
        orch.unlock(self.root,u)
        self.assertEqual(self.count('SELECT COUNT(*) FROM locks WHERE task_id=?',('TASK-0105',)),0)
        con=orch.connect(self.root)
        try:
            row=con.execute("SELECT data_json FROM events WHERE task_id=? AND action='MANUAL_UNLOCK'",('TASK-0105',)).fetchone()
            self.assertIsNotNone(row)
            self.assertTrue(json.loads(row['data_json'])['force'])
        finally: con.close()

    # Gate B: a multi-path task with any single conflict is refused entirely; no half locks.
    def test_multi_path_conflict_refuses_whole_dispatch(self):
        self.add('TASK-0106-A',['src/','docs/']); self.add('TASK-0106-B',['docs/x.md','tests/'])
        orch.task_ready(self.root,'TASK-0106-A'); orch.task_ready(self.root,'TASK-0106-B')
        orch.task_dispatch(self.root,'TASK-0106-A')
        with self.assertRaises(RuntimeError) as cm: orch.task_dispatch(self.root,'TASK-0106-B')
        self.assertIn('path conflict',str(cm.exception))
        self.assertEqual(self.count('SELECT COUNT(*) FROM locks WHERE task_id=?',('TASK-0106-B',)),0)
        self.assertEqual(self.count('SELECT COUNT(*) FROM locks WHERE task_id=?',('TASK-0106-A',)),2)

    # Gate A: task add rejects self dependency and unknown dependency without dirty rows.
    def test_task_add_rejects_self_and_missing_dependency(self):
        with self.assertRaises(ValueError) as cm: self.add('TASK-0107',['src/'],['TASK-0107'])
        self.assertIn('cannot depend on itself',str(cm.exception))
        with self.assertRaises(RuntimeError) as cm: self.add('TASK-0108',['src/'],['TASK-9999'])
        self.assertIn('dependency not found',str(cm.exception))
        self.assertEqual(self.count('SELECT COUNT(*) FROM tasks WHERE id=?',('TASK-0107',)),0)
        self.assertEqual(self.count('SELECT COUNT(*) FROM tasks WHERE id=?',('TASK-0108',)),0)

    # --- review-fix retry cap configurable via <root>/.orchestration/config.json ---
    def test_retry_cap_is_configurable_from_config(self):
        self.add('TASK-0201',['cfg.py']); orch.task_ready(self.root,'TASK-0201'); orch.task_dispatch(self.root,'TASK-0201')
        (self.root/'.orchestration'/'config.json').write_text(json.dumps({'retry_cap':1}),encoding='utf-8')
        self.assertEqual(orch.read_retry_cap(self.root),1)
        self.advance('TASK-0201','IN_PROGRESS')
        self.advance('TASK-0201','REVIEW_READY'); self.advance('TASK-0201','MASTER_REVIEW')
        # With cap 1 the first MASTER_REVIEW -> IN_PROGRESS is allowed (retries 0 -> 1).
        self.advance('TASK-0201','IN_PROGRESS')
        self.assertEqual(self.count('SELECT retries FROM tasks WHERE id=?',('TASK-0201',)),1)
        self.advance('TASK-0201','REVIEW_READY'); self.advance('TASK-0201','MASTER_REVIEW')
        # The second round must hit the configured cap and divert to BLOCKED.
        self.advance('TASK-0201','IN_PROGRESS',note='round 2')
        con=orch.connect(self.root)
        try: self.assertEqual(con.execute('SELECT status FROM tasks WHERE id=?',('TASK-0201',)).fetchone()['status'],'BLOCKED')
        finally: con.close()

    def test_retry_cap_defaults_for_missing_or_invalid_config(self):
        cfg=self.root/'.orchestration'/'config.json'
        self.assertEqual(orch.read_retry_cap(self.root),3)  # init wrote the default
        cfg.unlink()                                        # missing file
        self.assertEqual(orch.read_retry_cap(self.root),3)
        cfg.write_text(json.dumps({'other':5}),encoding='utf-8')  # key missing
        self.assertEqual(orch.read_retry_cap(self.root),3)
        for bad in [0,-1,'3',2.5,True,None]:
            cfg.write_text(json.dumps({'retry_cap':bad}),encoding='utf-8')
            self.assertEqual(orch.read_retry_cap(self.root),3,bad)
        cfg.write_text('{ not json',encoding='utf-8')       # malformed file
        self.assertEqual(orch.read_retry_cap(self.root),3)

    def test_retry_cap_missing_config_keeps_default_budget(self):
        (self.root/'.orchestration'/'config.json').unlink()
        self.add('TASK-0202',['cfg2.py']); orch.task_ready(self.root,'TASK-0202'); orch.task_dispatch(self.root,'TASK-0202')
        # 4 review rounds: the first IN_PROGRESS is DISPATCHED->IN_PROGRESS and does
        # not consume the retry budget, so retries reaches the default cap of 3 only
        # after the fourth IN_PROGRESS from MASTER_REVIEW.
        for dest in ['IN_PROGRESS','REVIEW_READY','MASTER_REVIEW']*4:
            self.advance('TASK-0202',dest)
        self.assertEqual(self.count('SELECT retries FROM tasks WHERE id=?',('TASK-0202',)),3)
        self.advance('TASK-0202','IN_PROGRESS')
        con=orch.connect(self.root)
        try: self.assertEqual(con.execute('SELECT status FROM tasks WHERE id=?',('TASK-0202',)).fetchone()['status'],'BLOCKED')
        finally: con.close()

    # --- MERGED requires a full 40-hex merge commit SHA ---
    def test_merged_requires_full_40_hex_merge_sha(self):
        head='a'*40
        self.drive_to_approved('TASK-0301','one.py',head)
        with self.assertRaises(RuntimeError) as cm:
            self.advance('TASK-0301','MERGED',merge_sha='def456',note='merge ready')
        self.assertIn('40',str(cm.exception))
        self.assertEqual(self.count("SELECT merged_sha FROM tasks WHERE id=?",('TASK-0301',)),'')
        con=orch.connect(self.root)
        try: self.assertEqual(con.execute('SELECT status FROM tasks WHERE id=?',('TASK-0301',)).fetchone()['status'],'APPROVED')
        finally: con.close()
        # A valid 40-hex value is accepted (mixed case normalised to lower case).
        self.advance('TASK-0301','MERGED',merge_sha='B'*40,note='merge ready')
        self.assertEqual(self.count("SELECT merged_sha FROM tasks WHERE id=?",('TASK-0301',)),'b'*40)

    # --- task amend corrects merge_sha and records a RECORD_AMENDED event ---
    def amend(self, tid, merge_sha=None, note=''):
        class A: pass
        a=A(); a.id=tid; a.merge_sha=merge_sha; a.note=note
        orch.task_amend(self.root,a)
    def merge(self, tid, path, head, merge_sha):
        self.drive_to_approved(tid,path,head)
        self.advance(tid,'MERGED',merge_sha=merge_sha,note='merge ready')
    def test_amend_corrects_merge_sha_and_records_event(self):
        old='a'*40; new='b'*40; third='c'*40
        self.merge('TASK-0302','two.py',old,old)
        self.amend('TASK-0302',merge_sha=new,note='backfill full SHA after short value')
        self.assertEqual(self.count("SELECT merged_sha FROM tasks WHERE id=?",('TASK-0302',)),new)
        # VERIFIED is also a valid amendment target.
        self.advance('TASK-0302','VERIFIED',note='verification complete')
        self.amend('TASK-0302',merge_sha=third,note='second correction')
        self.assertEqual(self.count("SELECT merged_sha FROM tasks WHERE id=?",('TASK-0302',)),third)
        con=orch.connect(self.root)
        try:
            rows=con.execute("SELECT note,data_json FROM events WHERE task_id=? AND action='RECORD_AMENDED' ORDER BY id",('TASK-0302',)).fetchall()
            self.assertEqual(len(rows),2)
            self.assertIn('backfill',rows[0]['note'])
            first=json.loads(rows[0]['data_json'])
            self.assertEqual(first['old'],old); self.assertEqual(first['new'],new)
            second=json.loads(rows[1]['data_json'])
            self.assertEqual(second['old'],new); self.assertEqual(second['new'],third)
        finally: con.close()
    def test_amend_rejects_non_terminal_missing_note_and_bad_sha(self):
        head='c'*40
        # A non-terminal task cannot be amended.
        self.drive_to_approved('TASK-0303','three.py',head)
        with self.assertRaises(RuntimeError) as cm:
            self.amend('TASK-0303',merge_sha='e'*40,note='too early')
        self.assertIn('MERGED',str(cm.exception))
        self.assertEqual(self.count("SELECT merged_sha FROM tasks WHERE id=?",('TASK-0303',)),'')
        # --note is mandatory.
        self.merge('TASK-0304','four.py',head,head)
        with self.assertRaises(RuntimeError) as cm:
            self.amend('TASK-0304',merge_sha='e'*40)
        self.assertIn('--note',str(cm.exception))
        self.assertEqual(self.count("SELECT merged_sha FROM tasks WHERE id=?",('TASK-0304',)),head)
        # The SHA must be a full 40-hex string.
        with self.assertRaises(RuntimeError) as cm:
            self.amend('TASK-0304',merge_sha='abc123',note='short')
        self.assertIn('40',str(cm.exception))
        with self.assertRaises(RuntimeError):
            self.amend('TASK-0304',merge_sha='z'*40,note='non-hex')
        self.assertEqual(self.count("SELECT merged_sha FROM tasks WHERE id=?",('TASK-0304',)),head)

    # --- a failing BEGIN must not be masked by a spurious ROLLBACK ---
    def test_begin_failure_propagates_original_error(self):
        real=orch.connect(self.root)
        class BeginFails:
            def __init__(self,con): self._c=con
            @property
            def in_transaction(self): return self._c.in_transaction
            def execute(self,sql,*a,**k):
                if sql.strip().upper().startswith('BEGIN'):
                    raise sqlite3.OperationalError('database is locked')
                return self._c.execute(sql,*a,**k)
            def close(self): self._c.close()
            def __getattr__(self,name): return getattr(self._c,name)
        self.add('TASK-0401',['lock.py']); orch.task_ready(self.root,'TASK-0401'); orch.task_dispatch(self.root,'TASK-0401')
        with mock.patch.object(orch,'connect',return_value=BeginFails(real)):
            with self.assertRaises(sqlite3.OperationalError) as cm:
                self.advance('TASK-0401','IN_PROGRESS')
        msg=str(cm.exception)
        self.assertIn('database is locked',msg)
        self.assertNotIn('cannot rollback',msg)

if __name__=='__main__': unittest.main()
