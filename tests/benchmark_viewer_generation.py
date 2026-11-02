"""Opt-in writer resource comparison on local synthetic ChatSpool, never Oracle."""
import argparse,json,subprocess,sys,time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from src.chat_spool import ChatSpool,encode_row
from src.html_renderer import write_viewer_html
from src.resource_usage import memory_usage


def worker(args):
    write=write_viewer_html
    if args.worker=='base':
        module=Path(args.output).parent/'baseline'/'html_renderer.py'
        namespace={'__file__':str(module)};exec(compile(module.read_text(encoding='utf-8'),str(module),'exec'),namespace)
        write=namespace['write_viewer_html']
    spool=ChatSpool(Path(args.spool).parent.parent);spool.directory=Path(args.spool).parent;spool.path=Path(args.spool);spool.messages=args.count
    before=memory_usage();start=time.perf_counter()
    with Path(args.output).open('w',encoding='utf-8',newline='') as output:
        write(output,spool.iter_rows(),'Synthetic','Channel','https://www.twitch.tv/videos/123',mode=args.mode)
    print(json.dumps(dict(version=args.worker,count=args.count,mode=args.mode,seconds=time.perf_counter()-start,before=before,after=memory_usage(),html_bytes=Path(args.output).stat().st_size)))


def parent(args):
    from viewer_fixtures import synthetic_rows
    out=Path(args.output);out.mkdir(parents=True,exist_ok=True);base=out/'baseline';base.mkdir(exist_ok=True)
    for name in ('html_renderer.py','viewer_template.html'):
        (base/name).write_bytes(subprocess.check_output(['git','show',args.baseline+':src/'+name],cwd=ROOT))
    results=[]
    for count in (10000,25000,100000):
        spool=ChatSpool(out/'spools');spool.open()
        try:
            batch=[]
            for row in synthetic_rows(count):
                batch.append(encode_row(row))
                if len(batch)==500:spool.append_batch(batch);batch=[]
            if batch:spool.append_batch(batch)
            for mode in ('online','offline'):
                for version in ('base','new'):
                    target=out/f'writer-{version}-{count}-{mode}.html'
                    command=[sys.executable,'-B',__file__,'--worker',version,'--count',str(count),'--mode',mode,'--spool',str(spool.path),'--output',str(target)]
                    result=json.loads(subprocess.check_output(command,cwd=ROOT,text=True,encoding='utf-8',timeout=180))
                    results.append(result);print(json.dumps(result),flush=True)
                    (out/'writer-results.json').write_text(json.dumps(results,indent=2),encoding='utf-8')
        finally:spool.close()

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--output',required=True);p.add_argument('--baseline',default='1a089003b84fdb12c13d419aef16c01d53d8df83');p.add_argument('--worker',choices=['base','new']);p.add_argument('--count',type=int);p.add_argument('--mode',default='online');p.add_argument('--spool')
    args=p.parse_args();worker(args) if args.worker else parent(args)
