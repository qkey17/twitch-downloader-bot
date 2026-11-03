"""Run the embedded probe in a disposable venv with synthetic PIL/pip modules.
No Pillow distribution is installed; fake decoders let each rejection be isolated.
"""
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
import venv
import test_registry_guard as registry

IMAGE = """from . import _imaging as core
class Picture:
    size=(2,2)
    mode='RGB'
    format=None
    def __enter__(self):return self
    def __exit__(self,*args):pass
    def save(self,out,format):out.write(format.encode())
    def load(self):pass
    def getpixel(self,position):return (32,64,96)
def new(*args):return Picture()
def open(buffer):
    result=Picture();result.format=buffer.read().decode();return result
"""

@unittest.skipUnless(registry.BASH,'Bash required')
class PillowRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='pillow-probe-');self.addCleanup(self.temp.cleanup)
        self.project=Path(self.temp.name)/'project';self.project.mkdir()
        self.env=os.environ|{'GIT_CONFIG_NOSYSTEM':'1','GIT_CONFIG_GLOBAL':os.devnull}
        self.git('init','-q');self.git('config','user.name','Synthetic');self.git('config','user.email','synthetic@example.invalid')
        self.git('config','core.autocrlf','false');self.git('config','core.hooksPath',str(self.project/'no-hooks'))
        self.req=self.project/'requirements.txt'
        self.old_req=b'aiohttp==3.13.5\npython-telegram-bot==22.7\n'
        self.req.write_bytes(self.old_req);self.old=self.commit()
        self.req.write_bytes(self.old_req+b'Pillow==12.3.0\n');self.new=self.commit()
        venv.EnvBuilder(with_pip=False).create(self.project/'.venv')
        native=self.project/'.venv'/('Scripts/python.exe' if os.name=='nt' else 'bin/python')
        site=Path(subprocess.check_output([str(native),'-I','-B','-c','import sysconfig; print(sysconfig.get_path("purelib"))'],text=True).strip())
        if os.name=='nt':
            wrapper=self.project/'.venv/bin/python';wrapper.parent.mkdir()
            wrapper.write_text('#!/bin/sh\nexec '+shlex.quote(registry.shell_path(native))+' "$@"\n',encoding='utf-8',newline='\n')
        self.pil=site/'PIL';self.pil.mkdir()
        (self.pil/'__init__.py').write_text("__version__='12.3.0'\n")
        (self.pil/'_imaging.py').write_text('')
        (self.pil/'Image.py').write_text(IMAGE)
        for name in ('ImageOps','ImageDraw'):(self.pil/(name+'.py')).write_text('')
        self.dist=site/'pillow-12.3.0.dist-info';self.dist.mkdir()
        self.metadata=self.dist/'METADATA';self.metadata.write_text('Metadata-Version: 2.1\nName: Pillow\nVersion: 12.3.0\n')
        self.pip=site/'pip';self.pip.mkdir();(self.pip/'__init__.py').write_text('');(self.pip/'__main__.py').write_text('raise SystemExit(0)')
        source=registry.SCRIPT.read_text(encoding='utf-8').removesuffix('main "$@"\n')
        source=source.replace('readonly PROJECT=/home/ubuntu/twitch-downloader-bot','readonly PROJECT='+shlex.quote(registry.shell_path(self.project)))
        self.harness=self.project/'probe.sh'
        self.harness.write_text(source+"\ngit_cmd() { command \"$REAL_GIT\" -C \"$FIXTURE\" \"$@\"; }\ncheck_runtime_dependencies \"$1\"\n",encoding='utf-8',newline='\n')
        self.private=self.project/'finished.html';self.private.write_bytes(b'PRIVATE_SENTINEL')
    def git(self,*args):
        return subprocess.check_output(['git','-C',str(self.project),*args],env=self.env,stderr=subprocess.PIPE).decode().strip()
    def commit(self):
        self.git('add','requirements.txt');self.git('commit','-qm','synthetic','--allow-empty');return self.git('rev-parse','HEAD')
    def probe(self,allowed,revision=None,category=None):
        p=subprocess.run([registry.BASH,'--noprofile','--norc',registry.shell_path(self.harness),revision or self.new],env=self.env|{'FIXTURE':registry.shell_path(self.project),'REAL_GIT':registry.shell_path(shutil.which('git'))},capture_output=True,text=True,timeout=20)
        self.assertEqual(allowed,p.returncode==0,p.stderr)
        if category:self.assertIn(category,p.stderr)
        self.assertNotIn('PRIVATE_SENTINEL',p.stdout+p.stderr)
        self.assertEqual(b'PRIVATE_SENTINEL',self.private.read_bytes())
        self.assertFalse(list(self.pil.rglob('__pycache__')))
        return p
    def test_new_manifest_and_unchanged_later_manifest_are_checked(self):
        self.probe(True)
        later=self.commit();self.probe(True,later)
        self.metadata.unlink();self.dist.rmdir();self.probe(False,later,'distribution is missing')
    def test_old_manifest_does_not_need_pillow(self):
        self.metadata.unlink();self.dist.rmdir();(self.pil/'__init__.py').write_text("raise RuntimeError('PRIVATE_SENTINEL')")
        self.probe(True,self.old)
    def test_missing_distribution_is_rejected(self):
        self.metadata.unlink();self.dist.rmdir();self.probe(False,category='distribution is missing')
    def test_distribution_and_pil_versions_are_both_checked(self):
        self.metadata.write_text('Name: Pillow\nVersion: 12.2.0\n');self.probe(False,category='both be 12.3.0')
        self.metadata.write_text('Name: Pillow\nVersion: 12.3.0\n')
        (self.pil/'__init__.py').write_text("__version__='12.2.0'");self.probe(False,category='both be 12.3.0')
    def test_python_and_native_import_failures_are_private(self):
        for name in ('ImageOps','ImageDraw','_imaging'):
            file=self.pil/(name+'.py');file.write_text("raise RuntimeError('PRIVATE_SENTINEL')")
            self.probe(False,category='Python/native imports failed');file.write_text('')
    def test_png_and_jpeg_round_trips_are_checked(self):
        for fmt in ('PNG','JPEG'):
            broken=IMAGE.replace('out.write(format.encode())',"out.write(('broken' if format=='"+fmt+"' else format).encode())")
            (self.pil/'Image.py').write_text(broken);self.probe(False,category='round-trip failed')
    def test_wrong_decoded_pixel_is_rejected(self):
        (self.pil/'Image.py').write_text(IMAGE.replace('return (32,64,96)','return (0,0,0)'))
        self.probe(False,category='round-trip failed')
    def test_pip_check_failure_prevents_admission(self):
        (self.pip/'__main__.py').write_text("print('PRIVATE_SENTINEL');raise SystemExit(1)")
        self.probe(False,category='pip check failed')
    def test_system_interpreter_is_not_a_substitute(self):
        source=self.harness.read_text(encoding='utf-8')
        source=source.replace('"$PROJECT/.venv/bin/python"',shlex.quote(registry.shell_path(sys.executable)))
        self.harness.write_text(source,encoding='utf-8',newline='\n')
        self.probe(False,category='unexpected virtual environment')
    def test_new_manifest_mode_is_checked_before_imports(self):
        self.git('update-index','--chmod=+x','requirements.txt');self.git('commit','-qm','synthetic mode')
        self.probe(False,self.git('rev-parse','HEAD'),'manifest mode')
    def test_timeout_and_manifest_read_failure_are_not_permission(self):
        source=self.harness.read_text(encoding='utf-8')
        self.harness.write_text(source.replace('check_runtime_dependencies "$1"','timeout() { return 124; }\ncheck_runtime_dependencies "$1"'),encoding='utf-8',newline='\n')
        self.probe(False,category='timed out')
        self.harness.write_text(source.replace('check_runtime_dependencies "$1"','git_cmd() { return 1; }\ncheck_runtime_dependencies "$1"'),encoding='utf-8',newline='\n')
        self.probe(False,category='could not inspect requirements')
