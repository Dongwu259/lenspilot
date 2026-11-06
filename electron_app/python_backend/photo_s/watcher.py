"""
PhotoS - Folder Watcher Daemon

Monitors a directory for new image files and auto-processes them.
Uses the watchdog library.
"""

import os
import time
import threading
from pathlib import Path
from typing import Callable, Optional

from .engine import (
    ProcessOptions,
    ProcessResult,
    process_image,
    format_size,
    ALL_INPUT_EXTENSIONS,
)


class _DebouncedHandler:
    """File-system event handler with debounce (wait for file to stabilize).

    Wraps watchdog's FileSystemEventHandler pattern.
    """

    # A file whose size never stops changing is abandoned after this many
    # stability checks (one per tick, so ≈ one per second).
    _MAX_STABILIZE_ATTEMPTS = 30

    def __init__(self, options: ProcessOptions,
                 on_process: Optional[Callable[[ProcessResult], None]] = None,
                 on_file: Optional[Callable[[str], None]] = None):
        """``on_file``（v2.5 autopilot）：稳定文件交给回调自定义处理，
        本 handler 不再走内置 process_image——返回 ProcessResult 列表恒空。
        与 ``on_process`` 互斥（on_file 优先）。"""
        self.options = options
        self.on_process = on_process
        self.on_file = on_file
        self._pending = {}   # path → first seen time
        self._processed = set()  # paths already processed
        self._attempts = {}  # path → failed stability-check count

    def _is_own_output(self, path) -> bool:
        """True for files this watcher writes itself (stem ends with the
        output suffix). Without -o the output lands back in the watched
        directory and would retrigger processing forever:
        a.jpg → a_compressed.jpg → a_compressed_compressed.jpg → …"""
        suffix = self.options.suffix
        return bool(suffix) and Path(path).stem.endswith(suffix)

    def on_created(self, event):
        """Called when a file is created."""
        if event.is_directory:
            return
        path = event.src_path
        ext = Path(path).suffix.lower()
        if ext not in ALL_INPUT_EXTENSIONS:
            return
        if self._is_own_output(path):
            return
        self._pending[path] = time.time()

    def on_modified(self, event):
        """v2.5：就地改写（不重建）的文件同样进防抖通道。

        已处理过的路径仍被 ``_processed`` 挡住——不会重复处理；
        这里只兜住「建 watcher 前就存在、之后被编辑器原地改写」的文件。
        """
        self.on_created(event)

    def tick(self):
        """Check pending files; process those that have stabilized."""
        now = time.time()
        to_process = []

        for path, first_seen in list(self._pending.items()):
            if now - first_seen < 2.0:  # 2-second debounce
                continue
            if path in self._processed or self._is_own_output(path):
                del self._pending[path]
                continue
            # Also check the file is still there and hasn't changed
            try:
                size = os.path.getsize(path)
                time.sleep(0.3)
                stable = os.path.getsize(path) == size
            except OSError:
                # Vanished before it could be processed — drop it quietly.
                del self._pending[path]
                continue
            if stable:
                to_process.append(path)
                # Mark processed / leave pending ONLY when the file actually
                # goes out for processing. Doing it unconditionally stranded
                # slow-copying files (never processed) and made later
                # re-drops of the same name get skipped forever.
                self._processed.add(path)
                del self._pending[path]
                self._attempts.pop(path, None)
            else:
                # Still being written (e.g. a large file mid-copy) — retry
                # next tick, with a cap so a perpetually-changing file is
                # eventually abandoned instead of re-statted forever.
                attempts = self._attempts.get(path, 0) + 1
                self._attempts[path] = attempts
                if attempts >= self._MAX_STABILIZE_ATTEMPTS:
                    print(f"⚠️  文件一直未稳定，已跳过 "
                          f"File never stabilized, skipped: {path}")
                    del self._pending[path]
                    self._attempts.pop(path, None)

        if self.on_file:
            for path in to_process:
                self.on_file(path)
            return []

        results = []
        for path in to_process:
            try:
                result = process_image(path, self.options)
                results.append(result)
                if self.on_process:
                    self.on_process(result)
            except Exception as e:
                results.append(ProcessResult(
                    input_path=path, output_path="",
                    input_size=0, output_size=0,
                    input_format="", output_format="",
                    input_dims=(0, 0), output_dims=(0, 0),
                    success=False, error=str(e),
                ))

        return results


def start_watching(
    watch_dir: str,
    options: ProcessOptions,
    recursive: bool = False,
    on_process: Optional[Callable[[ProcessResult], None]] = None,
    stop_event: Optional[threading.Event] = None,
    on_file: Optional[Callable[[str], None]] = None,
) -> None:
    """Watch a directory for new images and process them automatically.

    Blocks until Ctrl+C, or until ``stop_event`` is set (used by the GUI;
    the CLI leaves it None for the original Ctrl+C behavior).

    Args:
        watch_dir: Directory to monitor.
        options: ProcessOptions for auto-processing.
        recursive: Watch subdirectories too.
        on_process: Callback when a file is processed.
        stop_event: When set, the loop exits and the observer is stopped.
        on_file: v2.5 autopilot hook — stable files go to this callback
            instead of the built-in process_image (stdout stays quiet: the
            caller owns reporting).
    """
    try:
        from watchdog.observers import Observer
        from watchdog.events import FileSystemEventHandler
    except ImportError:
        print("❌ watchdog not installed. Run: pip3 install watchdog")
        return

    handler = _DebouncedHandler(options, on_process, on_file=on_file)

    class Handler(FileSystemEventHandler):
        def on_created(self, event):
            handler.on_created(event)

        def on_modified(self, event):
            handler.on_modified(event)

    observer = Observer()
    observer.schedule(Handler(), watch_dir, recursive=recursive)
    observer.start()

    if not on_file:
        print(f"👁  正在监视 Watching: {watch_dir}")
        print(f"   设置: {options.output_format} q={options.quality}")
        if options.remove_original:
            print("   ⚠️  处理后删除原文件 Remove original is ON")
        print("   按 Ctrl+C 停止 Press Ctrl+C to stop")
        print()

    processed_count = 0

    try:
        while not (stop_event is not None and stop_event.is_set()):
            time.sleep(1)
            results = handler.tick()
            for r in results:
                processed_count += 1
                name = os.path.basename(r.input_path)
                if r.success:
                    savings = r.input_size - r.output_size
                    print(f"  ✅ #{processed_count} {name} → "
                          f"{format_size(r.output_size)} "
                          f"(-{format_size(savings)})")
                    if r.achieved_quality:
                        print(f"     质量 Quality: {r.achieved_quality}")
                else:
                    print(f"  ❌ #{processed_count} {name}: {r.error}")
    except KeyboardInterrupt:
        print(f"\n👁  已停止 Stopped. 共处理 {processed_count} 个文件.")
    finally:
        observer.stop()
        observer.join()
