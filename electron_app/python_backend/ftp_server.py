"""
ftp_server.py — Built-in FTP server for receiving photos from cameras.

Uses pyftpdlib to run a lightweight anonymous/write-only FTP server.
When a file upload completes, emits ftp_file_received events to Electron.
"""

import os
import socket
import threading

_ftp_server = None
_ftp_thread = None
_ftp_handler = None


def get_local_ip():
    """Get the LAN IP address."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('8.8.8.8', 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return '127.0.0.1'


class _FTPHandler:
    """pyftpdlib handler with write-only semantics and event callbacks."""

    def __init__(self, conn, server, **kwargs):
        super().__init__(conn, server, **kwargs)

    def on_file_received(self, file_path):
        """Called when a file upload completes."""
        try:
            from service import send_event
            name = os.path.basename(file_path)
            size = os.path.getsize(file_path) if os.path.exists(file_path) else 0
            send_event('ftp_file_received', {
                'path': file_path,
                'name': name,
                'size': size,
            })
        except Exception:
            pass  # Service may not be loaded yet

    def on_incomplete_file_received(self, file_path):
        """Clean up partial uploads."""
        try:
            if os.path.exists(file_path):
                os.remove(file_path)
        except Exception:
            pass


def _create_handler(on_log=None):
    """Dynamically create a handler class with pyftpdlib if available."""
    try:
        from pyftpdlib.authorizers import DummyAuthorizer
        from pyftpdlib.handlers import FTPHandler as BaseFTPHandler
    except ImportError:
        raise ImportError(
            'pyftpdlib is required for FTP server.\n'
            'Install: pip install pyftpdlib'
        )

    class _Handler(BaseFTPHandler, _FTPHandler):
        pass

    # Override to capture logs
    orig_log = _Handler.log

    def _log(self, msg, logfun=None):
        if on_log:
            on_log(msg)
        if logfun:
            logfun(msg)

    _Handler.log = _log
    return _Handler


def start_ftp_server(folder, port=2121, username='', password='', bind_ip='127.0.0.1'):
    """
    Start the FTP server in a background thread.

    Args:
        folder: Directory to receive files into (created if missing)
        port: FTP port (default 2121, 21 requires root/admin)
        username: Username for auth (default 'camera')
        password: Password for auth (auto-generated if not provided)
        bind_ip: IP to bind (default 127.0.0.1, use '0.0.0.0' for LAN)

    Returns:
        dict: {'ip': str, 'port': int, 'folder': str}
    """
    global _ftp_server, _ftp_thread

    if _ftp_server and _ftp_thread and _ftp_thread.is_alive():
        return {'error': 'FTP server already running', 'ip': get_local_ip(), 'port': port}

    from pyftpdlib.authorizers import DummyAuthorizer
    from pyftpdlib.handlers import FTPHandler
    from pyftpdlib.servers import FTPServer

    os.makedirs(folder, exist_ok=True)

    # Authorizer (pyftpdlib >= 2.0 uses permission strings: 'elr'=read, 'adfmwMT'=write)
    authorizer = DummyAuthorizer()
    if username and password:
        authorizer.add_user(username, password, folder, perm='adfmwMT')
    else:
        # Anonymous write-only — camera only needs to upload
        authorizer.add_anonymous(folder, perm='adfmwMT')

    # Custom handler with event callbacks
    class CameraFTPHandler(FTPHandler, _FTPHandler):
        pass

    # Capture log messages
    def log_callback(msg):
        try:
            from service import send_event
            send_event('ftp_log', {'message': msg})
        except Exception:
            pass

    orig_log = CameraFTPHandler.log
    def _log(self, msg, logfun=None):
        # Log connection/transfer info only
        if any(kw in str(msg).lower() for kw in ['connect', 'login', 'stor', 'upload', 'transfer', 'file', '226', 'anon']):
            log_callback(str(msg))
        if logfun:
            logfun(msg)
    CameraFTPHandler.log = _log

    # Start server in background thread
    ip = bind_ip
    started = threading.Event()
    start_result = {}

    def run_server():
        global _ftp_server
        try:
            CameraFTPHandler.authorizer = authorizer  # pyftpdlib >= 2.x: set on handler, not FTPServer
            CameraFTPHandler.max_cons = 10           # limit concurrent connections
            CameraFTPHandler.max_cons_per_ip = 5     # prevent single-client abuse
            _ftp_server = FTPServer((ip, port), CameraFTPHandler)
            from service import send_event
            send_event('ftp_server_started', {
                'ip': ip,
                'port': port,
                'folder': folder,
            })
            started.set()  # bind succeeded — signal before entering the blocking loop
            _ftp_server.serve_forever()
        except Exception as e:
            start_result['error'] = str(e)
            started.set()
            from service import send_event
            send_event('ftp_server_error', {'error': str(e)})

    _ftp_thread = threading.Thread(target=run_server, daemon=True)
    _ftp_thread.start()

    # Wait for the bind result instead of a fixed sleep — port-in-use and
    # permission errors must reach the caller, not just the event stream.
    if not started.wait(timeout=3.0):
        return {'error': 'FTP server start timed out', 'ip': ip, 'port': port, 'running': False}
    if start_result.get('error'):
        return {'error': start_result['error'], 'ip': ip, 'port': port, 'running': False}

    return {
        'ip': ip,
        'port': port,
        'folder': folder,
        'running': True,
    }


def stop_ftp_server():
    """Stop the FTP server."""
    global _ftp_server, _ftp_thread
    if _ftp_server:
        try:
            _ftp_server.close_all()
        except Exception:
            pass
        _ftp_server = None
    _ftp_thread = None
    from service import send_event
    send_event('ftp_server_stopped', {})
    return {'running': False}


def ftp_status():
    """Return current FTP server status."""
    global _ftp_server, _ftp_thread
    running = bool(_ftp_server and _ftp_thread and _ftp_thread.is_alive())
    ip = get_local_ip()
    return {
        'running': running,
        'ip': ip if running else None,
        'port': getattr(_ftp_server, 'port', None) if running else None,
    }


# ── IPC Handlers ──
def register_handlers():
    from service import register_handler

    def handle_ftp_start(params):
        folder = params.get('folder', '')
        if not folder:
            from service import _app_config
            folder = _app_config.get('copy_target_path', os.path.expanduser('~/FTP_Photos'))
        port = int(params.get('port', 2121))
        # Cameras connect over Wi-Fi, so bind the LAN interface by default —
        # a localhost binding makes the advertised address unreachable.
        # Access is gated by the auto-generated password below.
        username = params.get('username', 'camera')
        password = params.get('password', '')
        auto_generated = not password
        if auto_generated:
            password = os.urandom(16).hex()  # 128 bits entropy
        bind_ip = params.get('bind_ip', '0.0.0.0')
        result = start_ftp_server(folder, port, username, password, bind_ip=bind_ip)
        # Address the camera should dial: the real LAN IP, not the 0.0.0.0 wildcard.
        result['lan_ip'] = get_local_ip()
        # Communicate the password to the user (only for auto-generated)
        from service import send_event, send_log
        if auto_generated and password:
            send_event("ftp_credentials", {
                "username": username,
                "password": password,
                "port": port,
                "bind_ip": bind_ip,
            })
            send_log(f"FTP 密码（自动生成）: {password}（用户: {username}，端口: {port}）", "info")
        result["password"] = password
        result["username"] = username
        result["auto_generated"] = auto_generated
        return result

    def handle_ftp_stop(params):
        return stop_ftp_server()

    def handle_ftp_status(params):
        return ftp_status()

    # Shutdown cleanup: gracefully stop the FTP server
    from service import register_cleanup
    register_cleanup(lambda: stop_ftp_server())

    register_handler('ftp_start', handle_ftp_start)
    register_handler('ftp_stop', handle_ftp_stop)
    register_handler('ftp_status', handle_ftp_status)
