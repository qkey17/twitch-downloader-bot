"""Offsite orchestration uses fake age/rclone and a synthetic sealed snapshot."""
from contextlib import contextmanager
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

import test_backup_restore as fixture
from tools import backup_restore as snapshots, offsite_backup as offsite


def dropbox_hash(data):
    return hashlib.sha256(b''.join(hashlib.sha256(data[i:i+4*1024**2]).digest()
                                  for i in range(0,len(data),4*1024**2))).hexdigest()


class Cloud:
    def __init__(self):
        self.files, self.events, self.next_id = {}, [], 0
        self.quota = 2000000000
        self.fail = None
        self.after_upload = self.after_move = False
        self.empty_report = False
        self.before_delete = None
        self.before_move = None
        self.before_check = None
        self.sizeless = 0

    def add(self, name, data):
        self.next_id += 1
        self.files[name] = dict(data=data, id='id:test'+str(self.next_id))

    def metadata(self, name):
        item = self.files[name]
        return dict(Path=name, Name=name, IsDir=False, Size=len(item['data']), ID=item['id'],
                    Hashes={'dropbox':dropbox_hash(item['data'])}, ModTime='2026-10-27T00:00:00Z')

    def command(self, argv):
        command = argv[1]
        stage = command
        if command == 'backend': stage = 'features'
        if command == 'copyto': stage = 'dry-run' if '--dry-run' in argv else 'upload'
        if command == 'moveto': stage = 'move'
        if command == 'deletefile': stage = 'delete'
        self.events.append((stage, list(argv)))
        if self.fail == stage:
            raise offsite.OffsiteError('Synthetic external failure')
        if command == 'version': return b'rclone v1.60.1\n'
        if command == '--version': return b'1.1.1\n'
        if command == 'backend':
            remote = argv[3]
            name, root = remote.split(':',1)
            result = dict(Name=name,Root=root,String=f"Dropbox root '{root}'",Hashes=['dropbox'],
                          Features=dict(Move=True,About=True,CaseInsensitive=True))
        elif command == 'about': result = dict(free=self.quota-sum(len(v['data']) for v in self.files.values()))
        elif command == 'size': result = dict(bytes=sum(len(v['data']) for v in self.files.values()),
                                               count=len(self.files),sizeless=self.sizeless)
        elif command == 'lsjson':
            if '--stat' in argv:
                name = argv[2].rsplit('/',1)[-1]
                if name not in self.files: raise offsite.OffsiteError('Missing cloud object')
                result = self.metadata(name)
            else: result = [self.metadata(name) for name in self.files]
        elif command == 'copyto':
            self.assert_flags(argv)
            if '--dry-run' not in argv:
                name = argv[3].rsplit('/',1)[-1]
                if name not in self.files: self.add(name, Path(argv[2]).read_bytes())
                if self.after_upload:
                    self.after_upload = False
                    raise offsite.OffsiteError('Synthetic lost upload response')
            return b''
        elif command == 'moveto':
            self.assert_flags(argv)
            source, target = [p.rsplit('/',1)[-1] for p in argv[2:4]]
            if self.before_move: self.before_move(target)
            if target not in self.files:
                self.files[target] = self.files.pop(source)
            if self.after_move:
                self.after_move = False
                raise offsite.OffsiteError('Synthetic lost move response')
            return b''
        elif command == 'check':
            assert '--download' in argv and '--one-way' in argv
            name = argv[argv.index('--include')+1].removeprefix('/')
            if self.before_check: self.before_check(name)
            if name not in self.files or (Path(argv[2])/name).read_bytes() != self.files[name]['data']:
                raise offsite.OffsiteError('Synthetic full-content mismatch')
            return b'' if self.empty_report else ('= '+name+'\n').encode()
        elif command == 'deletefile':
            assert any(stage == 'check' for stage,_ in self.events), 'Deletion before a verified upload'
            assert argv[argv.index('--max-delete')+1] == '1'
            name = argv[2].rsplit('/',1)[-1]
            if self.before_delete: self.before_delete(name)
            del self.files[name]
            return b''
        else: raise AssertionError('Unexpected external command')
        return json.dumps(result).encode()

    @staticmethod
    def assert_flags(argv):
        assert '--ignore-existing' in argv and '--immutable' in argv
        assert argv[argv.index('--max-delete')+1] == '0'


class FakeExternal(offsite.External):
    PREFIX = b'TEST-AGE-NOT-REAL-ENCRYPTION\x00'

    def __init__(self, directory, limits, cloud):
        super().__init__(directory,limits)
        self.cloud, self.encryptions = cloud, 0

    def run(self, argv):
        self.check()
        return self.cloud.command(argv)

    @contextmanager
    def process(self, argv, *, stdin=None, stdout=None, monitor=None):
        assert argv == ['age','--encrypt','--recipient',offsite.RECIPIENT]
        assert stdin == subprocess.PIPE
        self.encryptions += 1
        stdout.write(self.PREFIX)
        parent = self
        class Input:
            closed = False
            def write(self, data):
                if parent.cloud.fail == 'encrypt': raise BrokenPipeError('Synthetic age failure')
                return stdout.write(data)
            def flush(self): pass
            def close(self): self.closed = True
        class Process:
            stdin = Input()
        yield Process()
        assert Process.stdin.closed
        if monitor: monitor()


class OffsiteTests(unittest.TestCase):
    setUp_fixture = fixture.BackupRestoreTests.setUp
    plan = fixture.BackupRestoreTests.plan
    publish = fixture.BackupRestoreTests.publish
    ready = fixture.BackupRestoreTests.ready
    rows = fixture.BackupRestoreTests.rows
    change = fixture.BackupRestoreTests.change
    setUp_base = fixture.BackupRestoreTests.setUp_fixture
    create = fixture.BackupRestoreTests.create

    def setUp(self):
        # Reuse the real synthetic TDB schema/publication fixture, not a made-up DB validator.
        self.setUp_fixture = self.setUp_base
        fixture.BackupRestoreTests.setUp(self)
        self.ready()
        self.snapshot = self.create()
        self.state = self.root/'offsite-state'
        self.cloud = Cloud()
        self.cloud.add('first-snapshot.tar.gz.age',b'pre-existing independent good copy')
        self.cloud.add('foreign.txt',b'foreign data')
        self.limits = offsite.Limits(local_reserve=1)
        self.runners = []

    def send(self, resume=None, limits=None, state=None):
        state = state or self.state
        runner = FakeExternal(state,limits or self.limits,self.cloud)
        self.runners.append(runner)
        return offsite.send_backup(self.snapshot,state,limits=limits or self.limits,resume=resume,external=runner)

    def jobs(self):
        return offsite._jobs(self.state/'jobs')

    def mutations(self):
        return [(stage,args) for stage,args in self.cloud.events if stage in ('upload','move','delete')]

    def test_streaming_tar_preserves_complete_snapshot_and_never_needs_private_key(self):
        before = {p.relative_to(self.snapshot).as_posix():p.read_bytes() for p in self.snapshot.rglob('*') if p.is_file()}
        result = self.send()
        self.assertTrue(result['cloud_verified'])
        self.assertTrue(self.cloud.events.index(next(e for e in self.cloud.events if e[0]=='upload')) <
                        self.cloud.events.index(next(e for e in self.cloud.events if e[0]=='check')))
        ciphertext = self.cloud.files[result['archive']]['data']
        with tarfile.open(fileobj=io.BytesIO(ciphertext[len(FakeExternal.PREFIX):]),mode='r:gz') as archive:
            restored = {member.name.removeprefix('snapshot/'):archive.extractfile(member).read()
                        for member in archive.getmembers() if member.isfile()}
            self.assertEqual(before,restored)
            self.assertTrue(all(member.isfile() or member.isdir() for member in archive.getmembers()))
        self.assertEqual(before,{p.relative_to(self.snapshot).as_posix():p.read_bytes() for p in self.snapshot.rglob('*') if p.is_file()})
        self.assertEqual(result['sha256'], hashlib.sha256(ciphertext).hexdigest())
        self.assertFalse(list(self.state.rglob('*.tar.gz')))
        self.assertFalse(list(self.state.rglob('*.env')))
        self.assertEqual(1,self.runners[-1].encryptions)
        self.assertEqual(1,sum(stage=='check' for stage,_ in self.cloud.events))
        self.assertEqual('verified',self.jobs()[0][1]['state'])

    def test_verified_resume_neither_reencrypts_nor_reuploads(self):
        result = self.send()
        before = dict(self.cloud.files)
        count = len(self.mutations())
        again = self.send(resume=result['id'])
        self.assertEqual(result,again)
        self.assertEqual(before,self.cloud.files)
        self.assertEqual(count,len(self.mutations()))
        self.assertEqual(0,self.runners[-1].encryptions)

    def test_quota_and_budget_never_delete_old_data_to_make_room(self):
        initial = dict(self.cloud.files)
        self.cloud.quota = self.limits.account_reserve
        with self.assertRaises(offsite.OffsiteError): self.send()
        self.assertEqual([],self.mutations())
        self.assertEqual(initial,self.cloud.files)
        self.cloud.quota = 2000000000
        result = self.send()
        size = result['bytes']
        before = dict(self.cloud.files)
        self.cloud.events.clear()
        limits = offsite.Limits(max_archives=1,budget_bytes=size+size//2,local_reserve=1)
        with self.assertRaises(offsite.OffsiteError): self.send(limits=limits)
        self.assertEqual([],self.mutations())
        self.assertEqual(before,self.cloud.files)

    def test_unmanaged_bytes_and_unknown_quota_are_not_ignored(self):
        self.cloud.add('old-unmanaged.age', b'x'*5000)
        with self.assertRaises(offsite.OffsiteError): self.send(limits=offsite.Limits(budget_bytes=4000,local_reserve=1))
        self.assertEqual([],self.mutations())
        self.cloud.sizeless = 1
        with self.assertRaises(offsite.OffsiteError): self.send()
        self.assertEqual([],self.mutations())

    def test_rotation_only_removes_recorded_verified_objects_after_new_download_check(self):
        self.cloud.add('tdb-v1-20000101T000000Z-'+'f'*32+'.tar.gz.age', b'foreign matching pattern')
        results = [self.send() for _ in range(4)]
        removed = [args[2].rsplit('/',1)[-1] for stage,args in self.cloud.events if stage=='delete']
        self.assertEqual(1,len(removed))
        self.assertIn(removed[0],{r['archive'] for r in results[:-1]})
        self.assertIn(results[-1]['archive'],self.cloud.files)
        self.assertIn('first-snapshot.tar.gz.age',self.cloud.files)
        self.assertIn('foreign.txt',self.cloud.files)
        self.assertIn('tdb-v1-20000101T000000Z-'+'f'*32+'.tar.gz.age',self.cloud.files)
        self.assertEqual(3,sum(job['state']=='verified' for _,job in self.jobs()))

    def test_errors_at_each_external_stage_cannot_prune_existing_copies(self):
        for stage in ('version','--version','features','about','size','lsjson','encrypt','dry-run','upload','move','check'):
            with self.subTest(stage=stage):
                self.cloud = Cloud()
                self.cloud.add('first-snapshot.tar.gz.age',b'keep')
                self.cloud.fail = stage
                state = self.root/('state-'+stage.replace('-',''))
                with self.assertRaises((offsite.OffsiteError,OSError)):
                    self.send(state=state)
                self.assertEqual(b'keep',self.cloud.files['first-snapshot.tar.gz.age']['data'])
                self.assertFalse(any(s=='delete' for s,_ in self.cloud.events))

    def test_empty_check_report_is_not_success_and_does_not_rotate(self):
        self.cloud.empty_report = True
        with self.assertRaises(offsite.OffsiteError): self.send()
        self.assertEqual('publishing',self.jobs()[0][1]['state'])
        self.assertFalse(any(s=='delete' for s,_ in self.cloud.events))
        self.cloud.empty_report = False
        self.assertTrue(self.send(resume=self.jobs()[0][1]['id'])['cloud_verified'])

    def test_lost_upload_and_move_responses_resume_without_overwrite(self):
        for stage in ('upload','move'):
            with self.subTest(stage=stage):
                self.state = self.root/('state-'+stage)
                self.cloud = Cloud()
                self.cloud.add('first-snapshot.tar.gz.age',b'keep')
                setattr(self.cloud,'after_'+stage,True)
                with self.assertRaises(offsite.OffsiteError): self.send()
                job = self.jobs()[0][1]
                self.assertEqual('uploading' if stage=='upload' else 'publishing',job['state'])
                with self.assertRaises(offsite.OffsiteError): self.send()  # explicit resume required
                result = self.send(resume=job['id'])
                self.assertTrue(result['cloud_verified'])
                self.assertEqual(1,sum(s=='upload' for s,_ in self.cloud.events))
                self.assertEqual(1,sum(s=='move' for s,_ in self.cloud.events))

    def test_actual_download_mismatch_retains_all_existing_cloud_copies(self):
        for _ in range(3): self.send()
        before = dict(self.cloud.files)
        self.cloud.before_check = lambda name: self.cloud.files[name].update(data=b'damaged cloud bytes')
        with self.assertRaises(offsite.OffsiteError): self.send()
        for name,item in before.items(): self.assertEqual(item,self.cloud.files[name])
        self.assertFalse(any(stage=='delete' for stage,_ in self.cloud.events))

    def test_foreign_staging_or_final_name_collision_is_retained(self):
        for where in ('staging','final'):
            with self.subTest(where=where), patch.object(offsite.uuid,'uuid4',return_value=type('ID',(),{'hex':'a'*32})()):
                self.state = self.root/('state-'+where)
                self.cloud = Cloud()
                if where=='staging': name='_tdb-upload-'+'a'*32+'.age'
                else: name='tdb-v1-20261027T000000Z-'+'a'*32+'.tar.gz.age'
                if where=='final':
                    class Date:
                        @staticmethod
                        def now(_):
                            class Stamp:
                                def strftime(self,_): return '20261027T000000Z'
                            return Stamp()
                    date_patch = patch.object(offsite,'datetime',Date)
                else: date_patch = patch.object(offsite,'datetime',offsite.datetime)
                self.cloud.add(name,b'foreign keep')
                with date_patch, self.assertRaises(offsite.OffsiteError): self.send()
                self.assertEqual(b'foreign keep',self.cloud.files[name]['data'])
                self.assertEqual([],self.mutations())

    def test_partial_or_replaced_upload_is_not_adopted_or_deleted(self):
        self.cloud.after_upload = True
        with self.assertRaises(offsite.OffsiteError): self.send()
        job = self.jobs()[0][1]
        temporary = '_tdb-upload-'+job['id']+'.age'
        self.cloud.files[temporary]['data'] = b'partial or foreign content'
        before = dict(self.cloud.files)
        self.cloud.events.clear()
        with self.assertRaises(offsite.OffsiteError): self.send(resume=job['id'])
        self.assertEqual(before,self.cloud.files)
        self.assertEqual([],self.mutations())

    def test_collision_during_publication_cannot_overwrite_or_delete_foreign_final(self):
        self.cloud.before_move = lambda target: self.cloud.add(target,b'foreign concurrent destination')
        with self.assertRaises(offsite.OffsiteError): self.send()
        job = self.jobs()[0][1]
        self.assertEqual(b'foreign concurrent destination',self.cloud.files[job['name']]['data'])
        self.assertIn('_tdb-upload-'+job['id']+'.age',self.cloud.files)
        self.assertFalse(any(stage=='delete' for stage,_ in self.cloud.events))

    def test_live_paths_are_rejected_before_creating_state_or_lock(self):
        state = self.html/'offsite-state'
        with self.assertRaises(offsite.OffsiteError): self.send(state=state)
        self.assertFalse(state.exists())
        self.assertEqual([],self.cloud.events)

    def test_malformed_quota_backend_and_checksum_fail_without_mutation(self):
        original = self.cloud.command
        for command, result in [('about',{}), ('size',{'count':0,'bytes':0}),
                                ('backend',{'Features':None}), ('backend',{'Hashes':['DropboxHash']})]:
            with self.subTest(command=command,result=result):
                def invalid(argv):
                    if argv[1] == command: return json.dumps(result).encode()
                    return original(argv)
                with patch.object(self.cloud,'command',side_effect=invalid):
                    with self.assertRaises(offsite.OffsiteError): self.send()
                self.assertEqual([],self.mutations())

    def test_hardlinked_cipher_and_local_identifier_collision_do_not_overwrite(self):
        result = self.send()
        directory,job = self.jobs()[0]
        path = directory/'payload'/job['name']
        before = path.read_bytes()
        os.link(path,self.root/'another-link.age')
        self.cloud.events.clear()
        with self.assertRaises(offsite.OffsiteError): self.send(resume=result['id'])
        self.assertEqual([],self.mutations())
        identifier = type('Identifier',(),{'hex':result['id']})()
        with patch.object(offsite.uuid,'uuid4',return_value=identifier):
            with self.assertRaises(FileExistsError): self.send()
        self.assertEqual(before,path.read_bytes())
        self.assertEqual([],self.mutations())

    def test_real_child_pipe_streams_archive_and_is_reaped_on_failure(self):
        # A local copying process stands in for age; it has no encryption capability.
        original = subprocess.Popen
        children = []
        for failure in (False,True):
            with self.subTest(failure=failure):
                state = self.root/('process-state-'+str(failure))
                runner = FakeExternal(state,self.limits,self.cloud)
                runner.process = offsite.External.process.__get__(runner)
                def child(argv,**kwargs):
                    self.assertEqual(['age','--encrypt','--recipient',offsite.RECIPIENT],argv)
                    code = ("import sys;sys.exit(2)" if failure else
                            "import shutil,sys;shutil.copyfileobj(sys.stdin.buffer,sys.stdout.buffer,65536)")
                    process = original([sys.executable,'-I','-S','-B','-c',code],**kwargs)
                    children.append(process)
                    return process
                with patch.object(offsite.subprocess,'Popen',side_effect=child):
                    if failure:
                        with self.assertRaises((offsite.OffsiteError,OSError)):
                            offsite.send_backup(self.snapshot,state,external=runner,limits=self.limits)
                    else:
                        result = offsite.send_backup(self.snapshot,state,external=runner,limits=self.limits)
                        with tarfile.open(fileobj=io.BytesIO(self.cloud.files[result['archive']]['data']),mode='r:gz') as archive:
                            self.assertIn('snapshot/database.sqlite3',archive.getnames())
                self.assertIsNotNone(children[-1].poll())

    def test_corrupt_snapshot_local_cipher_and_journal_refuse_remote_mutation(self):
        result = self.send()
        job_dir, job = self.jobs()[0]
        (job_dir/'payload'/job['name']).write_bytes(b'bad ciphertext')
        self.cloud.events.clear()
        with self.assertRaises(offsite.OffsiteError): self.send(resume=result['id'])
        self.assertEqual([],self.mutations())
        job['name'] = 'first-snapshot.tar.gz.age'
        (job_dir/'job.json').write_text(json.dumps(job),encoding='utf-8')
        with self.assertRaises(offsite.OffsiteError): self.send()
        self.assertEqual([],self.mutations())
        (self.snapshot/'COMPLETE').write_bytes(b'corrupted')
        with self.assertRaises(snapshots.BackupError): self.send(state=self.root/'another-state')
        self.assertEqual([],self.mutations())

    def test_replaced_owned_cloud_object_blocks_rotation_instead_of_deleting_it(self):
        results = [self.send() for _ in range(3)]
        name = results[0]['archive']
        self.cloud.add(name,b'replacement belongs to someone else')
        with self.assertRaises(offsite.OffsiteError): self.send()
        self.assertEqual(b'replacement belongs to someone else',self.cloud.files[name]['data'])
        self.assertFalse(any(s=='delete' for s,_ in self.cloud.events))

    def test_delete_failure_keeps_new_verified_copy_and_resume_finishes_rotation(self):
        limits = offsite.Limits(max_archives=1,local_reserve=1)
        old = self.send(limits=limits)
        self.cloud.fail = 'delete'
        with self.assertRaises(offsite.OffsiteError): self.send(limits=limits)
        records = self.jobs()
        new = next(j for _,j in records if j['state']=='verified')
        self.assertIn(new['name'],self.cloud.files)
        self.assertIn(old['archive'],self.cloud.files)
        self.cloud.fail = None
        self.send(resume=new['id'],limits=limits)
        self.assertNotIn(old['archive'],self.cloud.files)
        self.assertIn(new['name'],self.cloud.files)

    def test_lock_and_local_disk_failure_prevent_second_sender(self):
        with offsite._lease(self.state):
            with self.assertRaises(offsite.OffsiteError): self.send()
        self.assertEqual([],self.mutations())
        self.assertTrue(self.send()['cloud_verified'])
        with patch.object(offsite.shutil,'disk_usage',return_value=type('Space',(),{'free':0})()):
            with self.assertRaises(offsite.OffsiteError): self.send()

    def test_state_loss_never_reconstructs_ownership_from_cloud_names(self):
        old = self.send()
        self.send(state=self.root/'fresh-private-state',limits=offsite.Limits(max_archives=1,local_reserve=1))
        self.assertIn(old['archive'],self.cloud.files)
        self.assertIn('first-snapshot.tar.gz.age',self.cloud.files)
        self.assertFalse(any(s=='delete' for s,_ in self.cloud.events))

    def test_snapshot_changes_while_archiving_cannot_upload(self):
        original = offsite._pack
        def pack(source,*args):
            path = next(p for p in (source/'html/0').iterdir() if p.is_file())
            path.write_bytes(b'changed source')
            return original(source,*args)
        with patch.object(offsite,'_pack',side_effect=pack):
            with self.assertRaises((offsite.OffsiteError,snapshots.BackupError)):
                self.send()
        self.assertEqual([],self.mutations())

    def test_symlink_cipher_or_state_is_rejected_without_remote_mutation(self):
        result = self.send()
        job_dir,job = self.jobs()[0]
        path = job_dir/'payload'/job['name']
        saved = self.root/'cipher.age'
        path.rename(saved)
        try: path.symlink_to(saved)
        except OSError: self.skipTest('Creating symlinks requires OS privileges')
        self.cloud.events.clear()
        with self.assertRaises((offsite.OffsiteError,snapshots.BackupError)):
            self.send(resume=result['id'])
        self.assertEqual([],self.mutations())


class ExternalProcessTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='tdb-offsite-',dir=Path(__file__).parent)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).absolute()
        self.runner = offsite.External(self.root,offsite.Limits(local_reserve=1,timeout=3))

    def test_process_timeout_and_bounded_output_are_redacted_and_reaped(self):
        for code in ("import time;time.sleep(20)", "import sys;sys.stdout.write('PRIVATE_TEST_SENTINEL'*1000000)",
                     "import sys;print('PRIVATE_TEST_SENTINEL');sys.exit(2)"):
            with self.subTest(code=code[:20]):
                self.runner.deadline = offsite.time.monotonic()+0.4
                with self.assertRaises(offsite.OffsiteError) as error:
                    self.runner.run([sys.executable,'-I','-S','-B','-c',code])
                self.assertNotIn('PRIVATE_TEST_SENTINEL',str(error.exception))
        self.runner.deadline = offsite.time.monotonic()+3
        self.assertEqual(b'ok',self.runner.run([sys.executable,'-I','-S','-B','-c',"print('ok')"]).strip())

    def test_rclone_flags_limit_parallelism_and_do_not_use_shell_or_destructive_sync(self):
        with patch.object(self.runner,'run',return_value=b'') as run:
            self.runner.rc('copyto','local.age',offsite.REMOTE+'/staging.age','--ignore-existing','--immutable')
        args = run.call_args.args[0]
        for flag,value in (('--transfers','1'),('--checkers','1'),('--dropbox-chunk-size','4Mi'),
                           ('--multi-thread-streams','0'),('--max-delete','0')):
            self.assertEqual(value,args[args.index(flag)+1])
        self.assertNotIn('sync',args)
        self.assertEqual('1',self.runner.env['GOMAXPROCS'])

    def test_private_key_invalid_remote_and_cancel_are_safe_refusals(self):
        with self.assertRaises(offsite.OffsiteError): offsite._recipient('AGE-SECRET-KEY-1-DO-NOT-PRINT')
        for remote in ('tdb-dropbox:', 'tdb-dropbox:../bad', ':dropbox,token=SECRET:', 'tdb-dropbox:TCD-Backups/../elsewhere'):
            with self.assertRaises(offsite.OffsiteError): offsite._remote(remote)
        self.runner.cancel.set()
        with self.assertRaises(offsite.OffsiteError): self.runner.run([sys.executable,'-c',"print('should not run')"])


if __name__ == '__main__':
    unittest.main()
