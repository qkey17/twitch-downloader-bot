"""Offline Bash tests: fake Git/systemd/SSH protocol; never contact a VM."""
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile
import textwrap
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'deploy/oracle_deploy.sh'
WORKFLOW = ROOT / '.github/workflows/deploy-oracle.yml'
BASH = (str(Path(os.environ.get('ProgramFiles', 'C:/Program Files')) / 'Git/bin/bash.exe')
        if os.name == 'nt' else shutil.which('bash'))
if BASH and not Path(BASH).is_file():
    BASH = None
OLD = 'a' * 40
TARGET = 'b' * 40


def shell_path(path):
    value = Path(path).resolve().as_posix()
    if os.name == 'nt':
        return '/' + value[0].lower() + value[2:]
    return value


MOCKS = r'''
event() { printf '%s\n' "$*" >> "$FAKE_ROOT/events"; }
id() { printf 'ubuntu\n'; }
flock() { event lock; [[ "$SCENARIO" != locked ]]; }
sleep() {
    if [[ "$stage" == stability && "$SCENARIO" == unstable ]]; then
        printf 'inactive' > "$FAKE_ROOT/active"
    fi
}
timeout() {
    shift
    if [[ "$1" == "$PROJECT/.venv/bin/python" ]]; then
        event "python:$*"
        [[ "$SCENARIO" != imports_fail || "$*" != *'import src.main'* ]]
    else
        "$@"
    fi
}
git() {
    [[ "$1" == -c ]] && shift 2
    [[ "$1" == -C ]] && shift 2
    event "git:$*"
    case "$1" in
        rev-parse)
            if [[ "$2" == --show-toplevel ]]; then printf '%s\n' "$PROJECT";
            else cat "$FAKE_ROOT/head"; fi ;;
        branch) if [[ "$SCENARIO" == branch ]]; then echo topic; else echo main; fi ;;
        status)
            if [[ "$SCENARIO" == status_error || ( "$SCENARIO" == status_after_update_error && $(cat "$FAKE_ROOT/head") == "$FAKE_TARGET" ) ]]; then return 1; fi
            if [[ "$SCENARIO" == dirty || -f "$FAKE_ROOT/partial-merge" ]]; then echo ' M src/main.py'; fi ;;
        remote) echo https://github.com/qkey17/twitch-downloader-bot.git ;;
        fetch) if [[ "$SCENARIO" == fetch_error ]]; then echo PRIVATE_TEST_SENTINEL >&2; return 1; fi ;;
        cat-file) return 0 ;;
        merge-base)
            [[ "$SCENARIO" != outside_main || "$4" != refs/remotes/origin/main ]] || return 1
            [[ "$SCENARIO" != downgrade || "$4" != "$FAKE_TARGET" ]] || return 1 ;;
        diff)
            if [[ "$2" == --quiet ]]; then [[ "$SCENARIO" != dependencies ]] || return 1;
            elif [[ "$*" == *--diff-filter=A* ]]; then
                if [[ "$SCENARIO" == new_paths_error ]]; then echo PRIVATE_TEST_SENTINEL >&2; return 1; fi
                if [[ "$SCENARIO" == collision ]]; then printf 'local.txt\0'; fi
                if [[ "$SCENARIO" == parent_collision ]]; then printf 'local.txt/nested.py\0'; fi
                if [[ "$SCENARIO" == parent_symlink ]]; then printf 'linked/nested.py\0'; fi
            else
                if [[ "$SCENARIO" == changed_paths_error ]]; then echo PRIVATE_TEST_SENTINEL >&2; return 1; fi
                if [[ "$SCENARIO" == protected_deleted ]]; then printf '.env\0'; fi
            fi ;;
        ls-tree)
            if [[ "$SCENARIO" == ls_tree_error ]]; then echo PRIVATE_TEST_SENTINEL >&2; return 1; fi
            if [[ "$SCENARIO" == protected ]]; then printf '.env\0'; else printf 'src/main.py\0'; fi ;;
        merge)
            [[ "$2" == --ff-only && "$3" == --no-edit && "$4" == --no-overwrite-ignore && "$5" == "$FAKE_TARGET" ]] || return 95
            if [[ "$SCENARIO" == merge_dirty ]]; then touch "$FAKE_ROOT/partial-merge"; return 1; fi
            if [[ "$SCENARIO" == merge_error ]]; then echo PRIVATE_TEST_SENTINEL >&2; return 1; fi
            printf '%s\n' "$5" > "$FAKE_ROOT/head" ;;
        *) return 96 ;;
    esac
}
mock_systemctl() {
    local value='' option
    for option in "$@"; do
        if [[ "$option" == --property=* ]]; then value=${option#--property=}; fi
    done
    case "$value" in
        WorkingDirectory) printf '%s\n' "$PROJECT" ;;
        ExecStart) printf '%s/.venv/bin/python -m src.main\n' "$PROJECT" ;;
        Type) echo simple ;;
        TimeoutStopUSec) if [[ "$SCENARIO" == unsafe_stop ]]; then echo 90s; else echo infinity; fi ;;
        KillSignal) echo 15 ;;
        KillMode) echo control-group ;;
        SendSIGKILL) echo no ;;
        StandardOutput|StandardError) echo journal ;;
        ActiveState) cat "$FAKE_ROOT/active" ;;
        SubState)
            if [[ $(cat "$FAKE_ROOT/active") == active ]]; then echo running; else echo dead; fi ;;
        InvocationID) cat "$FAKE_ROOT/invocation" ;;
        MainPID)
            if [[ $(cat "$FAKE_ROOT/active") == active ]]; then echo 1234; else echo 0; fi ;;
        NRestarts) echo 0 ;;
        *) printf 'ActiveState=%s\n' "$(cat "$FAKE_ROOT/active")" ;;
    esac
}
mock_journalctl() {
    event "journal:$*"
    if [[ "$SCENARIO" == no_marker ]]; then echo PRIVATE_TEST_SENTINEL;
    elif [[ "$SCENARIO" == wrong_logger ]]; then echo '2026 | INFO | another.logger | Application started';
    else echo '2026 | INFO | telegram.ext.Application | Application started'; fi
}
sudo() {
    shift  # -n
    if [[ "$1" == -l ]]; then return 0; fi
    if [[ "$1" == "$HELPER" ]]; then [[ "$SCENARIO" != no_marker ]]; return; fi
    [[ "$1" == mock_systemctl && "$2" == --no-block && "$4" == tdb.service ]] || return 94
    event "$3"
    if [[ "$3" == stop ]]; then
        if [[ "$SCENARIO" == stop_timeout ]]; then echo deactivating > "$FAKE_ROOT/active";
        else echo inactive > "$FAKE_ROOT/active"; fi
        if [[ "$SCENARIO" == signal_after_stop ]]; then kill -HUP "$$"; fi
    else
        [[ "$SCENARIO" != start_error ]] || return 1
        echo active > "$FAKE_ROOT/active"
        if [[ "$SCENARIO" != stale_invocation ]]; then printf '%032d\n' 2 > "$FAKE_ROOT/invocation"; fi
    fi
}
'''


@unittest.skipUnless(BASH, 'Bash is required for offline deployment tests')
class OracleDeploymentTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='tdb-deploy-', dir=ROOT/'tests')
        self.root = Path(temp.name).resolve()
        self.assertEqual((ROOT/'tests').resolve(), self.root.parent)
        self.addCleanup(temp.cleanup)
        project = self.root/'project'
        (project/'.git').mkdir(parents=True)
        (project/'.venv/bin').mkdir(parents=True)
        python = project/'.venv/bin/python'
        python.write_text('#!/bin/sh\nexit 0\n', encoding='utf-8')
        python.chmod(0o755)
        self.private_files = [project/'.env', project/'data.sqlite3', project/'finished.html', project/'local.txt']
        for path in self.private_files:
            path.write_bytes(b'keep-private-fixture')
        source = SCRIPT.read_text(encoding='utf-8')
        self.assertTrue(source.endswith('main "$@"\n'))
        source = source.removesuffix('main "$@"\n')
        source = source.replace('readonly PROJECT=/home/ubuntu/twitch-downloader-bot',
                                'readonly PROJECT='+shlex.quote(shell_path(project)))
        source = source.replace('readonly STATE_DIR=/home/ubuntu/.local/state/tdb-deploy',
                                'readonly STATE_DIR='+shlex.quote(shell_path(self.root/'locks')))
        source = source.replace('readonly STOP_TIMEOUT=600 START_TIMEOUT=180 STABILITY_SECONDS=10',
                                'readonly STOP_TIMEOUT=0 START_TIMEOUT=0 STABILITY_SECONDS=0')
        source = source.replace('/usr/bin/systemctl', 'mock_systemctl').replace('/usr/bin/journalctl', 'mock_journalctl')
        source = source.replace('if (( EUID == 0 )); then', 'if false; then')
        self.harness = self.root/'harness.sh'
        self.harness.write_text(source+'\n'+MOCKS+'\n${FAKE_ENTRY:-main}\n', encoding='utf-8', newline='\n')

    def run_case(self, scenario='success', request=None, entry='main'):
        (self.root/'partial-merge').unlink(missing_ok=True)
        (self.root/'head').write_text(OLD+'\n', encoding='utf-8')
        (self.root/'active').write_text('active\n', encoding='utf-8')
        (self.root/'invocation').write_text('0'*31+'1\n', encoding='utf-8')
        (self.root/'events').write_text('', encoding='utf-8')
        env = dict(os.environ, FAKE_ROOT=shell_path(self.root), SCENARIO=scenario,
                   SSH_ORIGINAL_COMMAND='deploy '+TARGET if request is None else request,
                   FAKE_TARGET=TARGET, FAKE_ENTRY=entry)
        result = subprocess.run([BASH, '--noprofile', '--norc', shell_path(self.harness)],
                                env=env, cwd=ROOT, capture_output=True, text=True, encoding='utf-8', timeout=15)
        events = (self.root/'events').read_text(encoding='utf-8').splitlines()
        for path in self.private_files:
            self.assertEqual(b'keep-private-fixture', path.read_bytes())
        self.assertNotIn('PRIVATE_TEST_SENTINEL', result.stdout+result.stderr)
        return result, events

    def test_exact_fast_forward_and_success_only_after_startup_checks(self):
        result, events = self.run_case()
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual('DEPLOYED '+TARGET, result.stdout.strip())
        merge = 'git:merge --ff-only --no-edit --no-overwrite-ignore '+TARGET
        self.assertLess(events.index('stop'), events.index(merge))
        self.assertLess(events.index(merge), events.index('start'))
        self.assertEqual(1, events.count('start'))
        self.assertEqual(TARGET, (self.root/'head').read_text().strip())

    def test_check_does_not_fetch_update_or_restart(self):
        result, events = self.run_case(request='check')
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual('CHECK_OK protocol=1', result.stdout.strip())
        self.assertFalse(any(e in ('start', 'stop') or e.startswith(('git:fetch', 'git:merge ')) for e in events))

    def test_preflight_failures_and_lock_never_stop_the_service(self):
        for scenario in ('locked', 'dirty', 'branch', 'unsafe_stop', 'fetch_error',
                         'outside_main', 'downgrade', 'dependencies', 'protected', 'collision',
                         'status_error', 'ls_tree_error', 'changed_paths_error', 'new_paths_error',
                         'protected_deleted', 'parent_collision'):
            with self.subTest(scenario=scenario):
                result, events = self.run_case(scenario)
                self.assertNotEqual(0, result.returncode)
                self.assertNotIn('stop', events)
                self.assertNotIn('start', events)
                self.assertNotIn('DEPLOYED', result.stdout)
                self.assertEqual(OLD, (self.root/'head').read_text().strip())
                self.assertFalse(list((self.root/'locks').glob('paths.*')))

    def test_unknown_checkout_after_update_withholds_recovery_start(self):
        result, events = self.run_case('status_after_update_error')
        self.assertNotEqual(0, result.returncode)
        self.assertIn('stop', events)
        self.assertNotIn('start', events)
        self.assertNotIn('DEPLOYED', result.stdout)
        self.assertIn('Manual repair is required', result.stderr)

    def test_symlink_lock_cannot_truncate_local_data(self):
        locks = self.root/'locks'
        locks.mkdir()
        lock = locks/'deploy.lock'
        try:
            lock.symlink_to(self.private_files[0])
        except OSError:
            self.skipTest('Creating symlinks requires Windows privileges')
        result, events = self.run_case()
        self.assertNotEqual(0, result.returncode)
        self.assertFalse(events)
        self.assertTrue(lock.is_symlink())

    def test_new_path_cannot_traverse_a_local_symlink(self):
        link = self.root/'project/linked'
        try:
            link.symlink_to(self.root, target_is_directory=True)
        except OSError:
            self.skipTest('Creating symlinks requires Windows privileges')
        result, events = self.run_case('parent_symlink')
        self.assertNotEqual(0, result.returncode)
        self.assertNotIn('stop', events)

    def test_failed_update_queues_old_service_start_but_remains_failed(self):
        result, events = self.run_case('merge_error')
        self.assertNotEqual(0, result.returncode)
        self.assertEqual(1, events.count('stop'))
        self.assertEqual(1, events.count('start'))
        self.assertEqual(OLD, (self.root/'head').read_text().strip())
        self.assertNotIn('DEPLOYED', result.stdout)

    def test_stop_timeout_never_updates_checkout(self):
        result, events = self.run_case('stop_timeout')
        self.assertNotEqual(0, result.returncode)
        self.assertFalse(any(e.startswith('git:merge ') for e in events))
        self.assertIn('start', events)

    def test_partial_failed_merge_is_not_started_automatically(self):
        result, events = self.run_case('merge_dirty')
        self.assertNotEqual(0, result.returncode)
        self.assertIn('stop', events)
        self.assertNotIn('start', events)
        self.assertIn('Manual repair is required', result.stderr)

    def test_ssh_hangup_after_stop_requests_recovery_without_success(self):
        result, events = self.run_case('signal_after_stop')
        self.assertEqual(129, result.returncode)
        self.assertIn('stop', events)
        self.assertIn('start', events)
        self.assertFalse(any(e.startswith('git:merge ') for e in events))
        self.assertNotIn('DEPLOYED', result.stdout)

    def test_failed_import_start_readiness_or_stability_never_report_success(self):
        for scenario in ('imports_fail', 'start_error', 'no_marker', 'stale_invocation', 'unstable'):
            with self.subTest(scenario=scenario):
                result, events = self.run_case(scenario)
                self.assertNotEqual(0, result.returncode)
                self.assertIn('start', events)
                self.assertNotIn('DEPLOYED', result.stdout)
                self.assertEqual(TARGET, (self.root/'head').read_text().strip())

    def test_protocol_rejects_injection_and_abbreviated_sha(self):
        for request in ('', 'deploy main', 'deploy '+TARGET+'; echo PRIVATE_TEST_SENTINEL', 'ready', 'deploy '+TARGET.upper()):
            with self.subTest(request_length=len(request)):
                result, events = self.run_case(request=request)
                self.assertNotEqual(0, result.returncode)
                self.assertFalse(events)

    def test_readiness_uses_current_invocation_without_returning_journal(self):
        result, events = self.run_case(entry='ready')
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual('', result.stdout)
        self.assertTrue(any('_SYSTEMD_INVOCATION_ID='+'0'*31+'1' in e for e in events))
        self.assertTrue(any('_PID=1234' in e for e in events))
        for scenario in ('no_marker', 'wrong_logger'):
            with self.subTest(scenario=scenario):
                result, _ = self.run_case(scenario, entry='ready')
                self.assertNotEqual(0, result.returncode)
                self.assertEqual('', result.stdout)

    def test_workflow_manual_gate_protocol_and_secret_handling(self):
        workflow = WORKFLOW.read_text(encoding='utf-8')
        run = textwrap.dedent(workflow.split('        run: |\n', 1)[1])
        mocks = r'''
ssh-keygen() { return 0; }
ssh() {
    printf '%s\n' "$*" >> "$RUNNER_TEMP/ssh-events"
    [[ "$FAKE_SCENARIO" != ssh_error ]] || return 1
    case "${!#}" in
        check)
            if [[ "$FAKE_SCENARIO" != bad_check ]]; then echo 'CHECK_OK protocol=1'; fi ;;
        "deploy $GITHUB_SHA")
            if [[ "$FAKE_SCENARIO" != bad_deploy ]]; then echo "DEPLOYED $GITHUB_SHA"; fi ;;
        *) return 90 ;;
    esac
}
'''
        script = self.root/'workflow.sh'
        script.write_text(mocks+'\n'+run, encoding='utf-8', newline='\n')
        for scenario, check_only, ref, target, expected_calls, success in (
            ('success', 'true', 'refs/heads/main', '', 1, True),
            ('success', 'false', 'refs/heads/main', '', 2, True),
            ('success', 'true', 'refs/heads/topic', '', 0, False),
            ('success', 'false', 'refs/heads/main', TARGET+'; id', 0, False),
            ('success', 'invalid', 'refs/heads/main', '', 0, False),
            ('ssh_error', 'false', 'refs/heads/main', '', 1, False),
            ('bad_check', 'false', 'refs/heads/main', '', 1, False),
            ('bad_deploy', 'false', 'refs/heads/main', '', 2, False),
        ):
            with self.subTest(scenario=scenario, check_only=check_only, ref=ref):
                events = self.root/'ssh-events'
                summary = self.root/'summary'
                events.unlink(missing_ok=True)
                summary.unlink(missing_ok=True)
                env = dict(os.environ, FAKE_SCENARIO=scenario, CHECK_ONLY=check_only,
                           GITHUB_REF=ref, GITHUB_SHA=TARGET, TARGET_INPUT=target,
                           ORACLE_SSH_PRIVATE_KEY='PRIVATE_TEST_SENTINEL',
                           ORACLE_SSH_KNOWN_HOSTS='PRIVATE_TEST_SENTINEL',
                           RUNNER_TEMP=shell_path(self.root), GITHUB_STEP_SUMMARY=shell_path(summary))
                result = subprocess.run([BASH, '--noprofile', '--norc', shell_path(script)],
                                        env=env, capture_output=True, text=True, timeout=10)
                self.assertEqual(success, result.returncode == 0, result.stderr)
                calls = events.read_text().splitlines() if events.exists() else []
                self.assertEqual(expected_calls, len(calls))
                output = result.stdout+result.stderr+(summary.read_text() if summary.exists() else '')
                self.assertNotIn('PRIVATE_TEST_SENTINEL', output)
                self.assertFalse(list(self.root.glob('tdb-ssh.*')))
                if not success or check_only == 'true':
                    self.assertNotIn('Deployment verified', output)
                for call in calls:
                    self.assertIn('StrictHostKeyChecking=yes', call)
                    self.assertIn('GlobalKnownHostsFile=/dev/null', call)
                    self.assertIn('HostKeyAlgorithms=ssh-ed25519', call)

    def test_bash_syntax_and_workflow_security_contract(self):
        result = subprocess.run([BASH, '-n', shell_path(SCRIPT)], capture_output=True, text=True, timeout=10)
        self.assertEqual(0, result.returncode, result.stderr)
        workflow = WORKFLOW.read_text(encoding='utf-8')
        self.assertIn('workflow_dispatch:', workflow)
        self.assertNotIn('\n  push:', workflow)
        self.assertIn('permissions: {}', workflow)
        self.assertIn('cancel-in-progress: false', workflow)
        self.assertIn('deployment: ${{ !inputs.check_only }}', workflow)
        self.assertIn('StrictHostKeyChecking=yes', workflow)
        self.assertNotIn('ssh-keyscan', workflow)
        self.assertNotIn('uses:', workflow)
        self.assertIn("default: true", workflow)
        run = textwrap.dedent(workflow.split('        run: |\n', 1)[1])
        result = subprocess.run([BASH, '-n'], input=run, capture_output=True, text=True, timeout=10)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertNotIn('timeout 120 git_cmd', SCRIPT.read_text())

    def test_emergency_workflow_exact_sha_protocol(self):
        run = textwrap.dedent(WORKFLOW.read_text().split('        run: |\n', 1)[1])
        script = self.root/'workflow-recovery.sh'
        mocks = r'''
ssh-keygen() { return 0; }
ssh() {
    echo "${!#}" >> "$RUNNER_TEMP/calls"
    case "${!#}" in
        "check-recovery $EXPECTED_CURRENT $TARGET_INPUT")
            [[ "$FAKE_CASE" != bad_check ]] || return 1
            echo "RECOVERY_CHECK_OK $EXPECTED_CURRENT $TARGET_INPUT";;
        "recover $EXPECTED_CURRENT $TARGET_INPUT")
            [[ "$FAKE_CASE" != bad_deploy ]] || return 1
            echo "DEPLOYED $TARGET_INPUT";;
        *) return 91;;
    esac
}
'''
        script.write_text(mocks+'\n'+run, encoding='utf-8', newline='\n')
        for expected, target, check, case, count, ok in (
            (OLD, TARGET, 'true', 'ok', 1, True),
            (OLD, TARGET, 'false', 'ok', 2, True),
            ('', TARGET, 'false', 'ok', 0, False),
            (OLD, '', 'false', 'ok', 0, False),
            (OLD, OLD, 'false', 'ok', 0, False),
            (OLD+'; id', TARGET, 'false', 'ok', 0, False),
            (OLD, TARGET, 'false', 'bad_check', 1, False),
            (OLD, TARGET, 'false', 'bad_deploy', 2, False),
        ):
            with self.subTest(case=case, check=check, count=count):
                calls=self.root/'calls'; calls.unlink(missing_ok=True)
                env=os.environ|dict(EMERGENCY_RECOVERY='true',EXPECTED_CURRENT=expected,
                    TARGET_INPUT=target,CHECK_ONLY=check,FAKE_CASE=case,GITHUB_REF='refs/heads/main',
                    GITHUB_SHA=TARGET,ORACLE_SSH_PRIVATE_KEY='PRIVATE_TEST_SENTINEL',
                    ORACLE_SSH_KNOWN_HOSTS='PRIVATE_TEST_SENTINEL',RUNNER_TEMP=shell_path(self.root),
                    GITHUB_STEP_SUMMARY=shell_path(self.root/'summary'))
                p=subprocess.run([BASH,'--noprofile','--norc',shell_path(script)],env=env,
                                 capture_output=True,text=True,timeout=10)
                self.assertEqual(p.returncode==0,ok,p.stderr)
                self.assertEqual(len(calls.read_text().splitlines()) if calls.exists() else 0,count)
                self.assertNotIn('PRIVATE_TEST_SENTINEL',p.stdout+p.stderr)
                self.assertFalse(list(self.root.glob('tdb-ssh.*')))


if __name__ == '__main__':
    unittest.main()
