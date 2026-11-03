"""Actual helper control flow with existing offline Git/systemd/Python doubles."""
import unittest
import test_registry_guard as registry
import test_oracle_deployment as deployment

OVERRIDES = r"""
git() {
    [[ "$1" == -c ]] && shift 2
    [[ "$1" == -C ]] && shift 2
    if [[ "$1" == ls-tree && "$*" == *'-- requirements.txt' ]]; then
        if [[ "$SCENARIO" == pillow_manifest_error && $(cat "$FAKE_ROOT/head") == "$FAKE_TARGET" ]]; then return 1; fi
        if [[ "$SCENARIO" == old_manifest || ( "$2" != "$FAKE_TARGET" && "$2" != HEAD && "$SCENARIO" != pillow_unchanged_missing ) ]]; then
            printf '100644 blob 384fc51190b284c4dd1ed4efa321bdcb7284bb46\trequirements.txt\n'
        else
            printf '100644 blob 5b606d55a0ba9a793ca91248e971598ab9ffaaca\trequirements.txt\n'
        fi
        return
    fi
    fixture_git "$@"
}
timeout() {
    if [[ "$2" == "$PROJECT/.venv/bin/python" && "$3" == -I && "$4" == -B ]]; then
        if [[ "$5" == - ]]; then
            event "pillow:$stage:$(cat "$FAKE_ROOT/head")"
            if [[ "$SCENARIO" == pillow_missing || "$SCENARIO" == pillow_unchanged_missing ||
                  ( $(cat "$FAKE_ROOT/head") == "$FAKE_TARGET" && ( "$SCENARIO" == pillow_post_update || "$SCENARIO" == imports_fail || "$SCENARIO" == signal_after_update_* ) ) ]]; then return 43; fi
        else
            event "runtime_pip:$stage"
            if [[ "$SCENARIO" == pillow_pip_post && $(cat "$FAKE_ROOT/head") == "$FAKE_TARGET" ]]; then return 1; fi
        fi
        return 0
    fi
    fixture_timeout "$@"
}

"""

@unittest.skipUnless(registry.BASH,'Bash required')
class PillowStartPaths(unittest.TestCase):
    run_case=deployment.OracleDeploymentTests.run_case
    def setUp(self):
        deployment.OracleDeploymentTests.setUp(self)
        source=self.harness.read_text(encoding='utf-8').removesuffix('${FAKE_ENTRY:-main}\n')
        source=source.replace('\ngit() {','\nfixture_git() {').replace('\ntimeout() {','\nfixture_timeout() {')
        source=source.replace('    stage=imports\n','    stage=imports\n    if [[ "$SCENARIO" == signal_after_update_* ]]; then kill -"${SCENARIO#signal_after_update_}" "$$"; fi\n')
        source=source.replace('    deadline=$((SECONDS + STOP_TIMEOUT))','    if [[ "$SCENARIO" == old_signal_* ]]; then kill -"${SCENARIO#old_signal_}" "$$"; fi\n    deadline=$((SECONDS + STOP_TIMEOUT))')
        self.harness.write_text(source+OVERRIDES+'\n${FAKE_ENTRY:-main}\n',encoding='utf-8',newline='\n')
    def test_ready_environment_is_checked_at_admission_and_before_one_start(self):
        p,events=self.run_case('pillow_ready');self.assertEqual(0,p.returncode,p.stderr)
        probes=[x for x in events if x.startswith('pillow:')]
        self.assertEqual(2,len(probes));self.assertTrue(probes[0].startswith('pillow:fetch:'))
        self.assertTrue(probes[1].startswith('pillow:start:'));self.assertEqual(1,events.count('start'))
    def test_bad_dependency_refuses_before_stop_or_update_even_on_later_release(self):
        for scenario in ('pillow_missing','pillow_unchanged_missing'):
            p,events=self.run_case(scenario);self.assertNotEqual(0,p.returncode)
            self.assertNotIn('stop',events);self.assertNotIn('start',events)
            self.assertFalse(any(e.startswith('git:merge ') for e in events))
    def test_prestart_failure_and_error_trap_never_start_new_manifest(self):
        for scenario in ('pillow_post_update','pillow_pip_post','pillow_manifest_error','imports_fail'):
            p,events=self.run_case(scenario);self.assertNotEqual(0,p.returncode)
            self.assertIn('stop',events);self.assertNotIn('start',events)
            self.assertEqual(deployment.TARGET,(self.root/'head').read_text().strip())
            self.assertIn('Recovery start withheld',p.stderr)
    def test_signal_traps_do_not_bypass_new_manifest_dependency(self):
        for signal in ('HUP','INT','TERM'):
            p,events=self.run_case('signal_after_update_'+signal);self.assertNotEqual(0,p.returncode)
            self.assertNotIn('start',events);self.assertIn('Recovery start withheld',p.stderr)
    def test_old_checkout_can_return_without_pillow_after_signals(self):
        for signal in ('HUP','INT','TERM'):
            p,events=self.run_case('old_signal_'+signal);self.assertNotEqual(0,p.returncode)
            self.assertEqual(deployment.OLD,(self.root/'head').read_text().strip())
            self.assertEqual(1,events.count('start'),(signal,p.returncode,p.stderr,events))
            self.assertEqual(1,sum(x.startswith('pillow:') for x in events)) # target admission only
    def test_ordinary_old_manifest_has_no_new_runtime_requirement(self):
        p,events=self.run_case('old_manifest');self.assertEqual(0,p.returncode,p.stderr)
        self.assertFalse(any(e.startswith('pillow:') for e in events));self.assertEqual(1,events.count('start'))
