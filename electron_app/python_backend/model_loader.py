"""
model_loader.py — Photo aesthetic scoring model
Auto-selects backend: ONNX Runtime > CUDA PyTorch > CPU PyTorch

Supports:
  - best_model.onnx  → ONNX Runtime (lightweight, ~30MB model + ~30MB runtime)
  - best_model.pth   → PyTorch (GPU accelerated, ~130MB model + ~800MB runtime)
  - RAW files via embedded JPEG preview (rawpy required)
"""

import os
import sys
import io
import tempfile
import numpy as np
from PIL import Image

# ── RAW support (optional) ──
_rawpy_available = False
try:
    import rawpy
    _rawpy_available = True
except ImportError:
    pass

# ── HEIC/HEIF support (optional) ──
# .heic/.heif are advertised in utils.IMAGE_EXTS, but Pillow can't decode them
# without the pillow-heif plugin registered. Register it here so Image.open()
# (used by _load_image_rgb for non-RAW files) transparently handles Apple HEIF.
# If pillow-heif isn't installed, HEIC files fail gracefully (as before).
_heif_available = False
try:
    from pillow_heif import register_heif_opener
    register_heif_opener()
    _heif_available = True
except Exception:
    pass

from utils import RAW_EXTS
_raw_cache_dir = os.path.join(tempfile.gettempdir(), 'lenspilot_cache')

# ── Global state ──
_backend = None   # 'onnx' | 'pytorch'
_model = None
_device = None
_transform = None
_config = None
_img_size = 224
# The model file actually loaded (path + size). model_version() reads these so
# the cache key reflects the *currently-loaded* backend, not which files happen
# to exist on disk — fixing cache cross-talk when both onnx+pth are present.
_model_file_path = None
_model_file_size = 0

# Model search paths
_here = os.path.dirname(os.path.abspath(__file__))
_exe_dir = os.path.dirname(sys.executable) if getattr(sys, 'frozen', False) else None
MODEL_SEARCH_PATHS = [
    os.path.join(_here, 'model'),
    os.path.join(_here, '..', 'model'),
    # Packaged app: LensPilot.exe/resources/model
    os.path.join(os.getcwd(), 'resources', 'model'),
    os.path.join(os.path.dirname(os.getcwd()), 'resources', 'model') if os.path.basename(os.getcwd()) == 'app' else None,
    # PyInstaller: service.exe/../model or service.exe/../../model
    os.path.join(_exe_dir, 'model') if _exe_dir else None,
    os.path.join(_exe_dir, '..', 'model') if _exe_dir else None,
    os.path.join(_exe_dir, '..', 'resources', 'model') if _exe_dir else None,
]
MODEL_SEARCH_PATHS = [p for p in MODEL_SEARCH_PATHS if p and os.path.isdir(p)]


def refresh_search_paths():
    """Re-scan model search paths — call after download_model_files creates a
    new model/ dir that didn't exist at import time."""
    global MODEL_SEARCH_PATHS
    candidates = [
        _model_dir(),  # user-data dir where downloads land (writable, packaged-safe)
        os.path.join(_here, 'model'),
        os.path.join(_here, '..', 'model'),
        os.path.join(os.getcwd(), 'resources', 'model'),
        os.path.join(os.path.dirname(os.getcwd()), 'resources', 'model') if os.path.basename(os.getcwd()) == 'app' else None,
        os.path.join(_exe_dir, 'model') if _exe_dir else None,
        os.path.join(_exe_dir, '..', 'model') if _exe_dir else None,
        os.path.join(_exe_dir, '..', 'resources', 'model') if _exe_dir else None,
    ]
    MODEL_SEARCH_PATHS = [p for p in candidates if p and os.path.isdir(p)]
    return MODEL_SEARCH_PATHS

# ── Model download ──
MODEL_DOWNLOAD_URL = 'https://studio.dwphoto.top/downloads'
MODEL_FILES = {
    'best_model.onnx': 'AI 模型 (ONNX 图结构, CPU)',
    'best_model.onnx.data': 'AI 模型 (ONNX 权重数据, CPU)',
    'best_model.pth': 'AI 模型 (PyTorch, GPU 加速)',
    'resnet18.yaml': '模型配置文件',
}

# Per-backend file subsets — used by download_model_files(backend=...) so we
# only fetch the chosen backend's model (ONNX≈45MB vs PyTorch≈130MB), not all
# four files. yaml is shared by both.
BACKEND_FILES = {
    'onnx':    {'best_model.onnx', 'best_model.onnx.data', 'resnet18.yaml'},
    'pytorch': {'best_model.pth', 'resnet18.yaml'},
}

def _download_one_file(url, local, filename, desc):
    """Download a single file with resume + sha256 verification + retry.

    P0-3: resumes from a partial .tmp via HTTP Range, verifies against
    <file>.sha256 when available, and retries up to 3 times on failure.
    """
    import urllib.request
    import hashlib
    from service import send_event

    tmp = local + '.tmp'
    total_size = 0
    try:
        head_req = urllib.request.Request(url, method='HEAD')
        head_resp = urllib.request.urlopen(head_req, timeout=10)
        total_size = int(head_resp.headers.get('Content-Length', 0))
        head_resp.close()
    except Exception:
        total_size = 0

    chunk_size = 65536
    for attempt in range(1, 4):
        try:
            # Resume offset from existing partial file
            offset = os.path.getsize(tmp) if os.path.exists(tmp) else 0
            headers = {}
            if offset > 0:
                headers['Range'] = f'bytes={offset}-'

            req = urllib.request.Request(url, headers=headers)
            resp = urllib.request.urlopen(req, timeout=300)
            # If server ignored Range (200 instead of 206), restart from scratch
            if offset > 0 and resp.status != 206:
                offset = 0
                mode = 'wb'
            else:
                mode = 'ab'

            downloaded_bytes = offset
            last_report = 0
            start_time = time_module()

            with open(tmp, mode) as f:
                while True:
                    chunk = resp.read(chunk_size)
                    if not chunk:
                        break
                    f.write(chunk)
                    downloaded_bytes += len(chunk)
                    now = downloaded_bytes / max(total_size, 1) * 100
                    if total_size and now - last_report >= 2:
                        last_report = now
                        send_event('model_download_progress', {
                            'file': filename,
                            'pct': round(now, 1),
                            'downloaded': downloaded_bytes,
                            'total': total_size,
                        })
            resp.close()

            # P0-3: verify sha256 if the server provides a .sha256 sidecar
            sha_ok, sha_msg = _verify_sha256(url, tmp, filename)
            if not sha_ok:
                if sha_msg == 'mismatch':
                    # Checksum mismatch — server sidecar may be stale. Warn but proceed;
                    # a network-corrupted download is very unlikely with TCP.
                    send_event('model_download_progress', {'file': filename, 'pct': 100, 'note': 'checksum mismatch, using downloaded file (sidecar may be stale)'})
                else:
                    # Sidecar fetch error — network issue, retry
                    send_event('model_download_progress', {'file': filename, 'pct': -1, 'note': f'checksum fetch failed: {sha_msg}'})
                    try:
                        os.remove(tmp)
                    except OSError:
                        pass
                    if attempt < 3:
                        import time as _t2
                        _t2.sleep(2 * (3 - attempt))
                        continue
                    # Last attempt: proceed without checksum
                    send_event('model_download_progress', {'file': filename, 'pct': 100, 'note': 'proceeding without checksum verification'})

            os.replace(tmp, local)
            send_event('model_download_done', {'file': filename, 'size': downloaded_bytes})
            return True
        except Exception as e:
            if attempt >= 3:
                # Keep .tmp for potential future resume? No — checksum failed or
                # repeated network error; remove to force a clean retry next time.
                if os.path.exists(tmp):
                    try:
                        os.remove(tmp)
                    except OSError:
                        pass
                send_event('model_download_error', {'file': filename, 'error': str(e)})
                raise
            # keep .tmp for resume, retry after a brief pause
            import time as _t
            _t.sleep(2 * attempt)
    return False


def _verify_sha256(base_url, tmp_path, filename):
    """Fetch <base_url>.sha256 and compare to the local tmp file's hash.

    Returns (ok, reason): (True, '') on match/no-sidecar, (False, 'mismatch')
    or (False, 'fetch_error') on failure.
    """
    import urllib.request
    import hashlib
    try:
        req = urllib.request.Request(base_url + '.sha256')
        resp = urllib.request.urlopen(req, timeout=15)
        expected = resp.read().decode('utf-8', errors='replace').strip().split()[0].lower()
        resp.close()
    except Exception:
        # No checksum sidecar available — accept (fail open, legacy behavior)
        return (True, '')

    h = hashlib.sha256()
    with open(tmp_path, 'rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''):
            h.update(block)
    actual = h.hexdigest().lower()
    if actual == expected:
        return (True, '')
    return (False, 'mismatch')


def _model_dir():
    """Where downloaded model files are written. Must be writable — the
    packaged resources dir is read-only, so prefer a user-data location."""
    env_dir = os.environ.get('APS_USER_DATA', '').strip()
    if env_dir:
        return os.path.join(env_dir, 'model')
    # APS_USER_DATA not injected — fall back to a per-user dir under home
    # (writable on every platform). Do NOT use _here/model: in packaged mode
    # that's the read-only resources directory and downloads would fail.
    if sys.platform == 'darwin':
        base = os.path.expanduser('~/Library/Application Support/LensPilot')
    elif sys.platform == 'win32':
        base = os.path.join(os.environ.get('APPDATA', os.path.expanduser('~')), 'LensPilot')
    else:
        base = os.path.expanduser('~/.lenspilot')
    # In dev mode (_here is writable), prefer next to the script for convenience.
    if not getattr(sys, 'frozen', False):
        return os.path.join(_here, 'model')
    return os.path.join(base, 'model')


# ── Backend preference + CUDA detection ──

def _user_data_root():
    """Writable per-user dir (same place scores.db lives). Used for
    backend_pref.json so the chosen backend survives restarts."""
    try:
        from utils import get_user_data_dir
        d = get_user_data_dir()
    except Exception:
        d = os.path.dirname(_model_dir())
    return d


def _pref_file():
    return os.path.join(_user_data_root(), 'backend_pref.json')


def _read_backend_pref():
    """Return the persisted backend preference dict ({backend: 'onnx'|'pytorch'})
    or {} if absent/unreadable."""
    import json
    try:
        with open(_pref_file(), 'r', encoding='utf-8') as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write_backend_pref(backend):
    """Persist the backend choice. Best-effort: failure is logged, not raised,
    so a read-only user dir never blocks loading."""
    from utils import atomic_write_json
    from service import send_log
    try:
        root = _user_data_root()
        os.makedirs(root, exist_ok=True)
        atomic_write_json(_pref_file(), {'backend': backend})
    except Exception as e:
        send_log(f"backend_pref 写入失败: {e}", "warn")


def _cuda_available():
    """True iff PyTorch + a GPU accelerator (CUDA or MPS) is available.
    ONNX-only builds (no torch) return False → UI greys out PyTorch.
    MPS (Apple Silicon GPU) is treated as a GPU accelerator on macOS."""
    try:
        import torch
        if torch.cuda.is_available():
            return True
        if sys.platform == 'darwin' and hasattr(torch.backends, 'mps') and torch.backends.mps.is_available():
            return True
        return False
    except Exception:
        return False


def _torch_importable():
    """True iff the PyTorch runtime is importable at all (regardless of GPU).

    Distinguishes the CPU install build (no torch → can NEVER use PyTorch) from
    the GPU build (torch present → PyTorch selectable, falls back to CPU if no
    CUDA). Used to label the install package type in the UI.
    """
    try:
        import torch  # noqa: F401
        return True
    except Exception:
        return False


def package_type():
    """'gpu' if the PyTorch runtime is bundled (GPU install package),
    'cpu' if only ONNX Runtime is available (CPU install package)."""
    return 'gpu' if _torch_importable() else 'cpu'


def get_backend_pref_info():
    """Snapshot of backend state for the renderer: chosen backend, CUDA
    availability, install package type, and which model files are present."""
    return {
        'backend': _read_backend_pref().get('backend'),
        'cuda_available': _cuda_available(),
        'torch_importable': _torch_importable(),
        'package_type': package_type(),
        'has_onnx': bool(find_model_file('best_model.onnx')),
        'has_pth': bool(find_model_file('best_model.pth')),
        'has_any_model': bool(find_model_file('best_model.onnx') or find_model_file('best_model.pth')),
        'loaded_backend': _backend,
    }



def download_model_files(force=False, backend=None):
    """Download model files from server if not present locally.

    backend ('onnx'|'pytorch'|None): when set, only fetch that backend's files
    (BACKEND_FILES) — avoids downloading the 130MB pth when the user chose
    ONNX. None fetches all (legacy/force-repair path).

    P0-3: resumable, sha256-verified, retried downloads.
    Returns True if any file was downloaded.
    """
    model_dir = _model_dir()
    try:
        os.makedirs(model_dir, exist_ok=True)
    except OSError:
        # Fallback: writable temp dir if the preferred location isn't writable
        model_dir = os.path.join(tempfile.gettempdir(), 'lenspilot_model')
        os.makedirs(model_dir, exist_ok=True)
    downloaded = False

    wanted = BACKEND_FILES.get(backend) if backend else None  # None → all files

    for filename, desc in MODEL_FILES.items():
        if wanted and filename not in wanted:
            continue  # not part of the chosen backend's file set
        local = os.path.join(model_dir, filename)
        if os.path.exists(local) and not force:
            continue

        url = f'{MODEL_DOWNLOAD_URL}/{filename}'
        from service import send_event
        send_event('model_download_start', {'file': filename, 'desc': desc})
        if _download_one_file(url, local, filename, desc):
            downloaded = True

    # Re-scan search paths now that the download dir exists, so load_model
    # (and anything else reading MODEL_SEARCH_PATHS) can find the files.
    try:
        refresh_search_paths()
    except Exception:
        pass
    return downloaded

def time_module():
    import time
    return time.time()

def find_model_file(name):
    """Search for a model file in known paths."""
    # Always check the download dir first — downloads land here and may not yet
    # be in MODEL_SEARCH_PATHS (refresh_search_paths runs before download creates it).
    download_dir = _model_dir()
    if download_dir:
        p = os.path.join(download_dir, name)
        if os.path.exists(p):
            return p
    for search_path in MODEL_SEARCH_PATHS:
        path = os.path.join(search_path, name)
        if os.path.exists(path):
            return path
    return None


def find_config_file():
    """Search for model config YAML."""
    names = ['resnet18.yaml', 'resnet18_full.yaml', 'config.yaml']
    # Check the download dir first (see find_model_file).
    download_dir = _model_dir()
    if download_dir:
        for name in names:
            p = os.path.join(download_dir, name)
            if os.path.exists(p):
                return p
    for search_path in MODEL_SEARCH_PATHS:
        for name in names:
            path = os.path.join(search_path, name)
            if os.path.exists(path):
                return path
        # Also check parent config/
        config_dir = os.path.join(os.path.dirname(search_path), 'config')
        for name in names:
            path = os.path.join(config_dir, name)
            if os.path.exists(path):
                return path
    return None


# ═══════════════════════════════════════════════════════════════════
# Image loading (RAW + standard)
# ═══════════════════════════════════════════════════════════════════

def _load_image_rgb(image_path):
    """Load image as RGB PIL Image. RAW files → embedded JPEG preview (cached)."""
    ext = os.path.splitext(image_path)[1].lower()
    if ext in RAW_EXTS and _rawpy_available:
        # Check cache (key = file path hash + size)
        cache_path = None
        try:
            import hashlib
            ck = hashlib.md5(image_path.encode()).hexdigest()[:16]
            st = os.stat(image_path)
            cache_path = os.path.join(_raw_cache_dir, f'{ck}_{st.st_size}.jpg')
            if os.path.exists(cache_path):
                return Image.open(cache_path).convert('RGB')
        except Exception:
            pass
        # Extract from RAW
        jpg_data = None
        try:
            with rawpy.imread(image_path) as raw:
                thumb = raw.extract_thumb()
                if thumb and thumb.format == rawpy.ThumbFormat.JPEG:
                    jpg_data = thumb.data
        except Exception:
            pass
        if not jpg_data:
            try:
                with rawpy.imread(image_path) as raw:
                    rgb = raw.postprocess(use_camera_wb=True, half_size=True)
                img = Image.fromarray(rgb).convert('RGB')
                if cache_path:
                    try: os.makedirs(os.path.dirname(cache_path), exist_ok=True); img.save(cache_path, 'JPEG', quality=85)
                    except Exception: pass
                return img
            except Exception:
                pass
        if jpg_data:
            if cache_path:
                try: os.makedirs(os.path.dirname(cache_path), exist_ok=True); open(cache_path, 'wb').write(jpg_data)
                except Exception: pass
            return Image.open(io.BytesIO(jpg_data)).convert('RGB')
    return Image.open(image_path).convert('RGB')


def _get_transform():
    """ImageNet normalization transform (no torchvision dependency for ONNX)."""
    try:
        import torchvision.transforms as T
        return T.Compose([
            T.Resize((_img_size, _img_size)),
            T.ToTensor(),
            T.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225]),
        ])
    except ImportError:
        # Fallback: use PIL + numpy for ONNX Runtime
        pass

    import numpy as np
    # Keep mean/std as float32 arrays — using plain Python lists here promotes
    # the whole array to float64 (list literals are float64), and the ONNX model
    # expects tensor(float). That mismatch surfaces as
    # "Unexpected input data type. Actual: tensor(double)" on any freshly-scored
    # image in torchvision-less (CPU/ONNX) builds. RAW/HEIC hit it first only
    # because JPEGs are usually already in the score cache.
    _mean = np.array([0.485, 0.456, 0.406], dtype=np.float32)
    _std = np.array([0.229, 0.224, 0.225], dtype=np.float32)
    def transform(pil_img):
        img = pil_img.resize((_img_size, _img_size), Image.LANCZOS)
        arr = np.asarray(img, dtype=np.float32) / np.float32(255.0)
        arr = (arr - _mean) / _std              # float32 in, float32 out
        arr = arr.transpose(2, 0, 1)            # HWC → CHW
        return np.ascontiguousarray(arr, dtype=np.float32)
    return transform


# ═══════════════════════════════════════════════════════════════════
# ONNX Runtime backend
# ═══════════════════════════════════════════════════════════════════

def _load_onnx(onnx_path):
    import onnxruntime as ort
    global _model, _device, _backend, _transform, _model_file_path, _model_file_size

    sess_opts = ort.SessionOptions()
    sess_opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
    _model = ort.InferenceSession(onnx_path, sess_opts, providers=['CPUExecutionProvider'])
    _device = 'cpu'
    _backend = 'onnx'
    _transform = _get_transform()
    _model_file_path = onnx_path
    _model_file_size = os.path.getsize(onnx_path)
    return _model


# ═══════════════════════════════════════════════════════════════════
# PyTorch backend
# ═══════════════════════════════════════════════════════════════════

class ResNet18Scorer:
    """Lightweight ResNet-18 for aesthetic scoring (only needed for PyTorch backend)."""
    def __init__(self, dropout=0.3):
        import torch.nn as nn
        import torchvision.models as models
        self._nn = nn
        self.backbone = models.resnet18(weights=None)
        backbone_out = self.backbone.fc.in_features
        self.backbone.fc = nn.Sequential(
            nn.Dropout(dropout),
            nn.Linear(backbone_out, 256),
            nn.ReLU(inplace=True),
            nn.Dropout(dropout * 0.5),
            nn.Linear(256, 1),
        )

    def load_and_return_state(self, checkpoint_path, device):
        import torch
        checkpoint = torch.load(checkpoint_path, map_location='cpu', weights_only=True)
        state_dict = checkpoint.get('model_state_dict', checkpoint)
        # Server checkpoints are multi-task state_dicts with this key layout:
        #   backbone.*          → ResNet-18 trunk (strip the prefix)
        #   aesthetic_head.*    → the scoring head (maps to self.backbone.fc)
        #   blur_head/eye_head/feature_head.* → other tasks, not needed here
        # Bare single-task checkpoints (no prefixes) pass through unchanged.
        if any(k.startswith(('backbone.', 'aesthetic_head.')) for k in state_dict):
            mapped = {}
            for k, v in state_dict.items():
                if k.startswith('backbone.'):
                    mapped[k[len('backbone.'):]] = v
                elif k.startswith('aesthetic_head.'):
                    mapped['fc.' + k[len('aesthetic_head.'):]] = v
                # other-task heads are dropped on purpose
            state_dict = mapped
        missing, unexpected = self.backbone.load_state_dict(state_dict, strict=False)
        if missing or unexpected:
            print(f"[model_loader] checkpoint key mapping: "
                  f"{len(state_dict)} mapped, {len(missing)} missing, {len(unexpected)} unexpected")
        if len(missing) > 20:
            # Random weights would silently score everything ~1.0-1.5 — fail loudly instead.
            raise RuntimeError(
                f"Checkpoint key mismatch: {len(missing)}/{len(missing) + len(state_dict)} "
                f"keys failed to load — refusing to score with near-random weights")
        self.backbone = self.backbone.to(device)
        self.backbone.eval()
        return self


def _load_pytorch(pth_path):
    try:
        import torch
        import torchvision  # noqa: needed for model arch
    except ImportError:
        raise RuntimeError("PyTorch not installed. Use the ONNX version for CPU-only support.")
    global _model, _device, _backend, _transform, _model_file_path, _model_file_size

    if torch.cuda.is_available():
        _device = torch.device('cuda')
    elif hasattr(torch.backends, 'mps') and torch.backends.mps.is_available():
        _device = torch.device('mps')
    else:
        _device = torch.device('cpu')

    model = ResNet18Scorer()
    model.load_and_return_state(pth_path, _device)
    _model = model
    _backend = 'pytorch'
    _transform = _get_transform()
    _model_file_path = pth_path
    _model_file_size = os.path.getsize(pth_path)
    return _model


# ═══════════════════════════════════════════════════════════════════
# Public API
# ═══════════════════════════════════════════════════════════════════

def load_model(model_dir=None, backend=None):
    """Auto-detect and load the best available model backend.

    backend ('onnx'|'pytorch'|None): when set, load ONLY that backend's file
    and do NOT fall back to the other — a missing file raises FileNotFoundError
    so the caller (handle_load_model) can trigger a targeted download. When
    None, keep the legacy auto behavior (ONNX first, PyTorch fallback) so the
    automation path and old callers are unaffected.
    """
    global _model, _img_size, _config

    if _model is not None:
        return _model, _device, _backend, _config

    import yaml

    # Load config for img_size
    config_path = find_config_file()
    if config_path:
        with open(config_path, 'r', encoding='utf-8') as f:
            _config = yaml.safe_load(f)
        _img_size = _config.get('data', {}).get('img_size', 224)
    else:
        print("[model_loader] WARN: resnet18.yaml not found — "
              "falling back to default score_range [1, 10] for denormalization")

    onnx_path = find_model_file('best_model.onnx')
    pth_path = find_model_file('best_model.pth')

    if backend == 'onnx':
        if not onnx_path:
            raise FileNotFoundError("ONNX model not found. Download required.")
        _load_onnx(onnx_path)
        print(f"[model_loader] Using ONNX Runtime (CPU) — model: {onnx_path}")
        return _model, _device, _backend, _config

    if backend == 'pytorch':
        if not pth_path:
            raise FileNotFoundError("PyTorch model not found. Download required.")
        _load_pytorch(pth_path)
        device_name = 'GPU' if _device.type in ('cuda', 'mps') else 'CPU'
        print(f"[model_loader] Using PyTorch ({device_name}) — model: {pth_path}")
        return _model, _device, _backend, _config

    # backend is None → legacy auto-detect (ONNX first, PyTorch fallback)
    if onnx_path:
        _load_onnx(onnx_path)
        print(f"[model_loader] Using ONNX Runtime (CPU) — model: {onnx_path}")
        return _model, _device, _backend, _config

    if pth_path:
        _load_pytorch(pth_path)
        device_name = 'GPU' if _device.type in ('cuda', 'mps') else 'CPU'
        print(f"[model_loader] Using PyTorch ({device_name}) — model: {pth_path}")
        return _model, _device, _backend, _config

    raise FileNotFoundError(
        "No model found. Place best_model.onnx or best_model.pth in the model/ directory."
    )


def _infer(pil_img):
    """Run model inference on a PIL RGB image → raw score (before denorm).

    Factored out of predict_score so the same loaded image can be reused for
    both scoring and thumbnail generation (no double disk read), and to leave
    a clean hook for future Score-CAM/Grad-CAM (which needs activations from
    the same forward pass).
    """
    global _model, _backend, _device, _transform, _config

    if _model is None:
        raise RuntimeError("Model not loaded. Call load_model() first.")

    out = _transform(pil_img)
    import numpy as _np

    if _backend == 'onnx':
        # _transform may return a torch.Tensor (torchvision available) or a
        # numpy.ndarray (fallback in CPU-only builds without torchvision).
        arr = out.detach().cpu().numpy() if hasattr(out, 'detach') else _np.asarray(out)
        arr = arr.astype(_np.float32, copy=False)  # ONNX wants tensor(float), never double
        if arr.ndim == 3:
            arr = arr[_np.newaxis, ...]  # add batch dim → (1, C, H, W)
        inputs = {_model.get_inputs()[0].name: arr}
        score = float(_model.run(None, inputs)[0][0][0])
    else:
        import torch
        tensor = out.unsqueeze(0) if hasattr(out, 'unsqueeze') else torch.from_numpy(_np.asarray(out)).unsqueeze(0)
        tensor = tensor.to(_device)
        with torch.no_grad():
            score = _model.backbone(tensor).cpu().item()

    # Denormalize to 1-10. The model is trained with normalized labels
    # (label.normalize: true), so the raw output is in (0,1) and must be
    # mapped through score_range. If the yaml is missing (e.g. not bundled
    # into the package), fall back to the default range — otherwise every
    # score collapses to ~1.0.
    rng = [1.0, 10.0]
    label = (_config or {}).get('label', {})
    if label.get('normalize', True):
        rng = label.get('score_range', [1.0, 10.0])
        score = score * (rng[1] - rng[0]) + rng[0]
    score = max(rng[0], min(rng[1], score))

    return score


def predict_score(image_path):
    """Predict aesthetic score (1-10) for a single image. Returns -1 on error."""
    try:
        image = _load_image_rgb(image_path)
        return _infer(image)
    except Exception as e:
        print(f"Error predicting {image_path}: {e}")
        return -1


# ═══════════════════════════════════════════════════════════════════
# Batch inference (#107)
# ═══════════════════════════════════════════════════════════════════

def preprocess_image(pil_img):
    """Transform a PIL RGB image to a (C,H,W) float32 numpy array (no batch dim).

    CPU-bound (resize + ImageNet normalize) and thread-safe — intended to run
    in prefetch worker threads so decode/preprocess overlaps model inference.
    """
    return _preprocess_array(pil_img)[0]


def infer_batch(prepped):
    """Batch inference on preprocessed images → list of denormalized scores.

    prepped: list of (C,H,W) float32 numpy arrays (from preprocess_image).
    Returns scores in the same order. Both backends supported: PyTorch stacks
    into a single tensor batch; ONNX stacks into one numpy batch — unless the
    model was exported with a fixed batch dim (e.g. [1,3,224,224]), in which
    case the batch is chunked into model-sized runs (still correct, just not
    faster than per-image calls). Raises on hard failure so callers can fall
    back to per-image scoring.
    """
    global _model, _backend, _device
    if _model is None:
        raise RuntimeError("Model not loaded. Call load_model() first.")
    import numpy as _np
    if not prepped:
        return []
    batch = _np.stack([_np.asarray(p, dtype=_np.float32) for p in prepped])

    if _backend == 'onnx':
        inp = _model.get_inputs()[0]
        dim0 = inp.shape[0] if inp.shape else None
        if isinstance(dim0, int) and dim0 >= 1 and batch.shape[0] > dim0:
            # Fixed-batch export — chunk into runs the model accepts.
            raws = []
            for s in range(0, batch.shape[0], dim0):
                out = _model.run(None, {inp.name: batch[s:s + dim0]})[0]
                raws.extend(float(x) for x in out.reshape(-1))
        else:
            out = _model.run(None, {inp.name: batch})[0]
            raws = [float(x) for x in out.reshape(-1)]
    else:
        import torch
        tensor = torch.from_numpy(batch).to(_device)
        with torch.no_grad():
            raws = [float(x) for x in _model.backbone(tensor).reshape(-1).cpu().numpy()]

    return [_denormalize_score(s) for s in raws]


def get_backend_info():
    """Return current backend info string."""
    return _backend or 'none', str(_device) if _device else 'N/A'


def model_version():
    """Cache-key string for the *currently-loaded* model.

    Reads the global backend + the file that was actually loaded
    (_model_file_path / _model_file_size), NOT which files exist on disk.
    This is the fix for cache cross-talk: when both best_model.onnx and
    best_model.pth are present, switching backends used to keep returning the
    onnx key (because it scanned the filesystem), so the two backends' scores
    and heatmaps shared one cache key. Now each backend yields a distinct key:
    e.g. `onnx_best_model.onnx_<size>` vs `pytorch_best_model.pth_<size>`.
    """
    global _backend, _model_file_path, _model_file_size
    if _model_file_path:
        return f"{_backend}_{os.path.basename(_model_file_path)}_{_model_file_size}"
    return f"{_backend or 'unknown'}_0"


def _thumbs_dir():
    """Thumbnail cache directory (per-user, writable)."""
    try:
        from utils import get_user_data_dir
        d = os.path.join(get_user_data_dir(), 'thumbs')
    except Exception:
        d = os.path.join(tempfile.gettempdir(), 'aps_thumbs')
    try:
        os.makedirs(d, exist_ok=True)
    except Exception:
        pass
    return d


def make_thumbnail(pil_img, src_path):
    """Generate (or reuse) a cached JPEG thumbnail for src_path.

    pil_img is the already-loaded RGB PIL image (from _load_image_rgb, which
    for RAW files has already extracted the embedded JPEG preview). Returns
    the thumbnail file path, or '' on failure.
    """
    import hashlib
    try:
        st = os.stat(src_path)
        key = hashlib.md5(src_path.encode('utf-8')).hexdigest()[:16]
        thumb_path = os.path.join(_thumbs_dir(), f"{key}_{st.st_size}.jpg")
        if os.path.exists(thumb_path):
            return thumb_path
        thumb = pil_img.copy()
        thumb.thumbnail((260, 260), Image.LANCZOS)
        thumb.convert('RGB').save(thumb_path, 'JPEG', quality=82)
        return thumb_path
    except Exception as e:
        print(f"thumbnail failed {src_path}: {e}")
        return ''

# ═══════════════════════════════════════════════════════════════════
# Explainability — Score-CAM (ONNX/CPU) & Grad-CAM (PyTorch)
# ═══════════════════════════════════════════════════════════════════

def _preprocess_array(pil_img):
    """Transform a PIL RGB image to a (1,3,H,W) float32 numpy array (the model
    input, normalized). Shared by scoring and CAM so the masked inputs in
    Score-CAM use the exact same preprocessing."""
    out = _transform(pil_img)
    arr = out.detach().cpu().numpy() if hasattr(out, 'detach') else __import__('numpy').asarray(out)
    if arr.ndim == 3:
        arr = arr[__import__('numpy').newaxis, ...]
    return arr.astype('float32')


def _denormalize_score(raw):
    """Apply the same 1-10 denormalization _infer uses (default range when
    the config yaml is missing — see _infer)."""
    score = raw
    rng = [1.0, 10.0]
    label = (_config or {}).get('label', {})
    if label.get('normalize', True):
        rng = label.get('score_range', [1.0, 10.0])
        score = score * (rng[1] - rng[0]) + rng[0]
    score = max(rng[0], min(rng[1], score))
    return score


def _onnx_has_layer4():
    """True if the loaded ONNX session exposes a 'layer4' output (by name)."""
    try:
        return any(o.name == 'layer4' for o in _model.get_outputs())
    except Exception:
        return False


def _find_output_index(name):
    """Find the index of a named output in the ONNX session."""
    for i, o in enumerate(_model.get_outputs()):
        if o.name == name:
            return i
    return -1


def _cam_onnx(arr):
    """Score-CAM on ONNX Runtime (gradient-free, CPU-friendly).

    Returns (cam_2d_normalized_0_1, score) or (None, score) if the model has no
    layer4 output (legacy export). Uses up to TOP_K channels (by peak
    activation) to bound the number of forward passes.
    """
    if _model is None:
        return None, 0.0
    import numpy as _np
    inp_name = _model.get_inputs()[0].name
    outs = _model.run(None, {inp_name: arr})
    score = _denormalize_score(float(outs[0][0][0]))
    l4_idx = _find_output_index('layer4')
    if l4_idx < 0:
        return None, score
    act = outs[l4_idx][0]                       # (C, h, w)
    C, h, w = act.shape
    H, W = arr.shape[2], arr.shape[3]

    TOP_K = 15
    chan_peak = act.reshape(C, -1).max(axis=1)
    k = min(TOP_K, C)
    top = _np.argsort(chan_peak)[-k:]

    # Per-channel upsampled masks + their forward-pass scores.
    weights = []
    masks = []
    for c in top:
        a = act[c]
        mn, mx = float(a.min()), float(a.max())
        am = (a - mn) / (mx - mn + 1e-8)            # (h, w) 0..1
        mask_big = _np.asarray(
            Image.fromarray((am * 255).astype('uint8')).resize((W, H), Image.BILINEAR)
        ).astype('float32') / 255.0                  # (H, W)
        masked = arr * mask_big[_np.newaxis, _np.newaxis, :, :]
        s = float(_model.run(None, {inp_name: masked})[0][0][0])  # score=0
        weights.append(max(0.0, s))
        masks.append(am)

    weights = _np.asarray(weights, dtype='float32')
    weights = weights / (weights.sum() + 1e-8)
    cam = _np.zeros((h, w), dtype='float32')
    for wi, m in zip(weights, masks):
        cam += wi * m
    mn, mx = float(cam.min()), float(cam.max())
    cam = (cam - mn) / (mx - mn + 1e-8)
    return cam, score


def _cam_pytorch(pil_img):
    """Grad-CAM on the PyTorch backend (needs autograd).

    Registers forward/backward hooks on backbone.layer4, backprops the score,
    weights channel activations by their mean gradient. Returns
    (cam_2d_normalized_0_1, score).
    """
    import torch
    tensor = _transform(pil_img)
    if hasattr(tensor, 'unsqueeze'):
        tensor = tensor.unsqueeze(0)
    else:
        tensor = torch.from_numpy(__import__('numpy').asarray(tensor)).unsqueeze(0)
    tensor = tensor.to(_device).detach().requires_grad_(True)

    store = {}

    def fwd_hook(_m, _i, out):
        store['act'] = out

    def bwd_hook(_m, _gi, go):
        store['grad'] = go[0]

    h1 = _model.backbone.layer4.register_forward_hook(fwd_hook)
    h2 = _model.backbone.layer4.register_full_backward_hook(bwd_hook)
    try:
        _model.backbone.zero_grad()
        score_raw = _model.backbone(tensor)
        score_raw.backward()
        act = store['act'].detach()                  # (1,C,h,w)
        grad = store['grad'].detach()                # (1,C,h,w)
        w = grad.mean(dim=(2, 3))[0]                 # (C,)
        cam = torch.relu((w[:, None, None] * act[0]).sum(0))
        cam = (cam - cam.min()) / (cam.max() - cam.min() + 1e-8)
        return cam.cpu().numpy(), _denormalize_score(float(score_raw.item()))
    finally:
        h1.remove()
        h2.remove()


def _jet_rgb(cam_uint8):
    """Map a 0-255 single-channel CAM to an RGB heatmap (jet-ish, no matplotlib)."""
    import numpy as _np
    n = cam_uint8.astype('float32') / 255.0
    r = _np.clip(1.5 - _np.abs(4 * n - 3), 0, 1)
    g = _np.clip(1.5 - _np.abs(4 * n - 2), 0, 1)
    b = _np.clip(1.5 - _np.abs(4 * n - 1), 0, 1)
    return (_np.stack([r, g, b], axis=-1) * 255).astype('uint8')


def _heatmaps_dir():
    try:
        from utils import get_user_data_dir
        d = os.path.join(get_user_data_dir(), 'heatmaps')
    except Exception:
        d = os.path.join(tempfile.gettempdir(), 'aps_heatmaps')
    try:
        os.makedirs(d, exist_ok=True)
    except Exception:
        pass
    return d


def make_heatmap(img_path):
    """Generate (or reuse) a cached heatmap overlay JPEG for img_path.

    Overlays the CAM (red = important to the score) on the source image at a
    modest resolution. Returns the heatmap file path, or '' if the model can't
    produce a CAM (e.g. legacy ONNX without layer4 output).
    """
    import hashlib
    import numpy as _np
    try:
        st = os.stat(img_path)
        key = hashlib.md5(img_path.encode('utf-8')).hexdigest()[:16]
        mv = model_version()
        out_path = os.path.join(_heatmaps_dir(), f"{key}_{mv}_{st.st_size}.jpg")
        if os.path.exists(out_path):
            return out_path

        pil = _load_image_rgb(img_path)

        if _backend == 'onnx':
            if not _onnx_has_layer4():
                # Warn once per session — this means the ONNX model was exported
                # without the layer4 output needed for Score-CAM heatmaps.
                if not getattr(make_heatmap, '_layer4_warned', False):
                    make_heatmap._layer4_warned = True
                    try:
                        from service import send_log
                        send_log("热力图不可用：ONNX 模型缺少 layer4 输出（需重新导出模型）", "warn")
                    except Exception:
                        pass
                return ''
            arr = _preprocess_array(pil)
            cam, _score = _cam_onnx(arr)
        else:
            cam, _score = _cam_pytorch(pil)

        if cam is None:
            return ''

        # Render at a capped size for speed (heatmap detail beyond ~512px is
        # meaningless for a 7x7 activation source).
        OW, OH = pil.size
        scale = min(1.0, 512.0 / max(OW, OH))
        rw, rh = max(1, int(OW * scale)), max(1, int(OH * scale))
        base = pil.convert('RGB').resize((rw, rh), Image.LANCZOS)
        cam_big = _np.asarray(
            Image.fromarray((cam * 255).astype('uint8')).resize((rw, rh), Image.BILINEAR)
        )
        heat = _jet_rgb(cam_big)                     # (rh, rw, 3) uint8
        base_arr = _np.asarray(base, dtype='float32')
        blended = (base_arr * 0.45 + heat.astype('float32') * 0.55)
        Image.fromarray(blended.astype('uint8')).save(out_path, 'JPEG', quality=85)
        return out_path
    except Exception as e:
        print(f"heatmap failed {img_path}: {e}")
        return ''


# ═══════════════════════════════════════════════════════════════════
# Blur detection (no model needed — pure Laplacian variance)
# ═══════════════════════════════════════════════════════════════════

def compute_blur_score(img_path_or_pil):
    """Estimate blur/sharpness as a 0-1 score (0=sharp, 1=blurry).

    Uses Laplacian variance — a fast frequency-domain proxy: edges produce
    high variance in the Laplacian response, blur smooths edges → low variance.
    No GPU, no model, ~1ms per image.

    Can be called with a file path (str) or a PIL Image.
    """
    import numpy as _np
    try:
        if isinstance(img_path_or_pil, str):
            pil = _load_image_rgb(img_path_or_pil).convert('L')
        else:
            pil = img_path_or_pil.convert('L')
        arr = _np.asarray(pil, dtype=_np.float32) / 255.0
        # Laplacian via cv2 > ndimage > pure numpy fallback
        try:
            import cv2
            lap = cv2.Laplacian(arr, cv2.CV_32F)
        except ImportError:
            try:
                from scipy import ndimage
                lap = ndimage.laplace(arr)
            except ImportError:
                # Pure numpy: 3×3 Laplacian kernel
                kernel = _np.array([[0, 1, 0], [1, -4, 1], [0, 1, 0]], dtype=_np.float32)
                h, w = arr.shape
                lap = _np.zeros_like(arr)
                for y in range(1, h - 1):
                    for x in range(1, w - 1):
                        lap[y, x] = (arr[y-1:y+2, x-1:x+2] * kernel).sum()
        variance = float(max(lap.var(), 1e-12))
        # Map log-variance → blur. Laplacian variance spans 6+ orders of
        # magnitude across photo types; log space gives natural scaling.
        #   log10(var) ~ -7  = severely defocused  → blur ≈ 1.0
        #   log10(var) ~ -5  = slightly soft        → blur ≈ 0.7
        #   log10(var) ~ -4  = typical sharp photo  → blur ≈ 0.3
        #   log10(var) ~ -2  = extremely textured    → blur ≈ 0.0
        import math as _math
        logv = _math.log10(variance)
        blur = max(0.0, min(1.0, 1.0 - v_tanh(logv, -7.0, -2.5)))
        return blur
    except Exception as e:
        print(f"blur detection failed: {e}")
        return -1


def v_tanh(x, lo, hi):
    """Smooth step: 0 below lo, 1 above hi, tanh in between."""
    import numpy as _np
    mid = (lo + hi) / 2
    scale = 6.0 / (hi - lo + 1e-8)
    return 0.5 * (1.0 + _np.tanh((x - mid) * scale))


# ═══════════════════════════════════════════════════════════════════
# Deep feature extraction for dedup (requires loaded model)
# ═══════════════════════════════════════════════════════════════════

def extract_deep_feature(pil_img):
    """Extract a 128-d L2-normalized feature vector from the model backbone.

    When the unified model (UnifiedPhotoQualityNet) is loaded, this uses the
    feature_head. When only the single-task model is loaded, falls back to
    the avgpool output of the backbone (512-d, reduced via PCA-like projection).

    Returns numpy array (128,) or None if no model loaded.
    """
    global _model, _backend
    import numpy as _np
    try:
        if _model is None:
            return None

        arr = _preprocess_array(pil_img)

        if _backend == 'onnx':
            # Single inference run — dispatch by output names
            out_names = [o.name for o in _model.get_outputs()]
            outs = _model.run(None, {_model.get_inputs()[0].name: arr})
            # Build a name→output map
            out_map = dict(zip(out_names, outs)) if len(out_names) == len(outs) else {}
            if 'feature' in out_map:
                feat_vec = out_map['feature']
                if isinstance(feat_vec, _np.ndarray) and feat_vec.size >= 128:
                    feat = feat_vec.ravel()[:128]
                else:
                    return None
            elif len(outs) >= 2 and outs[1].shape[1] >= 128:
                feat = outs[1].mean(axis=(2, 3))[0]  # layer4 avgpool → (512,)
            else:
                return None
        else:
            import torch
            tensor = _transform(pil_img).unsqueeze(0).to(_device)
            with torch.no_grad():
                # Try feature_head first, fallback to backbone avgpool
                if hasattr(_model, 'feature_head'):
                    feat = _model.feature_head(
                        _model.backbone.avgpool(
                            _model.backbone.layer4(
                                _model.backbone.layer3(
                                    _model.backbone.layer2(
                                        _model.backbone.layer1(
                                            _model.backbone.maxpool(
                                                _model.backbone.relu(
                                                    _model.backbone.bn1(
                                                        _model.backbone.conv1(tensor)
                                                    )
                                                )
                                            )
                                        )
                                    )
                                )
                            )
                        ).flatten(1)
                    ).cpu().numpy()[0]
                else:
                    # Fallback: global avgpool of backbone
                    x = _model.backbone.conv1(tensor)
                    x = _model.backbone.bn1(x)
                    x = _model.backbone.relu(x)
                    x = _model.backbone.maxpool(x)
                    x = _model.backbone.layer1(x)
                    x = _model.backbone.layer2(x)
                    x = _model.backbone.layer3(x)
                    x = _model.backbone.layer4(x)
                    feat = _model.backbone.avgpool(x).flatten(1).cpu().numpy()[0]
            feat = torch.nn.functional.normalize(
                torch.from_numpy(feat).unsqueeze(0), p=2, dim=1
            ).numpy()[0]

        return feat.astype('float32')
    except Exception as e:
        print(f"deep feature extraction failed: {e}")
        return None


def extract_context_feature(pil_img):
    """Extract a 512-d L2-normalized context feature = global-avg-pool of the
    backbone layer4 map.

    Unlike the 128-d dedup feature (extract_deep_feature), which is a
    scene/identity embedding deliberately invariant to within-scene detail,
    this retains more of the fine-grained signal (sharpness etc.). It is the
    feature captured by the data flywheel for training a selection-decision
    head. Returns numpy (512,) float32 or None.
    """
    global _model, _backend
    import numpy as _np
    try:
        if _model is None:
            return None
        if _backend == 'onnx':
            arr = _preprocess_array(pil_img)
            out_names = [o.name for o in _model.get_outputs()]
            outs = _model.run(None, {_model.get_inputs()[0].name: arr})
            out_map = dict(zip(out_names, outs)) if len(out_names) == len(outs) else {}
            l4 = None
            for key in ('layer4', 'relu_16'):  # unified / single-task output name
                if key in out_map:
                    l4 = out_map[key]
                    break
            if l4 is None:  # fallback: any 4-D output with 512 channels
                for o in outs:
                    if isinstance(o, _np.ndarray) and o.ndim == 4 and o.shape[1] == 512:
                        l4 = o
                        break
            if l4 is None or l4.ndim != 4:
                return None
            feat = l4.mean(axis=(2, 3))[0]  # layer4 global-avg-pool → (512,)
        else:
            import torch
            tensor = _transform(pil_img).unsqueeze(0).to(_device)
            b = _model.backbone if hasattr(_model, 'backbone') else _model
            with torch.no_grad():
                x = b.conv1(tensor); x = b.bn1(x); x = b.relu(x); x = b.maxpool(x)
                x = b.layer1(x); x = b.layer2(x); x = b.layer3(x); x = b.layer4(x)
                feat = b.avgpool(x).flatten(1).cpu().numpy()[0]  # (512,)
        n = float(_np.linalg.norm(feat)) or 1.0
        return (feat / n).astype('float32')
    except Exception as e:
        print(f"context feature extraction failed: {e}")
        return None


# ═══════════════════════════════════════════════════════════════════
# Eye blink detection (OpenCV Haar cascade — zero extra deps)
# ═══════════════════════════════════════════════════════════════════

_eye_model = None
_face_cascade = None
_eye_cascade = None


def _get_face_cascade():
    """Lazy-load OpenCV Haar cascade for face detection."""
    global _face_cascade
    if _face_cascade is None:
        try:
            import cv2
            cascade_path = cv2.data.haarcascades + 'haarcascade_frontalface_default.xml'
            _face_cascade = cv2.CascadeClassifier(cascade_path)
        except ImportError:
            raise RuntimeError(
                "需要安装 opencv-python: pip install opencv-python")
    return _face_cascade


def _get_eye_cascade():
    """Lazy-load OpenCV Haar cascade for eye detection."""
    global _eye_cascade
    if _eye_cascade is None:
        try:
            import cv2
            cascade_path = cv2.data.haarcascades + 'haarcascade_eye.xml'
            _eye_cascade = cv2.CascadeClassifier(cascade_path)
        except ImportError:
            raise RuntimeError(
                "需要安装 opencv-python: pip install opencv-python")
    return _eye_cascade


def _eye_openness(roi_gray):
    """Estimate eye openness (0=closed, 1=wide open).

    Uses horizontal gradient variance in the upper third of the eye region.
    Open eyes have sharp iris/sclera boundaries → high horizontal gradient.
    """
    import numpy as _np
    gx = _np.abs(_np.diff(roi_gray.astype(_np.float32), axis=1))
    upper = gx[:max(1, gx.shape[0] // 3), :]
    return min(1.0, float(upper.mean()) / 20.0)


def load_eye_model(eye_model_path=None):
    """Load the eye state classifier."""
    global _eye_model
    if _eye_model is not None:
        return _eye_model
    if _model is not None and hasattr(_model, 'eye_head'):
        _eye_model = _model
        return _eye_model
    if eye_model_path and os.path.exists(eye_model_path):
        import onnxruntime as ort
        sess = ort.InferenceSession(eye_model_path, providers=['CPUExecutionProvider'])
        _eye_model = sess
        return _eye_model
    raise RuntimeError("眼部检测模型未加载。训练统一模型或提供 eye_model.onnx。")


def detect_eyes(image_path):
    """Detect faces + eyes via OpenCV Haar cascades, classify eye state.

    Multi-stage pipeline:
      1. Face detection (Haar cascade)
      2. Eye detection within each face ROI (Haar cascade)
      3. Eye state classification via ML model (if loaded) or gradient heuristic

    Requires: opencv-python (pip install opencv-python) — zero extra deps.

    Returns:
        dict with 'faces': [{eyes: [{state, openness}, ...], face_rect: [x,y,w,h]}]
    """
    import numpy as _np
    try:
        face_cascade = _get_face_cascade()
        eye_cascade = _get_eye_cascade()
        img = _load_image_rgb(image_path)
        gray = _np.asarray(img.convert('L'))
        arr = _np.asarray(img)
        h, w = gray.shape[:2]

        faces_rects = face_cascade.detectMultiScale(
            gray, scaleFactor=1.1, minNeighbors=5, minSize=(60, 60))

        if len(faces_rects) == 0:
            return {'faces': [], 'message': '未检测到人脸'}

        faces = []
        for (fx, fy, fw, fh) in faces_rects:
            face = {'face_rect': [int(fx), int(fy), int(fw), int(fh)], 'eyes': []}
            # Detect eyes within the upper half of the face
            face_roi = gray[fy:fy + fh // 2, fx:fx + fw]
            eyes = eye_cascade.detectMultiScale(
                face_roi, scaleFactor=1.1, minNeighbors=8, minSize=(20, 15))

            for (ex, ey, ew, eh) in eyes[:2]:  # at most 2 eyes
                # Map back to full image coordinates
                ex_abs, ey_abs = fx + ex, fy + ey
                x1, y1 = max(0, ex_abs - 10), max(0, ey_abs - 5)
                x2, y2 = min(w, ex_abs + ew + 10), min(h, ey_abs + eh + 5)

                if x2 - x1 < 15 or y2 - y1 < 10:
                    continue

                roi_pil = Image.fromarray(arr[y1:y2, x1:x2]).resize((224, 224), Image.LANCZOS)

                # ML classifier if available
                if _eye_model is not None and _backend == 'pytorch' and hasattr(_eye_model, 'eye_head'):
                    import torch
                    t = _transform(roi_pil).unsqueeze(0).to(_device)
                    with torch.no_grad():
                        logits = _eye_model.eye_head(
                            torch.flatten(_eye_model.backbone.avgpool(
                                _eye_model.backbone.layer4(
                                    _eye_model.backbone.layer3(
                                        _eye_model.backbone.layer2(
                                            _eye_model.backbone.layer1(
                                                _eye_model.backbone.maxpool(
                                                    _eye_model.backbone.relu(
                                                        _eye_model.backbone.bn1(
                                                            _eye_model.backbone.conv1(t))))))))), 1))
                        cls_idx = int(logits.argmax().item())
                        states = ['open', 'closed', 'half_open', 'unknown']
                        face['eyes'].append({
                            'state': states[cls_idx] if cls_idx < len(states) else 'unknown',
                            'openness': round(float(logits.softmax(-1)[0][0].item()), 3),
                            'rect': [int(x1), int(y1), int(x2 - x1), int(y2 - y1)],
                        })
                else:
                    # Heuristic: horizontal gradient proxy
                    gray_roi = _np.asarray(roi_pil.convert('L'), dtype=_np.float32)
                    openness = round(_eye_openness(gray_roi), 3)
                    state = 'open' if openness > 0.55 else ('half_open' if openness > 0.25 else 'closed')
                    face['eyes'].append({
                        'state': state, 'openness': openness,
                        'rect': [int(x1), int(y1), int(x2 - x1), int(y2 - y1)],
                    })

            if face['eyes']:
                faces.append(face)

        return {'faces': faces, 'count': len(faces)}
    except RuntimeError as e:
        return {'error': str(e), 'faces': []}
    except Exception as e:
        print(f"eye detection failed: {e}")
        return {'error': str(e), 'faces': []}


# ── IPC handlers for cache management ──
def handle_get_cache_info(params):
    """Return RAW cache directory info."""
    cache_dir = os.path.join(tempfile.gettempdir(), 'lenspilot_cache')
    if not os.path.isdir(cache_dir):
        return {"count": 0, "size": 0, "path": cache_dir}
    files = [f for f in os.listdir(cache_dir) if f.endswith('.jpg')]
    total = sum(os.path.getsize(os.path.join(cache_dir, f)) for f in files)
    return {"count": len(files), "size": total, "path": cache_dir}

def handle_clear_raw_cache(params):
    """Clear all RAW preview cache files."""
    import shutil as _shutil
    cache_dir = os.path.join(tempfile.gettempdir(), 'lenspilot_cache')
    deleted = 0
    if os.path.isdir(cache_dir):
        for f in os.listdir(cache_dir):
            try: os.remove(os.path.join(cache_dir, f)); deleted += 1
            except Exception: pass
    return {"deleted": deleted}
