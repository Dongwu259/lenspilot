"""
smoke_test.py — Comprehensive smoke test for Python backend.
Exercises key subsystems: IPC loop, model loading, scoring, cache,
preference engine, agent module, KB, skills, and config persistence.

Usage: python smoke_test.py  (from python_backend directory)
"""

import subprocess, json, time, sys, os, threading, tempfile, shutil

PASS = 0
FAIL = 0

def ok(label):
    global PASS
    PASS += 1
    print(f"  ✅ {label}")

def ng(label, detail=""):
    global FAIL
    FAIL += 1
    print(f"  ❌ {label} {detail}")

env = dict(os.environ)
env['PYTHONUNBUFFERED'] = '1'
env['PYTHONIOENCODING'] = 'utf-8'
td = tempfile.mkdtemp(prefix='aps_smoke_')
env['APS_USER_DATA'] = td
os.makedirs(os.path.join(td, 'agent_kb'), exist_ok=True)
os.makedirs(os.path.join(td, 'skills'), exist_ok=True)

print(f"[smoke] user_data: {td}")

p = subprocess.Popen(
    [sys.executable, 'service.py'],
    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    text=True, encoding='utf-8', env=env,
)
lines = []
lock = threading.Lock()

def reader():
    for line in p.stdout:
        with lock:
            lines.append(line.rstrip('\n'))
threading.Thread(target=reader, daemon=True).start()

def send(cmd, params=None, rid=None):
    obj = {'id': rid or cmd, 'command': cmd, 'params': params or {}}
    p.stdin.write(json.dumps(obj, ensure_ascii=False) + '\n')
    p.stdin.flush()

def find(rid, timeout=20):
    end = time.time() + timeout
    while time.time() < end:
        with lock:
            for ln in lines:
                try:
                    obj = json.loads(ln)
                except Exception:
                    continue
                if obj.get('id') == rid:
                    return obj
        time.sleep(0.1)
    return None

def has_event(event_name):
    with lock:
        return any(event_name in ln for ln in lines)

# Wait for backend ready
time.sleep(3)
if has_event('"ready":true'):
    ok('backend started')
else:
    ng('backend not ready')
print(f"  boot messages: {len(lines)}")

# ── 1. Config ──
print("\n── Config ──")
send('get_config')
cfg = find('get_config')
if cfg and cfg.get('result', {}).get('config'):
    ok('get_config')
else:
    ng('get_config', cfg)

send('save_config', {'config': {'smoke_test': True}})
saved = find('save_config')
if saved and saved.get('result', {}).get('ok'):
    ok('save_config')
else:
    ng('save_config', saved)

# ── 2. KB ──
print("\n── Knowledge Base ──")
send('agent_kb_list')
kb = find('agent_kb_list')
if kb and 'documents' in (kb.get('result') or {}):
    ok('kb_list')
else:
    ng('kb_list', kb)

send('agent_kb_save', {'name': 'smoke.md', 'content': '# Smoke\nTest content for smoke testing.'})
kb_saved = find('agent_kb_save')
if kb_saved and kb_saved.get('result', {}).get('ok'):
    ok('kb_save')
else:
    ng('kb_save', kb_saved)

send('agent_kb_get', {'name': 'smoke.md'})
kb_get = find('agent_kb_get')
if kb_get and kb_get.get('result', {}).get('name') == 'smoke.md':
    ok('kb_get')
else:
    ng('kb_get', kb_get)

send('agent_kb_delete', {'name': 'smoke.md'})
kb_del = find('agent_kb_delete')
if kb_del and kb_del.get('result', {}).get('ok'):
    ok('kb_delete')
else:
    ng('kb_delete', kb_del)

# ── 3. Prompts ──
print("\n── Prompts ──")
send('agent_get_prompts')
pr = find('agent_get_prompts')
if pr and 'prompts' in (pr.get('result') or {}):
    ok('agent_get_prompts')
else:
    ng('agent_get_prompts', pr)

# ── 4. Preference engine ──
print("\n── Preference Engine ──")
send('get_calibration_status')
cal = find('get_calibration_status')
if cal and 'calibration_ready' in (cal.get('result') or {}):
    ok('calibration_status')
else:
    ng('calibration_status', cal)

# ── 5. Photo scorer (no model loaded yet) ──
print("\n── Photo Scorer ──")
send('get_model_status')
ms = find('get_model_status')
if ms:
    ok('model_status')
else:
    ng('model_status', ms)

send('list_images_in_folder', {'folder': td})
ls = find('list_images_in_folder')
if ls and 'images' in (ls.get('result') or {}):
    ok('list_images_in_folder')
else:
    ng('list_images_in_folder', ls)

# ── 6. Cache ──
print("\n── Cache ──")
send('get_cache_info')
ci = find('get_cache_info')
if ci and 'count' in (ci.get('result') or {}):
    ok('get_cache_info')
else:
    ng('get_cache_info', ci)

send('clear_score_cache')
cc = find('clear_score_cache')
if cc and cc.get('result', {}).get('ok'):
    ok('clear_score_cache')
else:
    ng('clear_score_cache', cc)

# ── 7. Backend pref ──
print("\n── Backend ──")
send('get_backend_pref')
bp = find('get_backend_pref')
if bp and 'backend' in (bp.get('result') or {}):
    ok('get_backend_pref')
else:
    ng('get_backend_pref', bp)

send('get_cuda_available')
cu = find('get_cuda_available')
if cu:
    ok('get_cuda_available')
else:
    ng('get_cuda_available', cu)

# ── 8. Skills ──
print("\n── Skills ──")
send('skill_list')
sl = find('skill_list')
if sl:
    ok('skill_list')
else:
    ng('skill_list', sl)

# ── 9. Agent watcher ──
print("\n── Agent Watcher ──")
send('agent_watch_status')
ws = find('agent_watch_status')
if ws and 'watches' in (ws.get('result') or {}):
    ok('watch_status')
else:
    ng('watch_status', ws)

# Start a watcher on a temp folder
wd = tempfile.mkdtemp(dir=td)
send('agent_watch_start', {
    'id': 'smoke_w1', 'name': 'Smoke Watch',
    'watch_folder': wd, 'threshold': 2, 'quiet_seconds': 3,
    'prompt': 'count files', 'auto_approve_destructive': True,
}, rid='ws_start')
wsr = find('ws_start', timeout=10)
if wsr and wsr.get('result', {}).get('ok'):
    ok('watch_start')
else:
    ng('watch_start', wsr)

# Drop files and wait for detection
time.sleep(2)
for i in range(3):
    open(os.path.join(wd, f'smoke{i}.txt'), 'w').write(f'test{i}')
time.sleep(8)

send('agent_watch_status')
ws2 = find('agent_watch_status')
if ws2:
    ok('watch_running')
else:
    ng('watch_running', ws2)

send('agent_watch_stop', {'id': 'smoke_w1'})
wstop = find('agent_watch_stop')
if wstop and wstop.get('result', {}).get('ok'):
    ok('watch_stop')
else:
    ng('watch_stop', wstop)

# ── 10. Data flywheel (opt-in selection feedback) ──
print("\n── Data Flywheel ──")
send('feedback_get_status')
fs = find('feedback_get_status')
if fs and 'pending' in (fs.get('result') or {}) and 'enabled' in (fs.get('result') or {}):
    ok('feedback_get_status')
else:
    ng('feedback_get_status', fs)

# No cloud token → uploader must skip gracefully (never throw)
send('feedback_sync', {'cloud_token': ''})
fsy = find('feedback_sync')
if fsy and (fsy.get('result') or {}).get('skipped'):
    ok('feedback_sync skips without token')
else:
    ng('feedback_sync no-token', fsy)

# Direct feedback_queue module roundtrip (no model needed)
qcode = (
    "import feedback_queue as q;"
    "q.clear();"
    "assert q.enqueue({'model_version':'mv1','feature':[0.1,0.2,0.3],'label':1,'source':'manual_keep'});"
    "assert q.enqueue({'feature':[0.4],'label':0});"
    "assert not q.enqueue({'feature':[],'label':1});"
    "assert not q.enqueue({'feature':[1],'label':5});"
    "p=q.take_pending(10); assert len(p)==2, p;"
    "assert p[0]['featureDim']==3 and p[0]['label']==1, p[0];"
    "q.mark_uploaded([r['id'] for r in p]);"
    "assert q.count_pending()==0 and q.count_uploaded()==2;"
    "print('QUEUE_OK')"
)
qp = subprocess.run(
    [sys.executable, '-c', qcode],
    cwd=os.path.dirname(os.path.abspath(__file__)) or '.',
    env=env, capture_output=True, text=True,
)
if 'QUEUE_OK' in (qp.stdout or ''):
    ok('feedback_queue roundtrip')
else:
    ng('feedback_queue roundtrip', (qp.stdout or '') + (qp.stderr or ''))

# ── 11. Clear user data ──
print("\n── Cleanup ──")
send('clear_user_data')
cud = find('clear_user_data')
if cud and cud.get('result', {}).get('ok'):
    ok('clear_user_data')
else:
    ng('clear_user_data', cud)

send('get_crash_logs')
cl = find('get_crash_logs')
if cl:
    ok('get_crash_logs')
else:
    ng('get_crash_logs', cl)

# ── Shutdown ──
send('shutdown')
try:
    p.wait(timeout=5)
except Exception:
    p.kill()

shutil.rmtree(td, ignore_errors=True)

print(f"\n{'='*40}")
print(f"Results: {PASS} passed, {FAIL} failed  ({PASS + FAIL} total)")
if FAIL:
    print("❌ SOME TESTS FAILED")
    sys.exit(1)
else:
    print("✅ ALL TESTS PASSED")
    sys.exit(0)
