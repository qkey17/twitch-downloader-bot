"""Stable lease identity across publication, cleanup, recovery and processes."""
import os
import subprocess
import sys
import unittest

import test_result_registry as fixture
from src import html_cleanup as cleanup
from src.database import db_results as results


class StableLeaseTests(unittest.TestCase):
    plan=fixture.ResultRegistryTestCase.plan
    publish=fixture.ResultRegistryTestCase.publish
    ready=fixture.ResultRegistryTestCase.ready
    rows=fixture.ResultRegistryTestCase.rows
    change=fixture.ResultRegistryTestCase.change
    setUp=fixture.ResultRegistryTestCase.setUp

    def test_close_cleanup_and_recovery_keep_same_lock_inode(self):
        plan=self.plan(mode='public')
        self.publish(plan);plan.complete()
        path=results._lease_path(plan.id);identity=path.stat().st_ino
        plan.close();plan.close()
        row=self.rows()[0]
        with cleanup._result_lease(row) as handle:
            self.assertIsNotNone(handle)
        results.recover_results()
        self.assertTrue(cleanup._attempt(row))
        self.assertEqual(identity,path.stat().st_ino)
        handle=results._recovery_lease(plan.id);self.assertIsNotNone(handle);handle.close()

    def test_recovered_and_orphan_markers_remain_stable(self):
        plan=self.plan();self.publish(plan);plan.close()
        path=results._lease_path(plan.id);identity=path.stat().st_ino
        key='f'*32;orphan=results._recovery_lease(key);orphan.close()
        orphan_path=results._lease_path(key);orphan_inode=orphan_path.stat().st_ino
        for _ in range(2):results.recover_results()
        self.assertEqual('ready',self.rows()[0]['state'])
        self.assertEqual(identity,path.stat().st_ino)
        self.assertEqual(orphan_inode,orphan_path.stat().st_ino)

    @unittest.skipUnless(os.name=='posix','Real POSIX flock/inode race; runs in Linux CI')
    def test_waiting_process_keeps_same_inode_after_publication_close(self):
        plan=self.plan(mode='public');self.publish(plan);plan.complete()
        path=results._lease_path(plan.id);identity=path.stat().st_ino
        # Child opens BEFORE close, then locks AFTER close. Old close/unlink
        # would allow the parent/cleanup to lock a newly created second inode.
        code="""import sys,os
from src.file_lock import try_lock_file
with open(sys.argv[1],'r+b') as handle:
 print('opened',flush=True)
 sys.stdin.readline()
 assert try_lock_file(handle)
 print(os.fstat(handle.fileno()).st_ino,flush=True)
 sys.stdin.readline()
"""
        child=subprocess.Popen([sys.executable,'-B','-c',code,str(path)],stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        try:
            self.assertEqual('opened',child.stdout.readline().strip())
            plan.close()
            child.stdin.write('lock\n');child.stdin.flush()
            self.assertEqual(str(identity),child.stdout.readline().strip())
            self.assertIsNone(results._recovery_lease(plan.id))
            self.assertFalse(cleanup._attempt(self.rows()[0]))
            results.recover_results()
            self.assertTrue(plan.path().exists())
            self.assertEqual(identity,path.stat().st_ino)
        finally:
            child.stdin.write('exit\n');child.stdin.flush()
            child.communicate(timeout=5)
        self.assertEqual(0,child.returncode)
        self.assertTrue(cleanup._attempt(self.rows()[0]))

    def test_marker_symlink_or_foreign_file_is_not_modified(self):
        key='e'*32;path=results._lease_path(key);path.parent.mkdir(parents=True,exist_ok=True)
        path.write_bytes(b'foreign')
        self.assertIsNone(results._recovery_lease(key))
        self.assertEqual(b'foreign',path.read_bytes())
        path.unlink()
        target=self.root/'target';target.write_bytes(b'keep')
        try:path.symlink_to(target)
        except OSError:self.skipTest('OS does not permit symlinks')
        self.assertIsNone(results._recovery_lease(key))
        self.assertEqual(b'keep',target.read_bytes())
