import json
from pathlib import Path
import shutil
import subprocess
import unittest
from src.ui.ui_formatters import _fmt_dt_utc, MONTHS_RU
from src.html_renderer import render_viewer_html


class UserDates(unittest.TestCase):
    def cases(self):
        result=[(f'2026-{m:02d}-02T03:04:00Z',f'2 {name} 2026 03:04 UTC') for m,name in enumerate(MONTHS_RU,1)]
        return result+[
            ('2026-10-29T19:28:00+05:00','29 окт 2026 14:28 UTC'),
            ('2026-01-01T00:15:00+01:00','31 дек 2025 23:15 UTC'),
            ('2026-10-29 14:28:00','29 окт 2026 14:28 UTC'),
            ('2026-10-29','29 окт 2026 00:00 UTC'),
            (' 2026-10-29T14:28:00Z ','29 окт 2026 14:28 UTC'),
            *[(v,'—') for v in (None,'',{},123,'bad','2026-02-30T10:00:00Z','0000-01-01','2026-13-01','2026-01-01T24:00:00Z')]]

    def test_all_months_utc_and_unknown(self):
        for value,expected in self.cases():
            with self.subTest(value=value):self.assertEqual(expected,_fmt_dt_utc(value))

    @unittest.skipUnless(shutil.which('node'),'Node required')
    def test_html_modes_match_python_and_png_uses_same_date(self):
        for mode in ('online','offline'):
            html=render_viewer_html([],'title','channel','vod',mode=mode)
            code=html[html.index('function fmtStreamStart(iso){'):html.index('const streamStart =')]
            program=code+'\nconst cases='+json.dumps(self.cases(),ensure_ascii=True)+';for(const [v,e] of cases){if((fmtStreamStart(v)||"—")!==e)throw Error(JSON.stringify([v,e,fmtStreamStart(v)]));}'
            p=subprocess.run([shutil.which('node'),'-e',program],capture_output=True,text=True,encoding='utf-8',timeout=15)
            self.assertEqual(0,p.returncode,p.stdout+p.stderr)
            self.assertIn('dateText.textContent = streamStart || "—"',html)
