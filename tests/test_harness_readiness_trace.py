"""Observer-only tests: no VM/systemd, including exact exceptions and redaction."""
import json
from pathlib import Path
import sys
from types import SimpleNamespace as N
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).with_name('systemd_harness')))
from readiness_trace import Trace


def identity(data):
    return data['MainPID'], data['InvocationID'], data['NRestarts']


def fixture():
    class Runtime:
        error = None
        def manager_stopping(self):
            return False
        def service(self):
            if self.error:
                raise self.error
            return dict(MainPID='123', InvocationID='a'*32, NRestarts='1', Job='', secret='DO_NOT_LOG')
    class Gate:
        def request(self, action, token=None):
            return dict(pid=123,status='running',running=True,committed=False,token='DO_NOT_LOG')
    class Controller:
        def __init__(self):
            self.runtime=Runtime()
        def _ready_process(self):
            data=self.runtime.service()
            reply=Gate().request(action='status',token='DO_NOT_LOG')
            return identity(data)
        def _recover_locked(self):
            deadline=100
            ready_deadline=8
            stable_since=None
            def read():
                nonlocal ready_deadline
                data=self.runtime.service()
                ready_deadline=9
                return data
            read()
            self._ready_process()
            return dict(code='bot_restored', secret='DO_NOT_LOG')
    return N(Runtime=Runtime,Gate=Gate,Controller=Controller,_process_identity=identity)


class ObserverTests(unittest.TestCase):
    def test_manager_result_and_constructor_timeout_are_observed_without_substitution(self):
        m=fixture(); writes=[]; error=TimeoutError(110,'DO_NOT_LOG')
        def failed_connect(self):
            raise error
        with patch.object(m.Gate,'__init__',new=failed_connect):
            with self.assertRaises(TimeoutError) as caught:
                with Trace(m,writes.append):
                    self.assertFalse(m.Runtime().manager_stopping())
                    m.Gate()
        self.assertIs(error,caught.exception)
        records=writes[0]['records']
        self.assertTrue(any(r['kind']=='manager_end' and r['result'] is False for r in records))
        self.assertTrue(any(r['kind']=='gate_connect_error' and r['errno']==110 for r in records))
        self.assertNotIn('DO_NOT_LOG',json.dumps(writes))

    def test_success_direct_locals_results_redaction_and_restoration(self):
        m=fixture(); original=m.Controller._recover_locked; service=m.Runtime.service
        writes=[]
        with Trace(m,writes.append):
            result=m.Controller()._recover_locked()
        self.assertEqual('DO_NOT_LOG',result['secret'])
        self.assertIs(original,m.Controller._recover_locked)
        self.assertIs(service,m.Runtime.service)
        self.assertNotIn('DO_NOT_LOG',json.dumps(writes))
        reads=[r for r in writes[0]['records'] if r.get('method')=='read']
        self.assertEqual(9,reads[0]['direct_locals']['ready_deadline'])
        self.assertEqual('bot_restored',writes[0]['outcomes'][0]['code'])
        self.assertTrue(any(r['kind']=='gate_connect_end' for r in writes[0]['records']))

    def test_exception_timeout_and_systemexit_are_preserved(self):
        for error in (ValueError('DO_NOT_LOG'),TimeoutError('DO_NOT_LOG'),SystemExit(1)):
            m=fixture(); writes=[]; controller=m.Controller(); controller.runtime.error=error
            with self.assertRaises(type(error)) as caught:
                with Trace(m,writes.append):
                    controller._recover_locked()
            self.assertIs(error,caught.exception)
            self.assertNotIn('DO_NOT_LOG',json.dumps(writes))
            self.assertEqual(type(error).__name__,writes[0]['outcomes'][0]['error_type'])
            self.assertTrue(writes[0]['outcomes'][0]['frames'])

    def test_bounded_records_keep_terminal_result(self):
        m=fixture(); writes=[]
        with Trace(m,writes.append,limit=2) as trace:
            for _ in range(20):
                m.Controller()._recover_locked()
        self.assertEqual(2,len(writes[0]['records']))
        self.assertLessEqual(len(writes[0]['outcomes']),16)
        self.assertGreater(writes[0]['dropped'],0)
        self.assertLess(len(json.dumps(writes[0])),15000)

    def test_missing_monitor_is_explicit_and_write_failure_does_not_mask_result(self):
        m=fixture(); writes=[]
        with patch.object(sys.monitoring,'get_tool',return_value='occupied'):
            with Trace(m,writes.append):
                result=m.Controller()._recover_locked()
        self.assertEqual('bot_restored',result['code'])
        self.assertEqual('local_monitor_unavailable',writes[0]['records'][0]['kind'])
        with patch('sys.stderr'):
            with Trace(m,lambda value: (_ for _ in ()).throw(OSError('DO_NOT_LOG'))):
                result=m.Controller()._recover_locked()
        self.assertEqual('bot_restored',result['code'])
