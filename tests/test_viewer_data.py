import io,json,re,shutil,subprocess,unittest
from pathlib import Path
from src.html_renderer import render_viewer_html,write_viewer_html
from src.viewer_data import CompactRows,decode_row,FORMAT,FIELDS,USER_LIMIT,BADGE_LIMIT


def payload(html):
    return json.loads(re.search(r'<script id="chat-data" type="application/json">(.*?)</script>',html,re.S)[1])


class CompactDataTests(unittest.TestCase):
    def records(self):
        values=['01 : 02 : 03','</SCRIPT> Пользователь 🎮','long text & \" '*100,[{'text':'Kappa','emote':{'id':'25'},'emoteName':'Kappa'},{'text':'x<>&','unknown':True}],'#ABCDEF',[{'set_id':'subscriber','version':'1','extra':'✓'}]]
        for mask in range(64):
            yield {key:values[i] for i,key in enumerate(FIELDS) if mask & (1<<i)}
        yield dict(user='é e\u0301 🎮',text='line\u2028paragraph\u2029RTL\u202e & \"')
        for value in (None,False,0,'',[],{}):
            yield dict.fromkeys(FIELDS,value)
        yield dict(t='x',user='same',color='red',text='text',emotes={'Kappa':'url'},timestamp=123,**{'__proto__':{'polluted':True},'constructor':'safe'})

    def test_exact_lossless_round_trip_and_both_modes(self):
        source=list(self.records())
        for mode in ('online','offline'):
            data=payload(render_viewer_html(source,'</script>','channel','vod',mode=mode))
            self.assertEqual(FORMAT,data['format'])
            self.assertEqual(source,[decode_row(data,r) for r in data['rows']])
            self.assertTrue(all(isinstance(r,list) for r in data['rows']))
            self.assertEqual(mode,data['meta']['mode'])

    def test_bounded_dictionaries_and_inline_fallback(self):
        codec=CompactRows();encoded=[];source=[]
        for i in range(USER_LIMIT+10):
            row=dict(user=f'u{i}',color=f'#{i}',badges=[{'set_id':str(i),'version':'1'}],text=str(i))
            source.append(row);encoded.append(codec.encode(row))
        data=dict(format=FORMAT,users=codec.users.values,badges=codec.badges.values)
        self.assertEqual(USER_LIMIT,len(data['users']));self.assertEqual(BADGE_LIMIT,len(data['badges']))
        self.assertEqual(source,[decode_row(data,r) for r in encoded])
        self.assertIsInstance(encoded[-1][1],list);self.assertIsInstance(encoded[-1][4],list)
        huge=dict(user='Ю'*3000,badges=[{'set_id':'X'*5000}])
        self.assertEqual(huge,decode_row(data,codec.encode(huge)))

    def test_tables_do_not_retain_mutable_source_subobjects(self):
        codec=CompactRows();row=dict(user='u',color=None,badges=[{'set_id':'old'}]);r=codec.encode(row)
        row['badges'][0]['set_id']='new'
        self.assertEqual([{'set_id':'old'}],codec.badges.values[r[4]])

    def test_empty_payload_and_streaming_same_bytes(self):
        for count in (0,1,10000):
            def source():
                for i in range(count):yield dict(t='00:00:01',user='u',color=None,text=str(i),badges=[],fragments=[])
            out=io.StringIO();write_viewer_html(out,source(),'t','c','v')
            data=payload(out.getvalue());self.assertEqual(count,len(data['rows']))
            self.assertEqual(out.getvalue(),render_viewer_html(source(),'t','c','v'))

    def test_large_offline_catalog_is_written_in_bounded_pieces(self):
        class Sink:
            largest = 0
            def write(self,text):
                self.largest=max(self.largest,len(text))
                return len(text)
        sink=Sink()
        write_viewer_html(sink,iter([]),'t','c','v',mode='offline',local_emotes={str(i):'x'*1024 for i in range(10000)})
        self.assertLess(sink.largest,100000)

    @unittest.skipUnless(shutil.which('node'),'Node required')
    def test_actual_js_lazy_decoder_legacy_and_xss_safe_values(self):
        source=list(self.records())
        data=payload(render_viewer_html(source,'t','c','v'))
        template=Path('src/viewer_template.html').read_text(encoding='utf-8')
        functions=template[template.index('function messageUser('):template.index('const MODE =')]
        program='const DATA=JSON.parse('+json.dumps(json.dumps(data,ensure_ascii=True))+');const COMPACT_DATA=true;'+functions
        program+='console.log(JSON.stringify(DATA.rows.map(decodeMessage)));if({}.polluted)throw Error("prototype changed");'
        r=subprocess.run([shutil.which('node')],input=program,capture_output=True,text=True,encoding='utf-8',timeout=20)
        self.assertEqual(0,r.returncode,r.stderr);self.assertEqual(source,json.loads(r.stdout))
        legacy='const COMPACT_DATA=false;'+functions+';const r={text:"legacy"};if(decodeMessage(r)!==r||messageText(r)!=="legacy")throw Error("legacy");'
        r=subprocess.run([shutil.which('node'),'-e',legacy],capture_output=True,text=True,timeout=10)
        self.assertEqual(0,r.returncode,r.stderr)
