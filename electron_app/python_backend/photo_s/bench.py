"""
PhotoS - Batch Pipeline Benchmark

Runs the processing pipeline over a set of real images at several worker
counts and reports wall time, speedup, a per-stage time breakdown
(decode/load, core processing, encode/save) and — optionally — output
quality (PSNR/SSIM against the sources).

All outputs go to a temporary directory that is removed when the benchmark
ends, even on KeyboardInterrupt — the source directory is never touched.
"""

import os
import shutil
import tempfile
import threading
import time
from dataclasses import replace

from . import engine
from .metrics import compute_psnr, compute_ssim

# Prefix for the scratch directory; tests and users can spot leftovers.
TMP_PREFIX = "photo-s-bench-"


class _StageTimer:
    """Accumulates time spent inside selected engine functions.

    Wraps ``engine.process_image`` (whole pipeline), ``engine._get_image``
    (decode/load) and ``engine._save_image`` (encode/save) from the bench
    side, so the engine itself needs no timing hooks. Core processing time
    is the pipeline time minus the two measured stages.

    Totals sum across worker threads: with jobs > 1 the breakdown is
    worker-time and can exceed the wall clock. For jobs = 1 it lines up
    with the wall time of the run.
    """

    _WRAPPED = {"process_image": "pipeline", "_get_image": "load",
                "_save_image": "save"}

    def __init__(self):
        self._lock = threading.Lock()
        self.totals = {"pipeline": 0.0, "load": 0.0, "save": 0.0}
        self._orig = {}

    def _wrap(self, key, func):
        def timed(*args, **kwargs):
            t0 = time.monotonic()
            try:
                return func(*args, **kwargs)
            finally:
                dt = time.monotonic() - t0
                with self._lock:
                    self.totals[key] += dt
        return timed

    def __enter__(self):
        self._orig = {name: getattr(engine, name) for name in self._WRAPPED}
        for name, key in self._WRAPPED.items():
            setattr(engine, name, self._wrap(key, self._orig[name]))
        return self

    def __exit__(self, *exc):
        for name, func in self._orig.items():
            setattr(engine, name, func)
        self._orig = {}
        return False

    def reset(self):
        with self._lock:
            for key in self.totals:
                self.totals[key] = 0.0

    def snapshot(self):
        with self._lock:
            return dict(self.totals)


def _evaluate(results):
    """Mean PSNR/SSIM of successful outputs against their sources.

    Returns {"files": n, "psnr_db": float|None, "ssim": float|None}.
    psnr_db is None when every compared pair is pixel-identical (inf dB);
    ssim is None only when nothing could be compared.
    """
    psnrs = []
    ssims = []
    for r in results:
        if not r.success or not r.output_path or not os.path.exists(r.output_path):
            continue
        try:
            psnrs.append(compute_psnr(r.input_path, r.output_path))
            ssims.append(compute_ssim(r.input_path, r.output_path))
        except Exception:
            # Unreadable output/input must not kill the benchmark report.
            continue
    finite = [p for p in psnrs if p != float("inf")]
    return {
        "files": len(ssims),
        "psnr_db": round(sum(finite) / len(finite), 2) if finite else None,
        "ssim": round(sum(ssims) / len(ssims), 4) if ssims else None,
    }


def run_benchmark(files, job_list, options, evaluate=False):
    """Run the benchmark; return the report dict.

    Args:
        files: Source image paths (never modified, outputs never land here).
        job_list: Worker counts to benchmark, in order.
        options: Base ProcessOptions. jobs/output_dir/overwrite are
            overridden per run; remove_original is forced off for safety.
        evaluate: When True, score the first run's outputs against the
            sources (PSNR/SSIM) into the "evaluate" section. Quality is
            worker-count independent, so one run is enough.

    Returns:
        {"runs": [{"jobs", "files", "seconds", "speedup", "errors",
                   "stages": {"load", "process", "save"}}...],
         "evaluate": None | {"files", "psnr_db", "ssim"}}
    """
    tmpdir = tempfile.mkdtemp(prefix=TMP_PREFIX)
    try:
        runs = []
        baseline = None
        first_results = None
        with _StageTimer() as timer:
            for j in job_list:
                run_dir = os.path.join(tmpdir, f"jobs{j}")
                os.makedirs(run_dir, exist_ok=True)
                opts = replace(options, jobs=j, output_dir=run_dir,
                               overwrite=True, remove_original=False)
                timer.reset()
                t0 = time.monotonic()
                result = engine.batch_process(files, opts)
                dt = time.monotonic() - t0
                snap = timer.snapshot()
                load, save = snap["load"], snap["save"]
                process = max(0.0, snap["pipeline"] - load - save)
                if baseline is None:
                    baseline = dt
                if first_results is None:
                    first_results = result.results
                runs.append({
                    "jobs": j,
                    "files": len(files),
                    "seconds": round(dt, 3),
                    "speedup": round(baseline / dt, 3) if dt > 0 else 1.0,
                    "errors": sum(1 for r in result.results if not r.success),
                    "stages": {
                        "load": round(load, 3),
                        "process": round(process, 3),
                        "save": round(save, 3),
                    },
                })
        return {
            "runs": runs,
            "evaluate": _evaluate(first_results) if evaluate else None,
        }
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
