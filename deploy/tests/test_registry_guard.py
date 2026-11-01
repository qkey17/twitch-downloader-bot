"""Real Git blobs + actual Bash guard, offline. No application dependencies."""
import os
from pathlib import Path
import subprocess
import shutil
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT/'tests'))
from test_oracle_deployment import BASH, SCRIPT, shell_path


@unittest.skipUnless(BASH, 'Bash required')
class RegistryGuardTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory(prefix='guard-')
        self.addCleanup(temp.cleanup)
        self.repo=Path(temp.name)
        self.git('init','-q')
        self.git('config','user.name','Synthetic')
        self.git('config','user.email','synthetic@example.invalid')
        self.git('config','core.autocrlf','false')
        self.git('config','core.hooksPath',str(self.repo/'no-hooks'))
        self.file=self.repo/'src/database/db_results.py'
        self.file.parent.mkdir(parents=True)
        self.old=subprocess.check_output(['git','-C',str(ROOT),'show','264be7c0454769f741c6ffd3d2bf72ea0eae20fa:src/database/db_results.py'])
        self.new=(ROOT/'src/database/db_results.py').read_bytes().replace(b'\r\n',b'\n')
        self.file.write_bytes(self.old)
        self.before=self.commit()
        source=SCRIPT.read_text(encoding='utf-8').removesuffix('main "$@"\n')
        self.harness=self.repo/'guard.sh'
        self.harness.write_text(source+'''
git_cmd() { command "$REAL_GIT" -C "$FIXTURE" "$@"; }
previous=$1; target=$2
check_database_transition
''',encoding='utf-8',newline='\n')

    def git(self,*args):
        return subprocess.check_output(['git','-C',str(self.repo),*args],stderr=subprocess.PIPE).decode().strip()

    def commit(self):
        self.git('add','src')
        if (self.repo/'requirements.txt').exists():self.git('add','requirements.txt')
        self.git('commit','-qm','synthetic','--allow-empty')
        return self.git('rev-parse','HEAD')

    def guard(self,before,after,allowed):
        p=subprocess.run([BASH,'--noprofile','--norc',shell_path(self.harness),before,after],
                         env=os.environ|{'FIXTURE':shell_path(self.repo),'REAL_GIT':shell_path(shutil.which('git'))},capture_output=True,text=True,timeout=15)
        self.assertEqual(allowed,p.returncode==0,p.stderr)

    def test_exact_pair_and_later_head_with_same_blob_are_allowed(self):
        self.assertEqual('220c8817cb1e473b60ccae3bb57f615bb44af48e',self.git('rev-parse',self.before+':src/database/db_results.py'))
        self.file.write_bytes(self.new);after=self.commit()
        self.assertEqual('56647c94ac7d865b115613050f4c62041788cdfd',self.git('rev-parse',after+':src/database/db_results.py'))
        self.guard(self.before,after,True)
        self.guard(self.before,self.commit(),True)
        self.guard(after,self.commit(),True)  # Ordinary subsequent release.
        self.guard(after,self.before,False)  # No reverse-blob bypass.

    def test_unexpected_content_and_schema_change_are_blocked(self):
        for content in (self.new+b'\n# unreviewed\n',self.new.replace(b'CREATE TABLE',b'CREATE TABLE changed')):
            self.file.write_bytes(content);self.guard(self.before,self.commit(),False)

    def test_other_database_file_requirements_and_mode_change_are_blocked(self):
        self.file.write_bytes(self.new);allowed=self.commit()
        extra=self.repo/'src/database/migration.py';extra.write_text('migration')
        self.guard(self.before,self.commit(),False)
        extra.unlink();self.commit()
        requirements=self.repo/'requirements.txt';requirements.write_text('changed')
        self.guard(self.before,self.commit(),False)
        self.guard(allowed,self.git('rev-parse','HEAD'),False)

    def test_mode_change_and_unknown_source_cannot_use_exception(self):
        self.file.write_bytes(self.old+b'\n# another source\n');unknown=self.commit()
        self.file.write_bytes(self.new);after=self.commit()
        self.guard(unknown,after,False)
        self.git('update-index','--chmod=+x','src/database/db_results.py')
        self.git('commit','-qm','synthetic mode')
        self.guard(self.before,self.git('rev-parse','HEAD'),False)

    def test_ordinary_release_and_shell_syntax(self):
        (self.repo/'src/ordinary.py').write_text('ordinary')
        self.guard(self.before,self.commit(),True)
        p=subprocess.run([BASH,'-n',shell_path(SCRIPT)],capture_output=True)
        self.assertEqual(0,p.returncode,p.stderr)


if __name__=='__main__':unittest.main()
