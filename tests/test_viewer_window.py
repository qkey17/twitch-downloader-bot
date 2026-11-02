"""Offline algorithm regressions; full browser matrix is opt-in separately."""
import shutil,subprocess,unittest
from pathlib import Path

class ViewerWindowTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('node'),'Node.js required')
    def test_window_and_search_javascript(self):
        result=subprocess.run([shutil.which('node'),'--test',str(Path(__file__).with_suffix('.js'))],capture_output=True,text=True,encoding='utf-8',timeout=30)
        self.assertEqual(0,result.returncode,result.stdout+result.stderr)
