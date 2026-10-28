"""Real-systemd scenarios. Run ONLY in the offline, dedicated disposable guest."""
import argparse
from contextlib import closing
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
import uuid

if sys.platform == 'linux':
    import pwd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import guard
import guest

ROOT = guard.ROOT
DAILY = 'tdb-daily-backup.service'
RECOVER = 'tdb-backup-recover.service'
BOT = 'tdb.service'
TIMER = 'tdb-daily-backup.timer'


def demand(condition, description):
    if not condition:
        raise AssertionError(description)


class Lab:
    def __init__(self):
        guard.check()
        guard.require(os.geteuid() == 0, 'scenarios require guest root')
        self.case = None

    def events(self):
        if not self.case or not (self.case / 'events.jsonl').exists():
            return []
        return [json.loads(line) for line in (self.case / 'events.jsonl').read_text().splitlines()]

    def seen(self, name):
        return [e for e in self.events() if e['event'] == name]

    def await_event(self, name, timeout=15):
        return guest.wait(lambda: self.seen(name), timeout)[-1]

    def record(self, name):
        return json.loads(guest.user_command('record', name).stdout)

    def change(self, **values):
        path = self.case / 'control.json'
        guest.private_write(path, json.loads(path.read_text()) | values)

    def start(self, *units):
        guest.systemctl('start', '--no-block', *units)

    def idle(self, unit=DAILY, timeout=65):
        # Wait for job acceptance separately before using idle after a no-block start.
        return guest.wait(lambda: (s if s['ActiveState'] in ('inactive', 'failed')
                          and s.get('Job', '0') in ('', '0') else None)
                          if (s := guest.show(unit)) else None, timeout)

    def ready(self):
        return json.loads(guest.user_command('ready').stdout)

    def restored(self):
        guest.wait(lambda: self.record('owner') is None and self.ready(), 65)

    def kill(self, unit, sig):
        demand(unit in (DAILY, BOT), 'unexpected signal target')
        guest.systemctl('kill', '--kill-whom=main', f'--signal={sig}', unit)

    def new(self, **control):
        # All earlier case data/owner records are retained. Never reset an owner.
        for unit in (TIMER, DAILY, RECOVER, BOT):
            guest.systemctl('stop', '--no-block', unit)
            state = self.idle(unit, 85 if unit in (DAILY, RECOVER) else 12)
            # Inactive units can be garbage-collected as soon as idle() returns.
            # Failed units remain referenced with our default CollectMode; reset
            # only those, by exact name. Do not ignore manager/reset errors.
            if state['ActiveState'] == 'failed':
                guest.systemctl('reset-failed', unit)
        guest.systemctl('enable', RECOVER)
        case = ROOT / 'cases' / uuid.uuid4().hex
        case.mkdir(mode=0o700)
        uid, gid = pwd.getpwnam(guest.USER).pw_uid, pwd.getpwnam(guest.USER).pw_gid
        os.chown(case, uid, gid)
        for name in ('state', 'backups', 'offsite', 'data', 'html', 'locks'):
            path = case / name
            path.mkdir(mode=0o700)
            os.chown(path, uid, gid)
        self.case = case
        guest.private_write(ROOT / 'current.json', dict(case=str(case)))
        guest.private_write(case / 'control.json', control)
        guest.private_write(case / 'locks/deploy.lock', b'')
        guest.private_write(case / 'state/offsite.lock', b'')
        with closing(sqlite3.connect(case / 'data/vod_history.db')) as connection:
            connection.execute('CREATE TABLE synthetic_vods (id INTEGER PRIMARY KEY, result TEXT)')
            connection.execute('INSERT INTO synthetic_vods VALUES (1, "result.html")')
            connection.commit()
        for path in (case / 'data/vod_history.db', case / 'html/result.html'):
            if path.suffix == '.html':
                path.write_text('<!doctype html><title>SYNTHETIC ONLY</title><p>test VOD 1</p>')
            os.chmod(path, 0o600)
            os.chown(path, uid, gid)
        config = dict(database=str(case / 'data/vod_history.db'), html_roots=[str(case / 'html')],
                      admin_dir=str(guest.ADMIN), backup_dir=str(case / 'backups'),
                      offsite_state=str(case / 'offsite'), state_dir=str(case / 'state'),
                      socket='/run/tdb-harness/control.sock', deploy_lock=str(case / 'locks/deploy.lock'),
                      remote='synthetic:NEVER-NETWORK', window_start_utc='00:00', window_end_utc='23:59',
                      retry_seconds=1, stop_timeout_seconds=7, backup_timeout_seconds=12,
                      offsite_timeout_seconds=8, start_timeout_seconds=8, stability_seconds=1,
                      restart_retry_seconds=1, recovery_attempts=3)
        if control.get('fail_next_start'):
            # Four recovery entry points (finally/except/ExecStopPost/OnFailure)
            # may each observe automatic restart jobs. Keep this failure probe short.
            config.update(start_timeout_seconds=2, stop_timeout_seconds=2)
        guest.private_write(guest.CONFIG, config)
        self.start(BOT)
        self.await_event('bot_ready')
        demand(self.ready(), 'initial socket readiness')
        return self

    def cycle(self):
        self.start(DAILY)
        self.await_event('prepare')
        self.idle()

    def successful(self):
        demand(self.record('status')['code'] == 'backup_verified_offsite', 'successful controller status')
        demand(self.record('owner') is None and self.ready(), 'owner cleared only after ready')
        self.assert_order()

    def assert_order(self):
        events = self.events()
        names = [e['event'] for e in events]
        for name in ('prepare', 'bot_stopping', 'bot_stopped', 'snapshot_begin', 'snapshot_done',
                     'verify_done', 'offsite_begin', 'offsite_done'):
            demand(name in names, f'missing lifecycle event: {name}')
        indices = [names.index(name) for name in ('prepare', 'bot_stopping', 'bot_stopped',
                   'snapshot_begin', 'snapshot_done', 'verify_done', 'offsite_begin', 'offsite_done')]
        demand(indices == sorted(indices), 'prepare/stop/snapshot/verify/offsite order')
        sent = self.seen('offsite_done')[-1]
        ready = [e for e in self.seen('bot_ready') if str(e['pid']) == sent['main_pid']]
        demand(ready and sent['mono'] - ready[-1]['mono'] >= 1, 'offsite before stable replacement process')


def success(lab):
    lab.new().cycle()
    lab.successful()
    # Same-day rerun neither stops the process nor repeats the snapshot/offsite.
    before = len(lab.seen('snapshot_done'))
    lab.start(DAILY)
    lab.idle()
    demand(len(lab.seen('snapshot_done')) == before, 'same-day repeat created another snapshot')


def busy(lab):
    lab.new(busy=True)
    lab.start(DAILY)
    guest.wait(lambda: guest.show(DAILY)['MainPID'] != '0')
    time.sleep(2)
    demand(guest.show(DAILY)['ActiveState'] == 'activating', 'busy loop not running under systemd')
    demand(not lab.seen('prepare') and not lab.seen('bot_stopping'), 'active VOD was stopped')
    lab.kill(DAILY, 'SIGTERM')
    lab.idle()
    demand(lab.record('owner') is None and lab.ready(), 'busy run left ownership')


def controller_signal(lab, sig, during_stop=False):
    lab.new(block='snapshot', stop_delay=4 if during_stop else 0)
    lab.start(DAILY)
    lab.await_event('bot_stopping' if during_stop else 'snapshot_begin')
    demand(lab.record('owner') is not None, 'no persisted owner before signal')
    if during_stop:
        observed = guest.show(BOT)
        demand(observed['ActiveState'] == 'deactivating' and observed['Job'] not in ('', '0'),
               'did not observe a real pending stop job')
    lab.kill(DAILY, sig)
    lab.idle()
    lab.idle(RECOVER)
    lab.restored()
    demand(not lab.seen('offsite_done'), 'interrupted snapshot was sent')


def snapshot_failure(lab, phase):
    lab.new(fail=phase).cycle()
    lab.idle(RECOVER)
    lab.restored()
    demand(not lab.seen('offsite_done'), f'{phase} failure still sent offsite')
    demand(guest.show(DAILY)['Result'] != 'success', 'failure reported as success')


def start_failure(lab):
    lab.new(fail_next_start=True).cycle()
    lab.idle(RECOVER)
    owner = lab.record('owner')
    demand(owner is not None and 0 < owner['attempts'] <= 3, 'failed starts lost owner/budget')
    demand(lab.record('status')['status'] == 'critical', 'missing critical diagnostics')
    demand(not lab.seen('offsite_done'), 'failed start still sent offsite')


def slow_readiness(lab):
    lab.new(next_ready_delay=4)
    lab.start(DAILY)
    lab.await_event('snapshot_done')
    guest.wait(lambda: guest.show(BOT)['ActiveState'] == 'active')
    demand(lab.record('owner') is not None and not lab.seen('offsite_done'), 'active treated as socket readiness')
    lab.idle()
    lab.successful()


def autorestart(lab):
    lab.new(next_ready_delay=4)
    lab.start(DAILY)
    lab.await_event('snapshot_done')
    guest.wait(lambda: guest.show(BOT)['ActiveState'] == 'active')
    before = guest.show(BOT)
    lab.kill(BOT, 'SIGKILL')
    guest.wait(lambda: guest.show(BOT)['MainPID'] not in ('0', before['MainPID']))
    lab.idle()
    lab.successful()
    after = guest.show(BOT)
    demand(int(after['NRestarts']) >= 1 and before['InvocationID'] != after['InvocationID'],
           'did not observe real Restart=on-failure')


def exhausted(lab):
    lab.new()
    guest.user_command('seed-owner', '3')
    identity = guest.show(BOT)['InvocationID']
    result = guest.user_command('controller', 'recover')
    demand(json.loads(result.stdout)['code'] == 'bot_restored', 'active service cannot recover at limit')
    demand(guest.show(BOT)['InvocationID'] == identity, 'unnecessary new process at limit')
    guest.systemctl('stop', '--no-block', BOT)
    lab.idle(BOT)
    guest.user_command('seed-owner', '3')
    failed = guest.user_command('controller', 'recover', check=False)
    demand(failed.returncode != 0 and lab.record('owner')['attempts'] == 3, 'exhausted budget changed')
    demand(guest.show(BOT)['MainPID'] == '0', 'start attempted beyond budget')


def read_failures(lab):
    for mode in (1, 'always'):
        lab.new(read_fault=mode)
        guest.user_command('seed-owner', '0')
        identity = guest.show(BOT)['InvocationID']
        result = guest.user_command('controller', 'recover', check=False)
        demand(lab.seen('state_read_fault'), 'read fault was not injected')
        if mode == 1:
            demand(result.returncode == 0 and lab.record('owner') is None, 'transient failure not retried')
        else:
            demand(result.returncode != 0 and lab.record('owner')['attempts'] == 0, 'read error consumed owner/budget')
            demand(lab.record('status')['code'] == 'critical_service_state_unavailable', 'missing state-read diagnostic')
        demand(guest.show(BOT)['InvocationID'] == identity and not lab.seen('bot_stopping'),
               'state read failure changed running service')


def parallel_boot(lab):
    lab.new(ready_delay=2)
    guest.systemctl('stop', '--no-block', BOT)
    lab.idle(BOT)
    guest.user_command('seed-owner', '0')
    lab.start(RECOVER, BOT)  # One real manager transaction, neither ordered Before the other.
    lab.idle(RECOVER)
    lab.restored()
    demand(len(lab.seen('bot_start')) == 2, 'parallel start produced extra bot processes')


def persistent(lab):
    lab.new(ready_delay=2)
    guest.user_command('seed-owner', '0')
    timestamp = Path('/var/lib/systemd/timers/stamp-tdb-daily-backup.timer')
    marker = ROOT / 'timer-owned'
    guard.no_links(timestamp)
    timestamp.parent.mkdir(mode=0o755, exist_ok=True)
    demand(not timestamp.exists() or marker.exists(), 'unowned timer timestamp')
    if not timestamp.exists():
        timestamp.touch(mode=0o600, exist_ok=False)
    if not marker.exists():
        marker.touch(mode=0o600, exist_ok=False)
    os.utime(timestamp, (time.time() - 120, time.time() - 120))
    lab.start(RECOVER, TIMER)
    try:
        lab.await_event('offsite_done', 35)
        lab.idle()
        lab.restored()
        demand(guest.show(TIMER)['LastTriggerUSec'] not in ('', 'n/a'), 'Persistent catch-up did not fire')
        lab.successful()
    finally:
        guest.systemctl('stop', TIMER)


def holds_and_locks(lab):
    lab.new()
    guest.user_command('controller', 'hold')
    lab.start(DAILY)
    lab.idle()
    demand(lab.record('status')['code'] == 'manual_hold' and not lab.seen('prepare'), 'manual hold ignored')
    guest.user_command('controller', 'unhold')
    # Real deployment lock, no fake systemctl responses. Holder has a 4-second bound.
    process = subprocess.Popen(['/usr/sbin/runuser', '-u', guest.USER, '--', '/usr/bin/python3',
                                '-I', '-S', '-B', str(guest.CODE / 'synthetic.py'), 'lock', 'deploy'],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               env=dict(PATH='/usr/sbin:/usr/bin:/sbin:/bin', HOME=f'/home/{guest.USER}'))
    try:
        lab.await_event('lock_held')
        lab.start(DAILY)
        guest.wait(lambda: guest.show(DAILY)['MainPID'] != '0')
        time.sleep(.5)
        demand(not lab.seen('prepare') and lab.record('owner') is None, 'deployment lock ignored')
        other = guest.user_command('controller', 'recover', check=False)
        demand(other.returncode != 0, 'concurrent controller acquired same lease')
        # Unsupervised second run must still fail ensure_supervised.
        other = guest.user_command('controller', 'run', check=False)
        demand(other.returncode != 0 and 'run_requires_systemd_supervision' in other.stderr,
               'unsupervised second run accepted')
        lab.idle()
        lab.successful()
    finally:
        process.wait(timeout=6)


def offsite_fence(lab):
    lab.new(block='offsite')
    lab.start(DAILY)
    lab.await_event('offsite_begin')
    demand(lab.ready() and lab.record('owner') is None, 'offsite while not recovered')
    demand(guest.show(BOT)['ActiveState'] == 'active', 'bot stopped during offsite')
    lab.change(block=None)
    lab.idle()
    lab.successful()


CASES = (
    ('01-success-repeat', success), ('02-busy-vod', busy),
    ('03-sigterm', lambda lab: controller_signal(lab, 'SIGTERM')),
    ('04-sigkill-owner', lambda lab: controller_signal(lab, 'SIGKILL')),
    ('05-sigkill-stop-job', lambda lab: controller_signal(lab, 'SIGKILL', True)),
    ('06a-snapshot-error', lambda lab: snapshot_failure(lab, 'snapshot')),
    ('06b-verify-error', lambda lab: snapshot_failure(lab, 'verify')),
    ('07-start-error', start_failure), ('08-slow-ready', slow_readiness),
    ('09-auto-restart', autorestart), ('10-attempt-limit', exhausted),
    ('11-read-errors-INJECTED', read_failures), ('12-parallel-boot', parallel_boot),
    ('13-persistent-catchup', persistent), ('15-hold-locks-concurrency', holds_and_locks),
    ('16-offsite-fence', offsite_fence),
)


def run():
    lab = Lab()
    # Avoid crossing the real config's end-of-day window with a test that waits on busy.
    demand(datetime.now(timezone.utc).hour != 23, 'run suite outside 23:00-00:00 UTC')
    report = dict(base=guest.BASE, kind='REAL_SYSTEMD_WITH_SYNTHETIC_TOOLS',
                  systemd=guest.command(['/usr/bin/systemctl', '--version']).stdout.splitlines()[0],
                  python=sys.version.split()[0], cases=[], reboot='NOT_RUN: stage-powercut/resume required')
    report_path = ROOT / ('report-' + uuid.uuid4().hex + '.json')
    def save():
        guest.private_write(report_path, report, user=False)
    try:
        for name, scenario in CASES:
            print('RUN', name, flush=True)
            scenario(lab)
            units = {u: guest.show(u) for u in guard.UNITS}
            report['cases'].append(dict(name=name, status='PASS', case=str(lab.case),
                                        units={u: {k: state.get(k) for k in
                                               ('ActiveState', 'SubState', 'Result', 'MainPID', 'InvocationID',
                                                'NRestarts', 'MemoryPeak', 'CPUUsageNSec')} for u, state in units.items()}))
            save()
            print('PASS', name, flush=True)
    except BaseException as error:
        report['cases'].append(dict(name=name, status='FAIL', error=type(error).__name__,
                                    detail=str(error)[:240], case=str(lab.case)))
        save()
        raise
    finally:
        try:
            guest.cleanup()
            report['cleanup'] = 'PASS'
        except BaseException:
            report['cleanup'] = 'FAIL: discard/power off ONLY this VM; inspect remaining test units'
            raise
        finally:
            save()
            print('REPORT', report_path, flush=True)


def stage_powercut():
    lab = Lab().new(block='snapshot', powercut=True)
    guest.systemctl('enable', BOT, RECOVER)
    lab.start(DAILY)
    lab.await_event('snapshot_begin')
    owner = lab.record('owner')
    demand(owner is not None, 'missing crash ownership')
    guest.private_write(ROOT / 'powercut.json', dict(case=str(lab.case), owner=owner), user=False)
    os.sync()
    print('STAGED, NOT COMPLETE. Within 10 seconds quit ONLY this QEMU VM via its monitor.\n'
          'Restart the same private disk, then run scenarios.py resume.\n'
          'If you abandon this test, run guest.py cleanup inside this VM.', flush=True)
    # Deliberately no cleanup here: the next action is loss of this entire guest.
    # The synthetic barrier expires at 60s, so a missed powercut will fail resume.


def resume():
    lab = Lab()
    checkpoint = json.loads((ROOT / 'powercut.json').read_text())
    lab.case = guard.owned(Path(checkpoint['case']))
    try:
        boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
        demand(boot != checkpoint['owner']['boot_id'], 'guest did not reboot')
        lab.idle(RECOVER)
        lab.restored()
        demand(lab.record('status')['code'] == 'bot_restored', 'boot recovery did not clear owned crash')
        demand(lab.seen('snapshot_begin') and not lab.seen('snapshot_done'), 'crash barrier missed')
        demand(not lab.seen('offsite_done'), 'unexpected offsite on boot')
        guest.private_write(ROOT / 'reboot-result.json', dict(status='PASS', old_boot=checkpoint['owner']['boot_id'],
                                                             new_boot=boot, case=str(lab.case)), user=False)
        print('PASS 14-real-guest-power-loss-and-boot-recovery')
    finally:
        guest.cleanup()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('run', 'stage-powercut', 'resume'))
    args = parser.parse_args()
    try:
        guard.check()
        {'run': run, 'stage-powercut': stage_powercut, 'resume': resume}[args.action]()
    except (guard.Refused, OSError, ValueError, AssertionError, subprocess.SubprocessError) as error:
        sys.exit(f'REFUSED/FAILED: {error}')
