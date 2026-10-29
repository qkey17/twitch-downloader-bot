"""Limited real-systemd qualification. ONLY a fresh, offline TDB QEMU guest.

Run as root in that disposable guest after copying reviewed sources to /opt/recovery-source.
No production paths, tokens, network, backup operations, or Telegram imports.
"""
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import stat
import struct
import subprocess
import time

ROOT = Path('/opt/recovery-lab')
SOURCE = Path('/opt/recovery-source')
UNIT = 'tdb-forward-recovery-lab.service'


class LabError(RuntimeError):
    pass


def demand(condition, message):
    if not condition:
        raise LabError(message)


def run(args, *, user=False, check=True, env=None, cwd=None, timeout=30):
    if user:
        args=['/usr/sbin/runuser','-u','ubuntu','--',*args]
    p=subprocess.run(args,capture_output=True,text=True,check=False,timeout=timeout,cwd=cwd,env=env)
    if check and p.returncode:
        raise AssertionError({'command':args[:3], 'rc':p.returncode, 'stdout':p.stdout, 'stderr':p.stderr})
    return p


def isolated():
    demand(os.geteuid()==0 and Path('/proc/1/comm').read_text().strip()=='systemd', "lab condition failed at original line 32")
    demand(Path('/sys/class/dmi/id/sys_vendor').read_text().strip()=='TDB-Harness', "lab condition failed at original line 33")
    demand(Path('/sys/class/dmi/id/product_name').read_text().strip()=='TDB-Isolated-VM', "lab condition failed at original line 34")
    demand(sorted(p.name for p in Path('/sys/class/net').iterdir())==['lo'], "lab condition failed at original line 35")
    demand(os.readlink('/proc/self/ns/pid')==os.readlink('/proc/1/ns/pid'), "lab condition failed at original line 36")
    demand(not Path('/.dockerenv').exists() and not Path('/run/systemd/container').exists(), "lab condition failed at original line 37")
    mounts=[l.split() for l in Path('/proc/self/mountinfo').read_text().splitlines()]
    demand(not any(m[m.index('-')+1] in ('9p','virtiofs','nfs','nfs4','cifs','fuse.sshfs') for m in mounts), "lab condition failed at original line 39")
    root=next(m for m in mounts if m[4]=='/')
    dev=Path('/dev/vda1').stat()
    demand(stat.S_ISBLK(dev.st_mode) and root[2]==f'{os.major(dev.st_rdev)}:{os.minor(dev.st_rdev)}', "lab condition failed at original line 42")
    with socket.socket(socket.AF_UNIX) as peer:
        peer.connect('/run/systemd/private')
        pid,uid,_=struct.unpack('3i',peer.getsockopt(socket.SOL_SOCKET,socket.SO_PEERCRED,12))
        demand((pid,uid)==(1,0), "lab condition failed at original line 46")
    demand(not any(Path(p).exists() for p in ('/home/ubuntu/twitch-downloader-bot','/home/ubuntu/tdb-admin','/var/www/tdb')), "lab condition failed at original line 47")
    demand(not ROOT.exists(), "lab condition failed at original line 48")


def properties():
    p=run(['/usr/bin/systemctl','show',UNIT,'--property=ActiveState','--property=SubState',
        '--property=MainPID','--property=InvocationID','--property=NRestarts','--property=Job'])
    return dict(l.split('=',1) for l in p.stdout.splitlines())


def main():
    isolated()  # Before any test mutation.
    ROOT.mkdir(mode=0o755)
    report={'status':'FAIL','cases':[], 'source_sha256':hashlib.sha256((SOURCE/'deploy/oracle_deploy.sh').read_bytes()).hexdigest()}
    unit=Path('/etc/systemd/system')/UNIT
    sudoers=Path('/etc/sudoers.d/tdb-forward-recovery-lab')
    try:
        import pwd
        try:
            account=pwd.getpwnam('ubuntu')
        except KeyError:
            run(['/usr/sbin/useradd','--create-home','--shell','/bin/bash','ubuntu'])
            account=pwd.getpwnam('ubuntu')
        work=ROOT/'work';work.mkdir();os.chown(work,account.pw_uid,account.pw_gid)
        project=work/'project';remote=work/'remote.git';seed=work/'seed'
        def git(*args,cwd=seed):
            return run(['/usr/bin/git',*args],user=True,cwd=cwd).stdout.strip()
        git('init','-b','main',str(seed),cwd=work)
        git('config','user.name','Recovery Lab');git('config','user.email','lab@example.invalid')
        run(['/usr/sbin/runuser','-u','ubuntu','--','mkdir',str(seed/'src')])
        versions=[]
        for label in ('working','broken','fixed','fixed-again'):
            code="import signal\nif __name__ == '__main__':\n"
            code += "    raise RuntimeError('synthetic broken update')\n" if label=='broken' else "    print('2030 | INFO | telegram.ext.Application | Application started',flush=True)\n    signal.pause()\n"
            # New commit even when two corrected versions have identical behavior.
            p=seed/'src/main.py';p.write_text('# '+label+'\n'+code);os.chown(p,account.pw_uid,account.pw_gid)
            git('add','.');git('commit','-qm',label);versions.append(git('rev-parse','HEAD'))
        working,broken,fixed,again=versions
        git('branch','working',working);git('init','--bare',str(remote),cwd=work)
        git('remote','add','origin',str(remote));git('push','-q','origin','main','working')
        git('clone','-q','--branch','working',str(remote),str(project),cwd=work)
        git('branch','-m','main',cwd=project)
        venv=project/'.venv/bin';venv.mkdir(parents=True)
        # This synthetic venv bypasses only pip check; imports/compile and bot use real Python.
        py=venv/'python';py.write_text('#!/bin/sh\nif [ "$*" = "-m pip check" ]; then exit 0; fi\nexec /usr/bin/python3 "$@"\n');py.chmod(0o755)
        run(['/usr/bin/chown','-R','ubuntu:ubuntu',str(project)])
        locks=work/'locks';locks.mkdir(mode=0o700);os.chown(locks,account.pw_uid,account.pw_gid)
        lock=locks/'deploy.lock';lock.touch(mode=0o600);os.chown(lock,account.pw_uid,account.pw_gid)
        fixtures=[]
        for name in ('synthetic.sqlite3','synthetic.html'):
            p=project/name;p.write_bytes(b'never modify synthetic user data');os.chown(p,account.pw_uid,account.pw_gid);fixtures.append(p)
        hashes={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in fixtures}
        helper=ROOT/'helper'
        code=(SOURCE/'deploy/oracle_deploy.sh').read_text()
        bindings={
            '/home/ubuntu/twitch-downloader-bot':str(project),
            '/home/ubuntu/.local/state/tdb-deploy':str(locks),
            '/usr/local/libexec/tdb-deploy':str(helper),
            '/home/ubuntu/tdb-daily-state':str(work/'daily'),
            '/home/ubuntu/tdb-admin/daily-backup.json':str(work/'admin/config.json'),
            'readonly RECOVERY_SOCKET=/run/tdb-maintenance/control.sock':'readonly RECOVERY_SOCKET='+str(work/'control.sock'),
            'readonly UNIT=tdb.service':'readonly UNIT='+UNIT,
            'https://github.com/qkey17/twitch-downloader-bot.git':str(remote),
        }
        for old,new in bindings.items():
            demand(old in code, "lab condition failed at original line 112");code=code.replace(old,new)
        helper.write_text(code);helper.chmod(0o755)
        report['bindings']=bindings;report['bound_helper_sha256']=hashlib.sha256(helper.read_bytes()).hexdigest()
        unit.write_text(f'''[Unit]
Description=Disposable forward recovery qualification
[Service]
Type=simple
User=ubuntu
Group=ubuntu
WorkingDirectory={project}
ExecStart={py} -m src.main
Restart=no
KillSignal=SIGTERM
KillMode=control-group
SendSIGKILL=no
TimeoutStopSec=infinity
StandardOutput=journal
StandardError=journal
''')
        sudoers.write_text(f'ubuntu ALL=(root) NOPASSWD: /usr/bin/systemctl --no-block start {UNIT}, /usr/bin/systemctl --no-block stop {UNIT}, {helper} ready\n');sudoers.chmod(0o440)
        run(['/usr/sbin/visudo','-cf',str(sudoers)])
        run(['/usr/bin/systemctl','daemon-reload'])
        def helper_call(request,check=True):
            return run(['/usr/bin/env','SSH_ORIGINAL_COMMAND='+request,str(helper)],user=True,check=check,timeout=240)
        run(['/usr/bin/systemctl','start',UNIT])
        end=time.monotonic()+40
        while run([str(helper),'ready'],check=False).returncode:
            demand(time.monotonic()<end, "lab condition failed at original line 139");time.sleep(.5)
        original=properties()
        run(['/usr/bin/systemctl','stop',UNIT])
        git('merge','--ff-only',broken,cwd=project)
        run(['/usr/bin/systemctl','start',UNIT],check=False)
        end=time.monotonic()+30
        while properties()['ActiveState']!='failed':
            demand(time.monotonic()<end, "lab condition failed at original line 146");time.sleep(.2)
        demand(helper_call('check',check=False).returncode!=0, "lab condition failed at original line 147")
        for initial,current,target in (('failed',broken,fixed),('inactive',fixed,again)):
            if initial=='inactive':run(['/usr/bin/systemctl','stop',UNIT])
            before=properties();demand(before['ActiveState']==initial and before['MainPID']=='0' and not before['Job'], "lab condition failed at original line 150")
            pre=helper_call(f'check-recovery {current} {target}')
            demand(pre.stdout.strip()==f'RECOVERY_CHECK_OK {current} {target}', "lab condition failed at original line 152")
            demand(git('rev-parse','HEAD',cwd=project)==current, "lab condition failed at original line 153")
            result=helper_call(f'recover {current} {target}')
            demand(result.stdout.strip()=='DEPLOYED '+target, "lab condition failed at original line 155")
            after=properties();demand(after['ActiveState']=='active' and after['SubState']=='running', "lab condition failed at original line 156")
            demand(after['MainPID']!='0' and after['InvocationID']!=before['InvocationID'] and after['NRestarts']=='0', "lab condition failed at original line 157")
            demand(git('rev-parse','HEAD',cwd=project)==target, "lab condition failed at original line 158")
            demand(all(hashlib.sha256(p.read_bytes()).hexdigest()==hashes[p.name] for p in fixtures), "lab condition failed at original line 159")
            report['cases'].append({'initial':initial,'before':before,'after':after,'sha':target,'stdout':result.stdout,'stderr':result.stderr})
        report.update(status='PASS',original=original,versions=versions,data_sha256=hashes)
    except Exception as e:
        report['error']=str(e)
    finally:
        report['journal']=run(['/usr/bin/journalctl','-u',UNIT,'--no-pager','-o','short-monotonic','-n','100'],check=False).stdout
        run(['/usr/bin/systemctl','stop',UNIT],check=False)
        report['final_state']=properties()
        for p in (unit,sudoers):p.unlink(missing_ok=True)
        run(['/usr/bin/systemctl','daemon-reload'])
        report['cleanup']=(report['final_state']['MainPID']=='0' and report['final_state']['ActiveState']=='inactive'
                           and not report['final_state']['Job'] and not unit.exists() and not sudoers.exists())
        if not report['cleanup']:
            report.update(status='FAIL', error='lab cleanup incomplete')
        (ROOT/'report.json').write_text(json.dumps(report,indent=2))
    return report


if __name__=='__main__':
    result = main()
    print(json.dumps(result,indent=2))
    raise SystemExit(0 if result['status']=='PASS' and result['cleanup'] else 1)
