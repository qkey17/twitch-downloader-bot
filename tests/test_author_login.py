import asyncio,base64,json,subprocess,tempfile,unittest
from pathlib import Path
from src.html_renderer import render_viewer_html
from src.twitch_api.fetch_comments import message_row
from src.viewer_data import decode_row

CASES=[('情報本部','jjuos','情報本部 (jjuos)'),('中文','real_login','中文 (real_login)'),('한국어','korean','한국어 (korean)'),('Alice','alice','Alice'),('same','same','same'),('Only',None,'Only'),(None,'login','login'),(None,None,'unknown'),('A'*200,'long_login','A'*200+' (long_login)'),('<img src=x onerror=alert(1)>','</script>','<img src=x onerror=alert(1)> (</script>)')]
def rows():
    for display,login,_ in CASES:
        c=message_row(dict(commenter=dict(displayName=display,login=login),message=dict(fragments=[])))
        yield dict(t='00:00:01',user=c[2],login=c.login,text='Synthetic message',fragments=[],color='#abcdef',badges=[])

class Names(unittest.TestCase):
    def test_actual_javascript_labels(self):
        import re,shutil
        if not shutil.which('node'):self.skipTest('Node unavailable; browser review remains separate')
        template=Path('src/viewer_template.html').read_text(encoding='utf-8')
        function=template[template.index('function authorLabel('):template.index('function appendUserMessage(')]
        result=subprocess.run(['node','-e',function+';process.stdout.write(JSON.stringify('+json.dumps(list(rows()))+'.map(authorLabel)))'],capture_output=True,text=True,encoding='utf-8',check=True)
        self.assertEqual([c[2] for c in CASES],json.loads(result.stdout))

    def test_source_and_compact_roundtrip_escape(self):
        import re
        values=list(rows())
        page=render_viewer_html(values,'T','C','https://www.twitch.tv/videos/1')
        payload=json.loads(re.search(r'<script id="chat-data" type="application/json">(.*?)</script>',page,re.S)[1])
        for i,row in enumerate(values):self.assertEqual(row,decode_row(payload,payload['rows'][i]))
        self.assertEqual('</script>',decode_row(payload,payload['rows'][-1])['login'])
        self.assertNotIn('<img src=x onerror=alert(1)>',page)
        self.assertEqual(7,len(message_row({})))

if __name__=='__main__':
    unittest.main()
