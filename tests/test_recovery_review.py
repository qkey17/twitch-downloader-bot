"""Fast regressions of unchanged helper decisions; no system services or network."""
import ast
import os
from pathlib import Path
import subprocess
import sys
import unittest

from tests.test_oracle_deployment import BASH, SCRIPT

LAB = Path(__file__).with_name('deployment_recovery_lab.py')


class LabGuardTests(unittest.TestCase):
    def test_no_optimization_sensitive_assertions(self):
        self.assertFalse(any(isinstance(n, ast.Assert) for n in ast.walk(ast.parse(LAB.read_text()))))

    def test_optimized_python_still_refuses_before_mutation(self):
        code = '''
import importlib.util, sys
spec=importlib.util.spec_from_file_location('lab',sys.argv[1])
lab=importlib.util.module_from_spec(spec);spec.loader.exec_module(lab)
lab.os.geteuid=lambda: -1
try: lab.main()
except lab.LabError: pass
else: raise RuntimeError('isolation did not refuse')
try: lab.demand(False,'synthetic PASS predicate failure')
except lab.LabError: pass
else: raise RuntimeError('PASS guard disabled')
print('OPTIMIZED_GUARDS_REFUSED')
'''
        p = subprocess.run([sys.executable, '-I', '-B', '-O', '-c', code, str(LAB)],
                           capture_output=True, text=True, timeout=10)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(p.stdout.strip(), 'OPTIMIZED_GUARDS_REFUSED')


@unittest.skipUnless(BASH, 'Bash required')
class RecoveryDecisionTests(unittest.TestCase):
    def run_shell(self, code, env):
        return subprocess.run([BASH, '--noprofile', '--norc', '-s'], input=code,
                              env=os.environ | env, text=True, capture_output=True, timeout=10)

    def test_exact_restart_substates_are_refused(self):
        src = SCRIPT.read_text()
        fn = src[src.index('stopped_unit() {'):src.index('\nrecovery_preflight()')]
        fn = fn.replace('/usr/bin/systemctl', 'fake_systemctl')
        for active, sub in (('activating','auto-restart'), ('activating','auto-restart-queued'),
                            ('failed','failed-before-auto-restart'), ('inactive','dead-before-auto-restart'),
                            ('deactivating','stop-post'), ('reloading','reload')):
            with self.subTest(active=active, sub=sub):
                props = f'LoadState=loaded\nActiveState={active}\nSubState={sub}\nMainPID=0\nControlPID=0\nTasksCurrent=[not set]\nControlGroup=\nJob='
                code = 'set -Eeuo pipefail\nfail(){ exit 17; }\nfake_systemctl(){ printf "%s\\n" "$PROPS"; }\n'+fn+'\nstopped_unit tdb.service\n'
                self.assertEqual(self.run_shell(code, {'PROPS':props}).returncode, 17)

    def test_recovery_identity_failures_and_no_trap_retry(self):
        src = SCRIPT.read_text()
        update = src[src.index('update_and_start() {'):src.index('\nmain() {')]
        finish = src[src.index('finish() {'):src.index('\npreflight() {')].replace('/usr/bin/systemctl','fake_systemctl')
        cases = [('stable',True,1), ('first_probe_restart',False,1), ('stability_restart',False,1),
                 ('last_probe_restart',False,1), ('changed_before_start',False,0),
                 ('compile_fail',False,0), ('imports_fail',False,0), ('start_error',False,1),
                 ('start_limit',False,1)]
        for case, success, starts in cases:
            with self.subTest(case=case):
                code = FLOW_MOCKS+'\n'+finish+'\n'+update+'\ntrap finish EXIT\nupdate_and_start 11111111111111111111111111111111\n'
                p = self.run_shell(code, {'CASE':case})
                self.assertEqual(p.returncode == 0, success, p.stderr)
                self.assertEqual(p.stdout.count('START_REQUEST'), starts, p.stdout+p.stderr)
                self.assertEqual('DEPLOYED' in p.stdout, success)
                self.assertIn('FAST_FORWARD',p.stdout)
                if not success:
                    self.assertNotIn('Recovery start requested',p.stderr)


FLOW_MOCKS = r'''
set -Eeuo pipefail
exec 7>&1
stage=review; service_touched=0; manifest=''; previous=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
target=bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb; emergency=1
PROJECT=/synthetic; HELPER=/synthetic/helper; UNIT=tdb.service; START_TIMEOUT=0; STABILITY_SECONDS=10
phase=A; probes=0
fail(){ echo "REFUSED: $1" >&2; exit 17; }
note(){ echo "$1" >&2; }
fake_systemctl(){ :; }
git_cmd(){ if [[ "$1" == rev-parse ]]; then echo "$target"; fi; }
timeout(){
 if [[ "$*" == *'merge --ff-only'* ]]; then echo FAST_FORWARD >&7; fi
 [[ "$CASE" != compile_fail || "$*" != *compileall* ]] &&
 [[ "$CASE" != imports_fail || "$*" != *'import src.main'* ]]
}
recovery_idle(){ [[ "$CASE" != changed_before_start ]] || fail 'state changed before start'; }
property(){
 case "$1" in
 ActiveState) [[ "$CASE" == start_limit ]] && echo failed || echo active;;
 SubState) [[ "$CASE" == start_limit ]] && echo failed || echo running;;
 InvocationID) [[ "$phase" == A ]] && printf '%032d\n' 2 || printf '%032d\n' 3;;
 MainPID) [[ "$phase" == A ]] && echo 1001 || echo 1002;;
 NRestarts) [[ "$phase" == A ]] && echo 0 || echo 1;;
 esac
}
sudo(){
 if [[ "$*" == *'--no-block start'* ]]; then
  echo START_REQUEST >&7; [[ "$CASE" != start_error ]]; return
 fi
 probes=$((probes+1))
 if [[ ( "$CASE" == first_probe_restart && "$probes" == 1 ) || ( "$CASE" == last_probe_restart && "$probes" == 2 ) ]]; then phase=B; fi
 return 0
}
sleep(){ if [[ "$CASE" == stability_restart ]]; then phase=B; fi; }
'''
