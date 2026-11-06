import subprocess, json, time, sys, os, threading, tempfile
env = dict(os.environ); env['PYTHONUNBUFFERED'] = '1'; env['PYTHONIOENCODING'] = 'utf-8'
env['APS_USER_DATA'] = os.path.join(os.path.abspath('.'), '_tmp_p3p5')
os.makedirs(env['APS_USER_DATA'], exist_ok=True)
p = subprocess.Popen([sys.executable, 'service.py'], stdin=subprocess.PIPE,
                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                     text=True, encoding='utf-8', env=env)
lines = []
lock = threading.Lock()
def reader():
    for line in p.stdout:
        with lock: lines.append(line.rstrip('\n'))
threading.Thread(target=reader, daemon=True).start()
def send(cmd, params=None, rid=None):
    p.stdin.write(json.dumps({'id': rid or cmd, 'command': cmd, 'params': params or {}}, ensure_ascii=False) + '\n'); p.stdin.flush()
def find(rid, timeout=15):
    end = time.time() + timeout
    while time.time() < end:
        with lock:
            for ln in lines:
                try: obj = json.loads(ln)
                except: continue
                if obj.get('id') == rid: return obj
        time.sleep(0.1)
    return None
time.sleep(4)
print('boot lines:', len(lines))
send('agent_kb_list'); print('kb_list ->', find('agent_kb_list'))
send('agent_kb_save', {'name':'测试.md','content':'人像修图保留质感'}); print('kb_save ->', find('agent_kb_save'))
send('agent_get_prompts'); print('prompts ->', find('agent_get_prompts'))
send('agent_watch_status'); print('watch_status(empty) ->', find('agent_watch_status'))
# start a watch on a temp folder, drop 3 files, threshold=2, quiet=3s
d = tempfile.mkdtemp()
send('agent_watch_start', {'id':'w1','name':'测试监听','watch_folder':d,'threshold':2,'quiet_seconds':3,'prompt':'处理新照片','auto_approve_destructive':True}, rid='ws')
print('watch_start ->', find('ws'))
time.sleep(3)  # let it seed seen
for i in range(3):
    open(os.path.join(d, f'img{i}.jpg'), 'wb').write(b'\xff\xd8\xff' + b'x'*(1000+i))
print('dropped 3 files, waiting for fire/progress...')
time.sleep(10)
send('agent_watch_status'); print('watch_status ->', find('agent_watch_status'))
# show watch events
with lock:
    we = [ln for ln in lines if 'agent_watch' in ln]
print('watch events:', we[-4:])
send('agent_watch_stop', {'id':'w1'}); print('watch_stop ->', find('agent_watch_stop'))
send('shutdown')
try: p.wait(timeout=5)
except: p.kill()
import shutil; shutil.rmtree(d, ignore_errors=True); shutil.rmtree(env['APS_USER_DATA'], ignore_errors=True)
print('DONE')
