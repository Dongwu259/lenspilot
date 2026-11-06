"""
verify_flywheel_0.8.1.py — end-to-end check of the UNTESTED seam:
real ONNX model → deep-feature extracted at keep-time → local queue → uploader
HTTP contract. Node server ingest+store is already covered by the studio-server
HTTP e2e; this covers the client half with a real model.

Run from electron_app/python_backend/ :  python verify_flywheel_0.8.1.py
Self-contained: builds a temp model dir (+minimal yaml), a test JPEG, a mock
feedback receiver, drives service.py via IPC, asserts a record with a real
feature vector was uploaded.
"""
import os, sys, json, time, shutil, tempfile, threading, subprocess
from http.server import BaseHTTPRequestHandler, HTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
PASS = FAIL = 0
def ok(m):
    global PASS; PASS += 1; print(f"  PASS  {m}")
def ng(m, d=""):
    global FAIL; FAIL += 1; print(f"  FAIL  {m}  {d}")

# ── mock feedback receiver ──
received = []
class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_POST(self):
        n = int(self.headers.get('Content-Length', 0))
        body = json.loads(self.rfile.read(n) or b'{}')
        recs = body.get('records', [])
        received.extend(recs)
        tok = self.headers.get('x-studio-token', '')
        resp = json.dumps({"ok": True, "accepted": len(recs), "token_seen": bool(tok)}).encode()
        self.send_response(200); self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(resp))); self.end_headers()
        self.wfile.write(resp)
srv = HTTPServer(('127.0.0.1', 0), H)
port = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()
print(f"[verify] mock receiver on 127.0.0.1:{port}")

# ── temp model dir (+ minimal yaml) + test image ──
td = tempfile.mkdtemp(prefix='fbverify_')
mdir = os.path.join(td, 'model'); os.makedirs(mdir)
for f in ('best_model.onnx', 'best_model.onnx.data'):
    shutil.copy(os.path.join(HERE, '..', 'model', f), os.path.join(mdir, f))
with open(os.path.join(mdir, 'resnet18.yaml'), 'w', encoding='utf-8') as f:
    f.write("model:\n  name: resnet18\n  img_size: 224\nlabel:\n  score_range: [1.0, 10.0]\n  normalize: true\n")
from PIL import Image
img_path = os.path.join(td, 'test.jpg')
Image.new('RGB', (640, 480), (120, 90, 60)).save(img_path)

env = dict(os.environ, PYTHONUNBUFFERED='1', PYTHONIOENCODING='utf-8', APS_USER_DATA=td)
p = subprocess.Popen([sys.executable, 'service.py'], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                     stderr=subprocess.STDOUT, text=True, encoding='utf-8', env=env, cwd=HERE)
lines, lock = [], threading.Lock()
def rd():
    for ln in p.stdout:
        with lock: lines.append(ln.rstrip('\n'))
threading.Thread(target=rd, daemon=True).start()
def send(cmd, params=None, rid=None):
    p.stdin.write(json.dumps({'id': rid or cmd, 'command': cmd, 'params': params or {}}, ensure_ascii=False) + '\n'); p.stdin.flush()
def find(rid, timeout=120):
    end = time.time() + timeout
    while time.time() < end:
        with lock:
            for ln in lines:
                try: o = json.loads(ln)
                except Exception: continue
                if o.get('id') == rid: return o
        time.sleep(0.15)
    return None

try:
    time.sleep(3)
    # 1. enable opt-in
    send('save_config', {'config': {'feedback_share_enabled': True}});
    r = find('save_config'); ok('opt-in enabled') if r and r.get('result', {}).get('ok') else ng('opt-in', r)
    # 2. load model
    send('load_model', {'model_dir': mdir, 'backend': 'onnx'}, rid='lm')
    r = find('lm', timeout=180)
    ok('model loaded') if r and not r.get('error') else ng('load_model', r)
    # 2b. score the photo first (realistic flow) so a cached model score exists
    send('score_files', {'files': [img_path]}, rid='sf')
    find('sf', timeout=120)
    # 3. keep a real photo -> triggers real feature extraction + enqueue
    send('set_manual_keep', {'path': img_path, 'keep': 1}, rid='mk')
    r = find('mk'); ok('set_manual_keep') if r and r.get('result', {}).get('ok') else ng('set_manual_keep', r)
    # 4. status shows a pending record
    send('feedback_get_status', rid='st'); r = find('st')
    pend = (r.get('result') or {}).get('pending', 0) if r else 0
    ok(f'queued 1 record (pending={pend})') if pend >= 1 else ng('nothing queued', r)
    # 5. sync to mock receiver
    send('feedback_sync', {'cloud_token': 'verify-token', 'server_url': f'http://127.0.0.1:{port}'}, rid='sy')
    r = find('sy'); res = (r or {}).get('result') or {}
    ok(f'sync uploaded={res.get("uploaded")}') if res.get('uploaded', 0) >= 1 else ng('sync', r)
    # 6. mock actually received a record with a real feature vector + label
    time.sleep(0.5)
    if received:
        rec = received[0]
        feat = rec.get('feature') or []
        ok(f'receiver got record: dim={rec.get("featureDim")} label={rec.get("label")} source={rec.get("source")}')
        ok('feature is 512-d layer4 context vector') if rec.get('featureDim') == 512 and len(feat) == 512 and any(abs(x) > 1e-6 for x in feat) else ng('feature not 512d', f'dim={rec.get("featureDim")} len={len(feat)}')
        ok('label==1 (keep)') if rec.get('label') == 1 else ng('label', rec.get('label'))
        ok(f'scalar score present ({rec.get("score")})') if rec.get('score') is not None else ng('score missing')
        ok(f'scalar blur present ({rec.get("blur")})') if rec.get('blur') is not None else ng('blur missing')
        ok('eyeOpen field carried (None until EAR detector)') if 'eyeOpen' in rec else ng('eyeOpen field missing')
    else:
        ng('receiver got NOTHING')
finally:
    send('shutdown')
    try: p.wait(timeout=5)
    except Exception: p.kill()
    srv.shutdown(); shutil.rmtree(td, ignore_errors=True)

print(f"\n{'='*44}\nResult: {PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
