import contextlib, http.client, io, json, threading, traceback
from http.server import HTTPServer
from pathlib import Path
from rpe_harness import server as m
results=[]
class QuietHandler(m.HarnessHandler):
    def log_message(self,*args): pass
class ProbeServer(HTTPServer):
    errors=[]
    def handle_error(self, request, address):
        self.errors.append(traceback.format_exc())
s=ProbeServer(('127.0.0.1',0),QuietHandler)
t=threading.Thread(target=s.serve_forever,daemon=True);t.start()
def call(name,method,path,body=None):
    con=http.client.HTTPConnection('127.0.0.1',s.server_port,timeout=3)
    raw=body if isinstance(body,str) else json.dumps(body) if body is not None else None
    before=len(s.errors)
    try:
        con.request(method,path,body=raw,headers={'Content-Type':'application/json'})
        res=con.getresponse(); txt=res.read().decode();
        try: txt=json.loads(txt)
        except Exception: pass
        results.append({'name':name,'status':res.status,'response':txt})
    except Exception as e:
        results.append({'name':name,'exception':type(e).__name__+': '+str(e)})
    finally:
        if len(s.errors)>before:results[-1]['server_error']=s.errors[-1]
        con.close()
call('health','GET','/health')
call('unknown_task','POST','/route',{'task_type':'unknown','goal':'为新任务选合适工具'})
call('known_type_unsupported_format','POST','/route',{'task_type':'file.operation','input_format':'mp4','goal':'裁剪视频'})
call('capability_overrides_format','POST','/route',{'task_type':'file.operation','input_format':'pdf','required_capability':'document','goal':'提取PDF文字','source_ref':'evt-audit-1','session_id':'sess-audit-1','constraints':['file_exists']})
call('missing_preconditions','POST','/route',{'task_type':'data.processing','input_format':'xlsx','goal':'清理工作表','constraints':[]})
call('cold_discover_default','POST','/causal/discover',{'task_type':'document.writing','goal':'制作演示文稿'})
call('invalid_json','POST','/route','{broken')
call('body_is_array','POST','/route',[])
call('top_k_string','POST','/route',{'task_type':'conversation','goal':'聊天','top_k':'many'})
call('unsupported_task_type','POST','/route',{'task_type':'spreadsheet.deduplicate','goal':'清理重复数据'})
call('missing_skill_usage','POST','/usage',{'skill_id':'does-not-exist','success':True})
sid='productivity/xlsx'
before=m.STATE.router.skills[sid].use_count
call('valid_usage','POST','/usage',{'skill_id':sid,'success':False})
after=m.STATE.router.skills[sid].use_count
call('reload','POST','/reload',{})
results.append({'name':'usage_persistence','before':before,'after_usage':after,'after_reload':m.STATE.router.skills[sid].use_count})
s.shutdown();s.server_close();t.join(timeout=3)
out=Path('/tmp/rpe-harness-audit-20261013/api_results.json');out.write_text(json.dumps(results,ensure_ascii=False,indent=2))
for r in results:
    concise={k:v for k,v in r.items() if k!='server_error'}
    print(json.dumps(concise,ensure_ascii=False))
