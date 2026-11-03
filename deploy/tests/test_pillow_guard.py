"""Exact requirements transition through actual Git and Bash; no package install."""
import subprocess
import unittest
import test_registry_guard as registry

ROOT=registry.ROOT
OLD='384fc51190b284c4dd1ed4efa321bdcb7284bb46'
NEW='5b606d55a0ba9a793ca91248e971598ab9ffaaca'

@unittest.skipUnless(registry.BASH,'Bash required')
class PillowGuardTests(unittest.TestCase):
    git=registry.RegistryGuardTests.git
    commit=registry.RegistryGuardTests.commit
    guard=registry.RegistryGuardTests.guard
    def setUp(self):
        registry.RegistryGuardTests.setUp(self)
        self.req=self.repo/'requirements.txt'
        self.old_req=subprocess.check_output(['git','-C',str(ROOT),'show','aaf2a30983822db9e1dc6c4778891633e3adb699:requirements.txt'])
        self.new_req=self.old_req+b'Pillow==12.3.0\n'
        self.req.write_bytes(self.old_req);self.old_head=self.commit()
    def target(self,data=None):
        self.req.write_bytes(self.new_req if data is None else data)
        return self.commit()
    def test_exact_pair_and_following_release(self):
        target=self.target()
        self.assertEqual(OLD,self.git('rev-parse',self.old_head+':requirements.txt'))
        self.assertEqual(NEW,self.git('rev-parse',target+':requirements.txt'))
        self.guard(self.old_head,target,True)
        self.guard(target,self.commit(),True)
        self.guard(target,self.old_head,False)
    def test_other_target_and_source_blobs_are_rejected(self):
        for data in (self.new_req+b'# extra\n',self.old_req+b'Pillow==12.2.0\n',self.new_req+b'aiohttp==3.13.6\n'):
            self.guard(self.old_head,self.target(data),False)
        unknown=self.target(self.old_req+b'\n')
        self.guard(unknown,self.target(),False)
    def test_database_changes_cannot_be_combined_even_with_pr31_pair(self):
        self.file.write_bytes(self.new)
        self.guard(self.old_head,self.target(),False)
        self.file.write_bytes(self.old)
        (self.file.parent/'migration.py').write_text('unexpected')
        self.guard(self.old_head,self.target(),False)
    def test_target_mode_delete_symlink_are_rejected(self):
        self.target()
        for mode,blob in (('100755',NEW),('120000',self.git('hash-object','-w','requirements.txt'))):
            self.git('update-index','--cacheinfo',mode+','+blob+',requirements.txt')
            self.git('commit','-qm','synthetic mode')
            self.guard(self.old_head,self.git('rev-parse','HEAD'),False)
        self.git('update-index','--force-remove','requirements.txt')
        self.git('commit','-qm','synthetic delete')
        self.guard(self.old_head,self.git('rev-parse','HEAD'),False)
    def test_source_mode_is_also_checked(self):
        self.git('update-index','--chmod=+x','requirements.txt')
        self.git('commit','-qm','source mode');source=self.git('rev-parse','HEAD')
        self.req.write_bytes(self.new_req);self.git('add','requirements.txt')
        self.git('update-index','--chmod=-x','requirements.txt');self.git('commit','-qm','normal target')
        self.guard(source,self.git('rev-parse','HEAD'),False)
    def test_git_failure_is_not_permission(self):
        self.target()
        code=self.harness.read_text(encoding='utf-8').replace('command "$REAL_GIT" -C "$FIXTURE" "$@"','return 2')
        self.harness.write_text(code,encoding='utf-8',newline='\n')
        self.guard(self.old_head,self.git('rev-parse','HEAD'),False)
