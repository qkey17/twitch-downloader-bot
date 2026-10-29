"""Systemd-supervised daily maintenance; never starts the bot in this process."""
import argparse
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import importlib.util
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import stat
import struct
import sys
import threading
import time
import uuid

if __package__:
    from . import offsite_backup as offsite
else:
    spec = importlib.util.spec_from_file_location('tdb_offsite', Path(__file__).with_name('offsite_backup.py'))
    offsite = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(offsite)
snapshots = offsite.snapshots

DEFAULTS = dict(service='tdb.service', database='/home/ubuntu/twitch-downloader-bot/vod_history.db',
    html_roots=['/var/www/tdb/html'], admin_dir='/home/ubuntu/tdb-admin',
    backup_dir='/home/ubuntu/tdb-backups', offsite_state='/home/ubuntu/tdb-offsite',
    state_dir='/home/ubuntu/tdb-daily-state', socket='/run/tdb-maintenance/control.sock',
    deploy_lock='/home/ubuntu/.local/state/tdb-deploy/deploy.lock', remote=offsite.REMOTE,
    reserve_bytes=10*1024**3, window_start_utc='10:00', window_end_utc='13:00', retry_seconds=900,
    stop_timeout_seconds=600, backup_timeout_seconds=900, offsite_timeout_seconds=1800,
    start_timeout_seconds=180, stability_seconds=10, recovery_attempts=3,
    restart_retry_seconds=15, max_archives=3, budget_bytes=1500000000, account_reserve_bytes=300000000)
PROPERTIES = ('LoadState','ActiveState','SubState','MainPID','InvocationID','Job','TimeoutStopUSec',
              'KillSignal','KillMode','SendSIGKILL','RuntimeDirectoryPreserve','Result','ExecMainStatus','NRestarts')
FORMAT = 'tdb-daily-state-v1'


class DailyError(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


class Config:
    def __init__(self, values=None):
        values = values or {}
        if not isinstance(values, dict) or set(values)-set(DEFAULTS):
            raise DailyError('invalid_config_fields')
        self.__dict__.update(DEFAULTS | values)
        for name in ('database','admin_dir','backup_dir','offsite_state','state_dir','socket','deploy_lock'):
            setattr(self, name, snapshots._absolute(getattr(self, name)))
        if not isinstance(self.html_roots, list) or not 1 <= len(self.html_roots) <= 16:
            raise DailyError('invalid_html_roots')
        self.html_roots = [snapshots._absolute(p) for p in self.html_roots]
        for name, default in DEFAULTS.items():
            if type(default) is int and (type(getattr(self,name)) is not int or getattr(self,name) < 1):
                raise DailyError('invalid_numeric_limit')
        if self.reserve_bytes < 10*1024**3 or self.recovery_attempts > 5:
            raise DailyError('unsafe_reserve_or_recovery_limit')
        if (not isinstance(self.service, str) or not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.@-]*\.service',self.service)
                or self.service in ('tdb-daily-backup.service','tdb-backup-recover.service')):
            raise DailyError('invalid_bot_service')
        offsite._remote(self.remote)
        for value in (self.window_start_utc,self.window_end_utc):
            if not isinstance(value,str) or not re.fullmatch(r'(?:[01][0-9]|2[0-3]):[0-5][0-9]',value):
                raise DailyError('invalid_utc_window')
        if self.window_start_utc >= self.window_end_utc:
            raise DailyError('invalid_utc_window')
        protected = [self.database,*self.html_roots,self.admin_dir]
        outputs = [self.state_dir,self.backup_dir,self.offsite_state]
        if (any(snapshots._overlap(a,b) for a in outputs for b in protected)
                or any(snapshots._overlap(a,b) for i,a in enumerate(outputs) for b in outputs[i+1:])
                or any(snapshots._overlap(a,self.deploy_lock) or snapshots._overlap(a,self.socket) for a in outputs)):
            raise DailyError('overlapping_paths')
        normalized = {name:([str(p) for p in self.html_roots] if name=='html_roots' else
                      str(getattr(self,name)) if isinstance(getattr(self,name),Path) else getattr(self,name))
                      for name in DEFAULTS}
        self.binding = hashlib.sha256(json.dumps(normalized,sort_keys=True).encode('utf-8')).hexdigest()


class Store:
    """Small private atomic records; no backup data is removed by this module."""
    def __init__(self, directory):
        self.directory = directory
        offsite._private(directory, create=True)

    def read(self, name, default=None):
        path = self.directory/(name+'.json')
        if not path.exists() and not path.is_symlink():
            return default
        offsite._private_file(path)
        try:
            record = json.loads(snapshots._read_small(path, 65536),object_pairs_hook=snapshots._unique_object)
        except (ValueError,UnicodeError,RecursionError):
            raise DailyError('invalid_private_record') from None
        if not isinstance(record,dict) or set(record) != {'format','value'} or record['format'] != FORMAT:
            raise DailyError('invalid_private_record')
        return record['value']

    def write(self, name, value):
        offsite._private(self.directory)
        target = self.directory/(name+'.json')
        if target.exists() or target.is_symlink():
            offsite._private_file(target)
        content = json.dumps(dict(format=FORMAT,value=value),sort_keys=True).encode('utf-8')
        if len(content)>65536:
            raise DailyError('private_record_too_large')
        temporary = self.directory/('.record-'+uuid.uuid4().hex+'.tmp')
        with snapshots._new_file(temporary) as output:
            output.write(content)
        if os.name == 'posix':
            with snapshots._open_input(self.directory,directory=True) as fd:
                os.replace(temporary.name,target.name,src_dir_fd=fd,dst_dir_fd=fd)
        else:
            snapshots._no_links(self.directory)
            os.replace(temporary,target)
        snapshots._sync_directory(self.directory)


@contextmanager
def file_lock(path):
    """Open the deployment helper's existing inode, never create/truncate it."""
    before = offsite._private_file(path)
    if os.name == 'posix':
        with snapshots._open_input(path.parent,directory=True) as parent:
            fd = os.open(path.name,os.O_RDWR|os.O_NOFOLLOW,dir_fd=parent)
    else:
        fd = os.open(path,os.O_RDWR|getattr(os,'O_BINARY',0))
    try:
        if (before.st_dev,before.st_ino) != (os.fstat(fd).st_dev,os.fstat(fd).st_ino):
            raise DailyError('administration_lock_replaced')
        try:
            if os.name == 'posix':
                import fcntl
                fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
            else:
                import msvcrt
                msvcrt.locking(fd,msvcrt.LK_NBLCK,1)
        except OSError:
            raise DailyError('administration_busy') from None
        yield fd
    finally:
        os.close(fd)


class Gate:
    def __init__(self,path,pid):
        if os.name != 'posix' or not hasattr(socket,'SO_PEERCRED'):
            raise DailyError('linux_coordination_required')
        snapshots._no_links(path)
        offsite._private(path.parent)
        info = path.lstat()
        if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise DailyError('unsafe_control_socket')
        self.connection = socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
        try:
            self.connection.settimeout(60)
            self.connection.connect(str(path))
            peer,uid,_ = struct.unpack('3i',self.connection.getsockopt(socket.SOL_SOCKET,socket.SO_PEERCRED,12))
            if peer != pid or uid != os.geteuid():
                raise DailyError('unexpected_control_peer')
        except BaseException:
            self.connection.close()
            raise
        self.pid = pid

    def __enter__(self): return self
    def close(self): self.connection.close()
    def __exit__(self,*_): self.close()

    def request(self,action,token=None):
        self.connection.settimeout(60 if action=='prepare' else 10)
        request = dict(version=1,action=action)
        if token is not None: request['token']=token
        self.connection.sendall(json.dumps(request).encode('ascii')+b'\n')
        output = bytearray()
        while not output.endswith(b'\n'):
            chunk = self.connection.recv(4097-len(output))
            if not chunk or len(output)+len(chunk)>4096:
                raise DailyError('invalid_control_response')
            output.extend(chunk)
        try:
            result = json.loads(output)
        except (ValueError,UnicodeError):
            raise DailyError('invalid_control_response') from None
        if (not isinstance(result,dict) or set(result) != {'version','status','pid','token','committed','running'}
                or type(result['version']) is not int or result['version']!=1
                or type(result['pid']) is not int or result['pid']!=self.pid
                or type(result['committed']) is not bool or type(result['running']) is not bool
                or result['status'] not in ('ready','busy','committed','released','running','unavailable')
                or (result['token'] is not None and (not isinstance(result['token'],str)
                    or not re.fullmatch(r'[0-9a-f]{32}',result['token'])))):
            raise DailyError('invalid_control_response')
        return result

    def status(self): return self.request('status')
    def prepare(self,token): return self.request('prepare',token)
    def commit(self,token): return self.request('commit',token)
    def release(self,token): return self.request('release',token)


class Commands(offsite.External):
    def check(self,extra=0):
        # Recovery must still run when the disk reserve has been exhausted.
        if self.cancel.is_set() or time.monotonic()>=self.deadline:
            # External.process's watchdog catches OffsiteError, not DailyError.
            raise offsite.OffsiteError('command_interrupted_or_timed_out')


class Runtime:
    def __init__(self,config,config_path=None):
        self.config = config
        self.config_path = config_path or config.admin_dir/'daily-backup.json'
        self.cancel = threading.Event()
        self.lock_fds = []
        self.commands = Commands(config.state_dir,offsite.Limits(local_reserve=1),cancel=self.cancel)

    def now(self): return datetime.now(timezone.utc)
    def monotonic(self): return time.monotonic()
    def sleep(self,seconds):
        if self.cancel.wait(seconds): raise DailyError('interrupted')
    def boot_id(self):
        result = snapshots._read_small(Path('/proc/sys/kernel/random/boot_id'),64).decode('ascii').strip()
        if not re.fullmatch(r'[0-9a-f-]{36}',result): raise DailyError('invalid_boot_id')
        return result

    def command(self,argv,timeout=10):
        self.commands.deadline = time.monotonic()+timeout
        # Holding an inherited lease also protects against a killed parent while a child drains.
        self.commands.lock_fd = self.lock_fds[-1] if self.lock_fds else None
        try:
            return self.commands.run(argv)
        except offsite.OffsiteError:
            code = ('command_interrupted_or_timed_out' if self.cancel.is_set()
                    or time.monotonic()>=self.commands.deadline else 'external_command_failed')
            raise DailyError(code) from None

    def service(self):
        raw = self.command(['/usr/bin/systemctl','show',self.config.service,'--no-pager',
                            '--property='+','.join(PROPERTIES)])
        result = dict(line.split('=',1) for line in raw.decode('ascii').splitlines() if '=' in line)
        if set(result)!=set(PROPERTIES): raise DailyError('unknown_service_state')
        return result

    def ensure_supervised(self):
        invocation = os.getenv('INVOCATION_ID','')
        if not re.fullmatch(r'[0-9a-f]{32}',invocation): raise DailyError('run_requires_systemd_supervision')
        raw = self.command(['/usr/bin/systemctl','show','tdb-daily-backup.service','--no-pager',
                            '--property=MainPID,InvocationID,KillMode,SendSIGKILL,ExecStopPost,Type,User'])
        data = dict(line.split('=',1) for line in raw.decode('ascii').splitlines() if '=' in line)
        command = f'/usr/bin/python3 -I -S -B {self.config.admin_dir}/daily_backup.py --config {self.config_path} recover'
        if (data.get('MainPID')!=str(os.getpid()) or data.get('InvocationID')!=invocation
                or data.get('KillMode')!='control-group' or data.get('SendSIGKILL')!='yes'
                or data.get('Type')!='oneshot' or command not in data.get('ExecStopPost','')):
            raise DailyError('run_requires_systemd_supervision')
        raw = self.command(['/usr/bin/systemctl','show','tdb-backup-recover.service','--no-pager',
                            '--property=LoadState,UnitFileState,ExecStart,User'])
        recovery = dict(line.split('=',1) for line in raw.decode('ascii').splitlines() if '=' in line)
        if (recovery.get('LoadState')!='loaded' or recovery.get('UnitFileState')!='enabled'
                or recovery.get('User')!=data.get('User') or command not in recovery.get('ExecStart','')):
            raise DailyError('installed_boot_recovery_required')

    def gate(self,pid): return Gate(self.config.socket,pid)
    def manager_stopping(self):
        value = self.command(['/usr/bin/systemctl','show','--property=SystemState','--value']).decode('ascii').strip()
        if value not in ('initializing','starting','running','degraded','maintenance','stopping'):
            raise DailyError('unknown_system_manager_state')
        return value=='stopping'
    def stop(self):
        self.command(['sudo','-n','/usr/bin/systemctl','--no-block','stop',self.config.service])
    def start(self):
        # This exact restricted sudo command is already used by Oracle deployment.
        self.command(['sudo','-n','/usr/bin/systemctl','--no-block','start',self.config.service])

    def _tool(self,name,args,timeout):
        path = self.config.admin_dir/name
        offsite._private(self.config.admin_dir)
        offsite._private_file(path)
        raw = self.command([sys.executable,'-I','-S','-B',str(path),*map(str,args)],timeout)
        try: result = json.loads(raw)
        except (ValueError,UnicodeError): raise DailyError('invalid_tool_result') from None
        if not isinstance(result,dict) or result.get('status')!='ok': raise DailyError('tool_not_successful')
        return result

    def snapshot(self,destination):
        c = self.config
        args = ['backup','--database',c.database,'--destination',destination,'--service',c.service,
                '--maintenance-stopped','--reserve-bytes',c.reserve_bytes,'--timeout-seconds',c.backup_timeout_seconds]
        for root in c.html_roots: args += ['--html-root',root]
        return self._tool('backup_restore.py',args,c.backup_timeout_seconds+15)

    def verify(self,path):
        return self._tool('backup_restore.py',['verify','--backup',path,'--timeout-seconds',
                          self.config.backup_timeout_seconds],self.config.backup_timeout_seconds+15)

    def resume_for(self,path):
        c = self.config
        jobs = c.offsite_state/'jobs'
        if not jobs.exists(): return None
        fingerprint = snapshots._read_small(path/'COMPLETE',65).decode('ascii').strip()
        unfinished = [job for _,job in offsite._jobs(jobs) if job['remote']==c.remote
                      and job['state'] in ('prepared','uploading','staged','publishing')]
        if not unfinished: return None
        if len(unfinished)!=1 or unfinished[0]['manifest_sha256']!=fingerprint:
            raise DailyError('unrelated_unfinished_offsite_job')
        return unfinished[0]['id']

    def job_ids(self):
        jobs = self.config.offsite_state/'jobs'
        return [job['id'] for _,job in offsite._jobs(jobs)] if jobs.exists() else []

    def resolve_job(self,path,baseline):
        jobs = self.config.offsite_state/'jobs'
        if not jobs.exists(): return None
        fingerprint = snapshots._read_small(path/'COMPLETE',65).decode('ascii').strip()
        matches = [job for _,job in offsite._jobs(jobs) if job['id'] not in baseline
                   and job['remote']==self.config.remote and job['manifest_sha256']==fingerprint
                   and job['state'] in ('prepared','uploading','staged','publishing','verified')]
        if len(matches)>1: raise DailyError('ambiguous_offsite_outcome')
        return matches[0]['id'] if matches else self.resume_for(path)

    def offsite(self,path,resume=None):
        c = self.config
        args = ['--backup',path,'--state-dir',c.offsite_state,'--remote',c.remote,
                '--local-reserve-bytes',c.reserve_bytes,'--timeout-seconds',c.offsite_timeout_seconds,
                '--max-archives',c.max_archives,'--budget-bytes',c.budget_bytes,
                '--account-reserve-bytes',c.account_reserve_bytes]
        if resume is not None: args += ['--resume',resume]
        result = self._tool('offsite_backup.py',args,c.offsite_timeout_seconds+15)
        if result.get('cloud_verified') is not True: raise DailyError('offsite_not_verified')
        return result

    def recovery_mode(self): self.cancel.clear()


def _active(data):
    return (data.get('LoadState')=='loaded' and data.get('ActiveState')=='active'
            and data.get('SubState')=='running' and re.fullmatch(r'[1-9][0-9]*',data.get('MainPID',''))
            and re.fullmatch(r'[0-9a-f]{32}',data.get('InvocationID','')) and data.get('Job') in ('','0'))


def _dead(data):
    return (data.get('LoadState')=='loaded' and data.get('ActiveState')=='inactive'
            and data.get('SubState')=='dead' and data.get('MainPID')=='0' and data.get('Job') in ('','0'))


def _stopped(data):
    return (_dead(data) or (data.get('LoadState')=='loaded' and data.get('ActiveState')=='failed'
            and data.get('SubState')=='failed' and data.get('MainPID')=='0' and data.get('Job') in ('','0')))


def _process_identity(data):
    if not _active(data) or not re.fullmatch(r'[0-9]+',data.get('NRestarts','')):
        return None
    return data['MainPID'],data['InvocationID'],data['NRestarts']


class Controller:
    def __init__(self,config,runtime=None):
        self.config, self.runtime = config, runtime or Runtime(config)
        self.store = Store(config.state_dir)

    @contextmanager
    def locked(self,administration=True):
        with offsite._lease(self.config.state_dir) as fd:
            self.runtime.lock_fds = [fd]
            try:
                if administration:
                    with file_lock(self.config.deploy_lock) as admin_fd:
                        self.runtime.lock_fds.append(admin_fd)
                        yield
                else: yield
            finally:
                self.runtime.lock_fds = []

    def report(self,status,code,**details):
        result = dict(status=status,code=code,time=self.runtime.now().isoformat(),**details)
        self.store.write('status',result)
        return result

    def _owner(self):
        owner = self.store.read('owner')
        if owner is not None and (not isinstance(owner,dict) or set(owner)!=
                {'token','boot_id','invocation','pid','attempts','snapshot','binding'}
                or owner['binding']!=self.config.binding
                or not isinstance(owner['token'],str) or not re.fullmatch(r'[0-9a-f]{32}',owner['token'])
                or not isinstance(owner['boot_id'],str) or not re.fullmatch(r'[0-9a-f-]{36}',owner['boot_id'])
                or not isinstance(owner['invocation'],str) or not re.fullmatch(r'[0-9a-f]{32}',owner['invocation'])
                or type(owner['pid']) is not int or owner['pid']<1
                or type(owner['attempts']) is not int or not 0<=owner['attempts']<=5
                or not isinstance(owner['snapshot'],str)):
            raise DailyError('invalid_restart_ownership')
        return owner

    def _held(self):
        held = self.store.read('hold',False)
        if type(held) is not bool: raise DailyError('invalid_manual_hold')
        return held

    def _wait(self,predicate,timeout):
        end = self.runtime.monotonic()+timeout
        while True:
            if predicate(): return True
            if self.runtime.monotonic()>=end: return False
            self.runtime.sleep(min(1,max(0,end-self.runtime.monotonic())))

    def _ready(self,owner=None):
        return self._ready_process(owner) is not None

    def _ready_process(self,owner=None,read=None):
        read = read or self.runtime.service
        data = read()
        identity = _process_identity(data)
        if identity is None: return None
        try:
            with self.runtime.gate(int(data['MainPID'])) as gate:
                reply = gate.status()
                if owner is not None and reply['token']==owner['token']:
                    # All controller children are gone before an independent recovery
                    # can own both locks; no stop job is pending in this observed invocation.
                    if gate.release(owner['token'])['status']!='released': return None
                    reply = gate.status()
                if (reply['status']!='running' or not reply['running'] or reply['token'] is not None
                        or reply['committed']): return None
        except (OSError,ValueError,DailyError):
            return None
        return identity if _process_identity(read())==identity else None

    def _recovery_read(self,operation,deadline,code):
        while True:
            try: return operation()
            except (OSError,UnicodeError,DailyError,offsite.OffsiteError):
                if self.runtime.monotonic()>=deadline: raise DailyError(code) from None
            self.runtime.sleep(min(1,max(0,deadline-self.runtime.monotonic())))

    def _recover_locked(self):
        owner = self._owner()
        if owner is None: return dict(status='ok',code='nothing_to_recover')
        if self._held(): raise DailyError('critical_manual_hold_with_restart_ownership')
        self.runtime.recovery_mode()
        c = self.config
        remaining = max(0,c.recovery_attempts-owner['attempts'])
        now = self.runtime.monotonic()
        # Existing phase limits also bound repeated automatic restarts/query errors.
        deadline = now+c.stop_timeout_seconds+(remaining+1)*(c.start_timeout_seconds+c.stability_seconds)
        deadline += max(0,remaining-1)*c.restart_retry_seconds
        job_deadline, next_start = now+c.stop_timeout_seconds, now
        ready_process = ready_deadline = unknown_deadline = stable_process = stable_since = None

        def read():
            nonlocal ready_process, ready_deadline, stable_process, stable_since
            data = self._recovery_read(self.runtime.service,
                min(deadline,self.runtime.monotonic()+c.start_timeout_seconds),'critical_service_state_unavailable')
            process = _process_identity(data)
            if process is not None and process!=ready_process:
                # Observe every read, including the socket's before/after fence.
                # Only a confirmed new identity grants a new readiness window;
                # query errors, pending jobs and an unready socket do not.
                ready_process = process
                ready_deadline = min(deadline,self.runtime.monotonic()+c.start_timeout_seconds)
                stable_process = stable_since = None
            return data

        def stopping():
            return self._recovery_read(self.runtime.manager_stopping,
                min(deadline,self.runtime.monotonic()+c.start_timeout_seconds),'critical_system_manager_unavailable')

        while True:
            if self.runtime.monotonic()>=deadline: raise DailyError('critical_recovery_timed_out')
            if stopping(): return dict(status='deferred',code='recovery_deferred_until_boot')
            data = read()
            now = self.runtime.monotonic()
            if now>=deadline: raise DailyError('critical_recovery_timed_out')
            if _active(data):
                if ready_deadline is None: ready_deadline = now+c.start_timeout_seconds
                process = self._ready_process(owner,read)
                now = self.runtime.monotonic()
                if now>=deadline: raise DailyError('critical_recovery_timed_out')
                if process is None:
                    stable_process = stable_since = None
                elif process!=stable_process:
                    # The complete first probe must finish on time. Once timely,
                    # stability and its final probe use only the global deadline.
                    if now>ready_deadline: raise DailyError('critical_application_readiness_timeout')
                    stable_process,stable_since = process,now
                if stable_process is not None and now-stable_since>=c.stability_seconds:
                    self.store.write('owner',None)
                    return dict(status='ok',code='bot_restored')
                if stable_process is None and now>=ready_deadline:
                    raise DailyError('critical_application_readiness_timeout')
                unknown_deadline = None
            elif _stopped(data):
                stable_process = stable_since = None
                unknown_deadline = None
                if now>=next_start:
                    # Reconcile boot/autorestart jobs again immediately before
                    # consuming a durable attempt. Never start an uncertain unit.
                    if stopping(): return dict(status='deferred',code='recovery_deferred_until_boot')
                    if not _stopped(read()):
                        self.runtime.sleep(min(1,max(0,deadline-self.runtime.monotonic())))
                        continue
                    if self.runtime.monotonic()>=deadline: raise DailyError('critical_recovery_timed_out')
                    if owner['attempts']>=c.recovery_attempts:
                        raise DailyError('critical_restart_attempts_exhausted')
                    owner['attempts']+=1
                    self.store.write('owner',owner)
                    try: self.runtime.start()
                    except Exception: pass  # The next observed state reconciles a lost response.
                    now = self.runtime.monotonic()
                    ready_deadline = now+c.start_timeout_seconds
                    job_deadline = now+c.stop_timeout_seconds
                    next_start = now+c.restart_retry_seconds
            else:
                stable_process = stable_since = None
                if (data.get('LoadState')=='loaded' and
                        (data.get('Job') not in ('','0') or data.get('ActiveState') in
                         ('activating','deactivating','reloading'))):
                    unknown_deadline = None
                    if now>=job_deadline: raise DailyError('critical_service_job_did_not_settle')
                else:
                    if unknown_deadline is None: unknown_deadline = now+c.start_timeout_seconds
                    if now>=unknown_deadline: raise DailyError('critical_service_state_unknown')
            if self.runtime.monotonic()>=deadline: raise DailyError('critical_recovery_timed_out')
            self.runtime.sleep(min(1,max(0,deadline-self.runtime.monotonic())))

    def recover(self):
        with self.locked():
            try:
                result = self._recover_locked()
                if result['code']=='nothing_to_recover': return result
                return self.report(**result)
            except Exception as error:
                self._critical(error.code if isinstance(error,DailyError) else 'critical_recovery_failed')
                raise

    def _critical(self,code):
        try: data = self.runtime.service()
        except Exception: data = {}
        diagnostics = {key:data.get(key,'unknown') for key in
                       ('ActiveState','SubState','Result','ExecMainStatus','NRestarts')}
        # These are fixed systemd properties, never journal/config/exception output.
        try: self.report('critical',code,service=diagnostics)
        finally: print(json.dumps(dict(status='critical',code=code,service=diagnostics)),file=sys.stderr)

    def hold(self):
        with self.locked():
            if self._owner() is not None: raise DailyError('recover_before_manual_hold')
            self.store.write('hold',True)
            return self.report('ok','manual_hold_set')

    def unhold(self):
        with self.locked():
            if self._owner() is not None: raise DailyError('recover_before_manual_unhold')
            self.store.write('hold',False)
            return self.report('ok','manual_hold_cleared')

    def status(self):
        with self.locked(administration=False):
            return dict(status='ok',owner=self._owner(),hold=self._held(),last=self.store.read('status'),
                        day=self.store.read('day'),pending=self.store.read('pending'),candidate=self.store.read('candidate'))

    def _window(self):
        now = self.runtime.now().astimezone(timezone.utc)
        def at(value):
            hour,minute = map(int,value.split(':'))
            return now.replace(hour=hour,minute=minute,second=0,microsecond=0)
        return now,at(self.config.window_start_utc),at(self.config.window_end_utc)

    def _space(self):
        for path in (self.config.state_dir,self.config.backup_dir,self.config.offsite_state):
            offsite._private(path)
        for path in (self.config.state_dir,self.config.backup_dir,self.config.offsite_state,
                     self.config.database.parent,*self.config.html_roots):
            if shutil.disk_usage(path).free<self.config.reserve_bytes:
                raise DailyError('local_reserve_unavailable')

    def _candidate(self,value):
        if (not isinstance(value,dict) or set(value)!={'snapshot','date'}
                or not isinstance(value['snapshot'],str) or not isinstance(value['date'],str)
                or not re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}',value['date'])):
            raise DailyError('invalid_snapshot_candidate')
        path = snapshots._absolute(value['snapshot'])
        if path.parent!=self.config.backup_dir: raise DailyError('invalid_pending_snapshot')
        return path

    def _send_pending(self,pending):
        if (not isinstance(pending,dict) or set(pending)!={'snapshot','date','baseline'}
                or not isinstance(pending['baseline'],list) or len(pending['baseline'])>1000
                or any(not isinstance(i,str) or not re.fullmatch(r'[0-9a-f]{32}',i) for i in pending['baseline'])):
            raise DailyError('invalid_pending_send')
        path = self._candidate({key:pending[key] for key in ('snapshot','date')})
        self.runtime.verify(path)
        result = self.runtime.offsite(path,resume=self.runtime.resolve_job(path,pending['baseline']))
        self.store.write('receipt',dict(snapshot=str(path),date=pending['date'],offsite=result))
        candidate = self.store.read('candidate')
        if candidate is not None and self._candidate(candidate)==path:
            self.store.write('candidate',None)
        self.store.write('pending',None)
        return result

    def _finish_offsite(self):
        if not self._ready(): raise DailyError('bot_not_ready_before_offsite')
        result = None
        pending = self.store.read('pending')
        if pending is not None: result = self._send_pending(pending)
        candidate = self.store.read('candidate')
        if candidate is not None:
            self._candidate(candidate)
            pending = dict(**candidate,baseline=self.runtime.job_ids())
            self.store.write('pending',pending)
            result = self._send_pending(pending)
        if not self._ready(): raise DailyError('bot_not_ready_after_offsite')
        return result

    def run(self):
        self.runtime.ensure_supervised()
        with self.locked(administration=False):
            # Recovery owns its own nested administration interval; never begin a
            # new snapshot while a previous stop remains unresolved.
            if self._owner() is not None:
                with file_lock(self.config.deploy_lock) as fd:
                    self.runtime.lock_fds.append(fd)
                    try: self._recover_locked()
                    except Exception:
                        self._critical('critical_recovery_failed')
                        raise
                    finally: self.runtime.lock_fds.pop()
            now,begin,end = self._window()
            date = now.date().isoformat()
            day = self.store.read('day')
            if day is not None and (not isinstance(day,dict) or set(day)!={'date','state','snapshot'}):
                raise DailyError('invalid_daily_ledger')
            if self._held(): return self.report('skipped','manual_hold')
            if not begin<=now<=end: return self.report('skipped','outside_daily_window')
            if self._owner() is not None: raise DailyError('recovery_deferred')
            # A killed controller may have verified a snapshot without recording
            # candidate yet. Its durable day record supplies a bounded candidate.
            if (day and day['state'] in ('running','failed') and day['snapshot'] is not None
                    and self.store.read('candidate') is None):
                path = self._candidate(dict(snapshot=day['snapshot'],date=day['date']))
                if (path/'COMPLETE').exists():
                    try: self.runtime.verify(path)
                    except Exception: pass
                    else: self.store.write('candidate',dict(snapshot=str(path),date=day['date']))
            if day and day['date']==date:
                if self.store.read('pending') is not None or self.store.read('candidate') is not None:
                    result = self._finish_offsite()
                    day['state']='ok'
                    self.store.write('day',day)
                    return self.report('ok','pending_offsite_verified',offsite=result)
                receipt = self.store.read('receipt')
                if (isinstance(receipt,dict) and receipt.get('snapshot')==day['snapshot']
                        and receipt.get('date')==date and self._ready()):
                    day['state']='ok'
                    self.store.write('day',day)
                    return self.report('ok','previous_offsite_verified',offsite=receipt['offsite'])
                return self.report('skipped','day_already_attempted')
            destination = None
            try:
                deadline = self.runtime.monotonic()+max(0,(end-now).total_seconds())
                while begin<=self.runtime.now()<=end and self.runtime.monotonic()<=deadline:
                    try:
                        with file_lock(self.config.deploy_lock) as fd:
                            self.runtime.lock_fds.append(fd)
                            try:
                                if self._held(): return self.report('skipped','manual_hold')
                                initial = self.runtime.service()
                                if not _active(initial):
                                    self.store.write('day',dict(date=date,state='skipped',snapshot=None))
                                    return self.report('skipped','bot_not_active')
                                if any(initial.get(key)!=value for key,value in
                                       dict(TimeoutStopUSec='infinity',KillSignal='15',KillMode='control-group',
                                            SendSIGKILL='no',RuntimeDirectoryPreserve='restart').items()):
                                    raise DailyError('unsafe_bot_shutdown_configuration')
                                self._space()
                                token = uuid.uuid4().hex
                                with self.runtime.gate(int(initial['MainPID'])) as gate:
                                    reply = gate.prepare(token)
                                    if reply['status']=='ready' and reply['running'] and reply['token']==token and not reply['committed']:
                                        current = self.runtime.service()
                                        if not _active(current) or (current['MainPID'],current['InvocationID'])!=(initial['MainPID'],initial['InvocationID']):
                                            raise DailyError('bot_changed_during_preparation')
                                        if self.runtime.now()>end: break
                                        destination = self.config.backup_dir/('daily-'+now.strftime('%Y%m%dT%H%M%SZ')+'-'+token)
                                        self.store.write('day',dict(date=date,state='running',snapshot=str(destination)))
                                        self.store.write('owner',dict(token=token,boot_id=self.runtime.boot_id(),
                                            invocation=initial['InvocationID'],pid=int(initial['MainPID']),attempts=0,
                                            snapshot=str(destination),binding=self.config.binding))
                                        try:
                                            committed = gate.commit(token)
                                            if committed['status']!='committed' or not committed['committed']:
                                                raise DailyError('maintenance_commit_failed')
                                            self.runtime.stop()
                                            if not self._wait(lambda:_dead(self.runtime.service()),self.config.stop_timeout_seconds):
                                                raise DailyError('graceful_stop_not_confirmed')
                                            self.runtime.snapshot(destination)
                                            self.runtime.verify(destination)
                                            self.store.write('candidate',dict(snapshot=str(destination),date=date))
                                        finally:
                                            # COMMIT survives socket EOF. Close before recovery,
                                            # which independently proves jobs settled before release.
                                            gate.close()
                                            self._recover_locked()
                                        break
                                    if reply['status'] not in ('busy','unavailable'):
                                        raise DailyError('maintenance_not_confirmed')
                            finally: self.runtime.lock_fds.pop()
                    except DailyError as error:
                        if error.code!='administration_busy': raise
                    remaining = min((end-self.runtime.now()).total_seconds(),deadline-self.runtime.monotonic())
                    if remaining<=0: break
                    self.runtime.sleep(min(self.config.retry_seconds,remaining))
                if destination is None:
                    self.store.write('day',dict(date=date,state='skipped',snapshot=None))
                    return self.report('skipped','busy_window_expired')
                # TDB is running and healthy before any Dropbox work, including retries.
                result = self._finish_offsite()
                self.store.write('day',dict(date=date,state='ok',snapshot=str(destination)))
                return self.report('ok','backup_verified_offsite',snapshot=str(destination),offsite=result)
            except BaseException:
                # SIGKILL/power loss bypass this handler; ExecStopPost/boot recover
                # use the durable owner record, with the same finite attempt budget.
                if self._owner() is not None:
                    try:
                        with file_lock(self.config.deploy_lock): self._recover_locked()
                    except Exception:
                        self._critical('critical_recovery_failed')
                        raise DailyError('critical_recovery_failed') from None
                self.store.write('day',dict(date=date,state='failed',snapshot=str(destination) if destination else None))
                self.report('error','daily_backup_failed',snapshot=str(destination) if destination else None)
                raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',required=True,type=Path)
    parser.add_argument('action',choices=('run','recover','hold','unhold','status'))
    args = parser.parse_args(argv)
    previous = {}
    try:
        path = snapshots._absolute(args.config)
        offsite._private_file(path)
        config = Config(json.loads(snapshots._read_small(path,65536),object_pairs_hook=snapshots._unique_object))
        runtime = Runtime(config,path)
        for name in ('SIGINT','SIGTERM'):
            sig = getattr(signal,name,None)
            if sig is not None: previous[sig]=signal.signal(sig,lambda *_:runtime.cancel.set())
        result = getattr(Controller(config,runtime),args.action)()
    except Exception as error:
        code = error.code if isinstance(error,DailyError) else 'administrative_operation_failed'
        parser.exit(1,json.dumps(dict(status='error',code=code))+ '\n')
    finally:
        for sig,handler in previous.items(): signal.signal(sig,handler)
    print(json.dumps(result))


if __name__=='__main__': main()
