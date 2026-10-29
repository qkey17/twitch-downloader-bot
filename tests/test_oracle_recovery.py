"""Offline recovery tests: real disposable Git history; no network/system services."""
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile
import unittest

from tests.test_oracle_deployment import BASH, ROOT, SCRIPT


@unittest.skipUnless(os.name == 'posix' and BASH and shutil.which('git'), 'POSIX Git/Bash required')
class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='tdb-recovery-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.project = self.root/'project'
        self.env = os.environ | {'GIT_CONFIG_NOSYSTEM':'1', 'GIT_TERMINAL_PROMPT':'0',
                                 'GIT_CONFIG_GLOBAL':'/dev/null', 'GIT_AUTHOR_NAME':'Lab',
                                 'GIT_AUTHOR_EMAIL':'lab@example.invalid', 'GIT_COMMITTER_NAME':'Lab',
                                 'GIT_COMMITTER_EMAIL':'lab@example.invalid'}
        self.git('init', '-b', 'main', str(self.project), cwd=self.root)
        (self.project/'src').mkdir()
        self.versions=[]
        for text in ('working', 'broken', 'fixed'):
            (self.project/'src/main.py').write_text('VERSION='+repr(text)+'\n')
            self.git('add','src/main.py'); self.git('commit','-qm',text)
            self.versions.append(self.git('rev-parse','HEAD').strip())
        self.old, self.broken, self.fixed = self.versions
        self.git('init','--bare',str(self.root/'remote.git'),cwd=self.root)
        self.git('remote','add','origin',str(self.root/'remote.git'))
        self.git('push','-q','origin','main')
        # New test checkout at broken version, without resetting any checkout.
        source=self.project
        self.project=self.root/'deployed'
        self.git('clone','-q',str(source),str(self.project),cwd=self.root)
        self.git('switch','-c','deployed',self.broken)
        # Branch name is a virtualized server identity, not an ancestry mock.
        (self.project/'.venv/bin').mkdir(parents=True)
        python=self.project/'.venv/bin/python'
        python.write_text('#!/bin/sh\nexit 0\n'); python.chmod(0o700)
        self.private=[]
        for name in ('.env','data.sqlite3','finished.html'):
            p=self.project/name;p.write_bytes(b'preserve-synthetic-data');self.private.append(p)
        self.locks=self.root/'locks';self.locks.mkdir(mode=0o700)
        (self.locks/'deploy.lock').touch(mode=0o600)
        self.state=self.root/'daily';self.config=self.root/'admin/config.json'
        self.script=self.root/'helper.sh'
        code=SCRIPT.read_text()
        replacements={
            'readonly PROJECT=/home/ubuntu/twitch-downloader-bot':'readonly PROJECT='+shlex.quote(str(self.project)),
            'readonly STATE_DIR=/home/ubuntu/.local/state/tdb-deploy':'readonly STATE_DIR='+shlex.quote(str(self.locks)),
            'readonly RECOVERY_STATE=/home/ubuntu/tdb-daily-state':'readonly RECOVERY_STATE='+shlex.quote(str(self.state)),
            'readonly RECOVERY_CONFIG=/home/ubuntu/tdb-admin/daily-backup.json':'readonly RECOVERY_CONFIG='+shlex.quote(str(self.config)),
            'readonly RECOVERY_SOCKET=/run/tdb-maintenance/control.sock':'readonly RECOVERY_SOCKET='+shlex.quote(str(self.root/'control.sock')),
            'readonly STOP_TIMEOUT=600 START_TIMEOUT=180 STABILITY_SECONDS=10':'readonly STOP_TIMEOUT=0 START_TIMEOUT=0 STABILITY_SECONDS=0',
            'if (( EUID == 0 )); then':'if false; then',
            '/usr/bin/systemctl':'mock_systemctl',
        }
        for before,after in replacements.items():
            self.assertIn(before,code);code=code.replace(before,after)
        code=code.removesuffix('main "$@"\n')
        self.script.write_text(code+'\n'+MOCKS+'\nmain\n')

    def git(self,*args,cwd=None):
        return subprocess.run(['git',*args],cwd=cwd or self.project,env=self.env,
                              check=True,capture_output=True,text=True,timeout=15).stdout

    def state_files(self, owner=None, hold=False):
        self.state.mkdir(mode=0o700);self.config.parent.mkdir(mode=0o700)
        self.config.write_text(json.dumps({'state_dir':str(self.state), 'deploy_lock':str(self.locks/'deploy.lock')}));self.config.chmod(0o600)
        (self.state/'offsite.lock').touch(mode=0o600)
        for name,value in (('owner',owner),('hold',hold)):
            p=self.state/(name+'.json');p.write_text(json.dumps({'format':'tdb-daily-state-v1','value':value}));p.chmod(0o600)

    def call(self, scenario='inactive', expected=None, target=None, mode='recover'):
        events=self.root/'events';events.write_text('')
        request=f'{mode} {expected or self.broken} {target or self.fixed}' if mode not in ('check','deploy') else mode+((' '+(target or self.fixed)) if mode=='deploy' else '')
        p=subprocess.run([BASH,'--noprofile','--norc',str(self.script)],
            env=self.env|{'SSH_ORIGINAL_COMMAND':request,'SCENARIO':scenario,'EVENTS':str(events)},
            text=True,capture_output=True,timeout=15)
        for f in self.private:self.assertEqual(f.read_bytes(),b'preserve-synthetic-data')
        self.assertNotIn('PRIVATE_SENTINEL',p.stdout+p.stderr)
        return p, events.read_text().splitlines()

    def test_recover_failed_and_inactive(self):
        for scenario in ('failed','inactive'):
            # Each successful call needs a distinct disposable history/checkout.
            with self.subTest(scenario=scenario):
                p,events=self.call(scenario,mode='check-recovery')
                self.assertEqual(p.returncode,0,p.stderr)
                self.assertEqual(self.git('rev-parse','HEAD').strip(),self.broken)
                self.assertFalse(events)
        p,events=self.call('failed')
        self.assertEqual(p.returncode,0,p.stderr)
        self.assertEqual(p.stdout.strip(),'DEPLOYED '+self.fixed)
        self.assertEqual(events,['start'])
        self.assertEqual(self.git('rev-parse','HEAD').strip(),self.fixed)

    def test_inactive_can_be_recovered(self):
        p,events=self.call();self.assertEqual(p.returncode,0,p.stderr);self.assertEqual(events,['start'])

    def test_refuses_active_transient_unknown_job_and_residual_processes(self):
        for scenario in ('active','activating','deactivating','unknown','state_error','job','control','tasks','unknown_tasks','timer','recovery_unit','daily_unit'):
            with self.subTest(scenario=scenario):
                p,events=self.call(scenario);self.assertNotEqual(p.returncode,0);self.assertFalse(events)
                self.assertEqual(self.git('rev-parse','HEAD').strip(),self.broken)

    def test_owner_and_hold_are_never_removed(self):
        self.state_files(owner={'attempts':3})
        before=(self.state/'owner.json').read_bytes()
        p,events=self.call();self.assertNotEqual(p.returncode,0);self.assertFalse(events)
        self.assertEqual((self.state/'owner.json').read_bytes(),before)
        (self.state/'owner.json').write_text(json.dumps({'format':'tdb-daily-state-v1','value':None}))
        (self.state/'hold.json').write_text(json.dumps({'format':'tdb-daily-state-v1','value':True}))
        p,events=self.call();self.assertNotEqual(p.returncode,0);self.assertFalse(events)

    def test_resolved_private_state_is_accepted(self):
        self.state_files();p,_=self.call();self.assertEqual(p.returncode,0,p.stderr)

    def test_busy_deployment_and_daily_locks(self):
        import fcntl
        for path in (self.locks/'deploy.lock',):
            with path.open('rb') as fd:
                fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
                p,events=self.call();self.assertNotEqual(p.returncode,0);self.assertFalse(events)
        self.state_files()
        with (self.state/'offsite.lock').open('rb') as fd:
            fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
            p,events=self.call();self.assertNotEqual(p.returncode,0);self.assertFalse(events)

    def test_wrong_expected_and_no_forward_or_main_ancestry(self):
        for expected,target in ((self.old,self.fixed),(self.broken,self.old),(self.broken,self.broken),
                                (self.broken,'f'*40)):
            with self.subTest(expected=expected,target=target):
                p,events=self.call(expected=expected,target=target);self.assertNotEqual(p.returncode,0);self.assertFalse(events)
        self.git('switch','-c','outside');(self.project/'unmerged.txt').touch()
        self.git('add','unmerged.txt');self.git('commit','-qm','not on remote main')
        outside=self.git('rev-parse','HEAD').strip();self.git('switch','deployed')
        p,events=self.call(target=outside);self.assertNotEqual(p.returncode,0);self.assertFalse(events)

    def test_dirty_checkout_and_missing_lock(self):
        p=self.project/'src/main.py';p.write_text('dirty')
        result,events=self.call();self.assertNotEqual(result.returncode,0);self.assertFalse(events)
        p.write_text('VERSION='+repr('broken')+'\n');(self.locks/'deploy.lock').unlink()
        result,events=self.call();self.assertNotEqual(result.returncode,0);self.assertFalse(events)
        self.assertFalse((self.locks/'deploy.lock').exists())

    def test_failed_start_and_readiness_have_no_trap_retry(self):
        p,events=self.call('start_error');self.assertNotEqual(p.returncode,0)
        self.assertEqual(events,['start']);self.assertNotIn('DEPLOYED',p.stdout)
        self.assertEqual(self.git('rev-parse','HEAD').strip(),self.fixed)

    def test_readiness_failure_retains_fixed_sources(self):
        p,events=self.call('not_ready');self.assertNotEqual(p.returncode,0);self.assertEqual(events,['start'])
        self.assertEqual(self.git('rev-parse','HEAD').strip(),self.fixed)

    def test_original_check_deploy_still_require_running_bot(self):
        for mode in ('check','deploy'):
            p,events=self.call(mode=mode);self.assertNotEqual(p.returncode,0);self.assertFalse(events)

    def test_private_state_fail_closed(self):
        self.state_files()
        for content in ('{', '{"format":"tdb-daily-state-v1","value":null,"value":null}'):
            (self.state/'owner.json').write_text(content)
            p,events=self.call();self.assertNotEqual(p.returncode,0);self.assertFalse(events)
        (self.state/'owner.json').unlink();(self.state/'owner.json').symlink_to(self.private[0])
        p,events=self.call();self.assertNotEqual(p.returncode,0);self.assertFalse(events)

    def test_committed_runtime_marker_is_not_discarded(self):
        marker=self.root/'control.sock.committed';marker.write_text('preserve')
        p,events=self.call();self.assertNotEqual(p.returncode,0);self.assertFalse(events)
        self.assertEqual(marker.read_text(),'preserve')


MOCKS = r'''
id() { echo ubuntu; }
git() {
    if [[ "$*" == *'remote get-url origin' ]]; then echo https://github.com/qkey17/twitch-downloader-bot.git;
    elif [[ "$*" == *'branch --show-current' ]]; then echo main;
    else command git "$@"; fi
}
mock_systemctl() {
    local unit=$2 prop=${3#--property=} state=inactive sub=dead
    [[ -f "$EVENTS.started" ]] && state=active && sub=running
    if [[ "$3" == --property=LoadState ]]; then
        if [[ "$unit" == tdb-daily-backup.timer ]]; then
            printf 'LoadState=not-found\nActiveState=inactive\nSubState=dead\nUnitFileState=\nJob=\n'
            [[ "$SCENARIO" != timer ]] || echo Job=42
            return
        fi
        if [[ "$unit" != tdb.service ]]; then
            if [[ ( "$SCENARIO" == recovery_unit && "$unit" == tdb-backup-recover.service ) || ( "$SCENARIO" == daily_unit && "$unit" == tdb-daily-backup.service ) ]]; then state=active;sub=running;
            else printf 'LoadState=not-found\nActiveState=inactive\nSubState=dead\nMainPID=0\nControlPID=0\nTasksCurrent=[not set]\nControlGroup=\nJob=\n';return;fi
        elif [[ ! -f "$EVENTS.started" ]]; then
            case "$SCENARIO" in
                failed) state=failed;sub=failed;; active) state=active;sub=running;;
                activating|deactivating|unknown) state=$SCENARIO;sub=$SCENARIO;;
                state_error) echo PRIVATE_SENTINEL >&2;return 1;;
            esac
        fi
        printf 'LoadState=loaded\nActiveState=%s\nSubState=%s\nMainPID=0\nControlPID=%s\nTasksCurrent=%s\nControlGroup=%s\nJob=%s\n' "$state" "$sub" "$([[ "$SCENARIO" == control ]] && echo 22 || echo 0)" "$([[ "$SCENARIO" == tasks ]] && echo 1 || echo '[not set]')" "$([[ "$SCENARIO" == unknown_tasks ]] && echo /system.slice/unknown.service)" "$([[ "$SCENARIO" == job ]] && echo 42)"
        return
    fi
    case "$prop" in
        WorkingDirectory) echo "$PROJECT";; ExecStart) echo "$PROJECT/.venv/bin/python -m src.main";;
        Type) echo simple;; TimeoutStopUSec) echo infinity;; KillSignal) echo 15;;
        KillMode) echo control-group;; SendSIGKILL) echo no;; StandardOutput|StandardError) echo journal;;
        ActiveState) echo "$state";; SubState) echo "$sub";;
        InvocationID) [[ -f "$EVENTS.started" ]] && printf '%032d\n' 2 || printf '%032d\n' 1;;
        MainPID) echo 1234;; NRestarts) echo 0;; *) echo '';;
    esac
}
sudo() {
    shift
    [[ "$1" != -l ]] || return 0
    if [[ "$1" == "$HELPER" ]]; then [[ "$SCENARIO" != not_ready ]]; return; fi
    [[ "$1 $2 $3 $4" == 'mock_systemctl --no-block start tdb.service' ]] || return 95
    echo start >> "$EVENTS"
    [[ "$SCENARIO" != start_error ]] || return 1
    touch "$EVENTS.started"
}
'''


if __name__ == '__main__':
    unittest.main()
