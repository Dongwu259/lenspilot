"""
preference_engine.py — Personalized aesthetic preference learning.

Provides:
  - EloRatingEngine: pairwise photo comparison → Elo ratings
  - CalibrationLearner: user corrections → score remapping function
  - FeatureWeightLearner: deep features → personalized weight vector
  - ProfileManager: multi-user profile CRUD + persistence
  - FeedbackCollector: implicit preference signals from user actions
"""

import os
import json
import time
import math
import threading

# ── Profile persistence ──

def _profile_dir():
    """Get the user preferences directory."""
    env = os.environ.get('APS_USER_DATA', '').strip()
    if env:
        base = env
    else:
        import sys
        if sys.platform == 'darwin':
            base = os.path.expanduser('~/Library/Application Support/LensPilot')
        elif sys.platform == 'win32':
            base = os.path.join(os.environ.get('APPDATA', os.path.expanduser('~')), 'LensPilot')
        else:
            base = os.path.expanduser('~/.lenspilot')
    d = os.path.join(base, 'preferences')
    os.makedirs(d, exist_ok=True)
    return d


def _profile_path(profile_id="default"):
    safe = (profile_id or "default").replace("..", "").replace("/", "_").replace("\\", "_")
    return os.path.join(_profile_dir(), f"{safe}.json")


# ══════════════════════════════════════════════════════════════════════
# Elo Rating Engine — pairwise comparison → relative rankings
# ══════════════════════════════════════════════════════════════════════

class EloRatingEngine:
    """Manages Elo ratings for photos based on pairwise A/B comparisons.

    Standard Elo: expected = 1 / (1 + 10^((rb - ra) / 400))
    update: ra' = ra + K * (outcome - expected)

    outcome: 1 if photo A wins, 0 if photo B wins, 0.5 for tie
    """

    def __init__(self, k_factor=32, base_rating=1500):
        self.k = k_factor
        self.base = base_rating
        self.ratings = {}     # path -> rating
        self.comparisons = []  # list of (path_a, path_b, winner)
        self._lock = threading.Lock()

    def _expected(self, ra, rb):
        return 1.0 / (1.0 + 10.0 ** ((rb - ra) / 400.0))

    def compare(self, path_a, path_b, winner="a"):
        """Record one comparison: winner is 'a' if A wins, 'b' if B wins, 'draw' for tie."""
        with self._lock:
            ra = self.ratings.get(path_a, self.base)
            rb = self.ratings.get(path_b, self.base)
            ea = self._expected(ra, rb)
            if winner == "a":
                sa = 1.0
            elif winner == "b":
                sa = 0.0
            else:
                sa = 0.5
            self.ratings[path_a] = ra + self.k * (sa - ea)
            self.ratings[path_b] = rb + self.k * ((1.0 - sa) - (1.0 - ea))
            self.comparisons.append((path_a, path_b, winner))

    def get_ratings(self):
        with self._lock:
            return dict(self.ratings)

    def get_top(self, n=50):
        with self._lock:
            return sorted(self.ratings.items(), key=lambda x: x[1], reverse=True)[:n]

    def comparison_count(self):
        return len(self.comparisons)

    def to_dict(self):
        with self._lock:
            return {
                "ratings": dict(self.ratings),
                "comparisons": list(self.comparisons),
                "k_factor": self.k,
                "base_rating": self.base,
            }

    @classmethod
    def from_dict(cls, d):
        eng = cls(k_factor=d.get("k_factor", 32), base_rating=d.get("base_rating", 1500))
        eng.ratings = d.get("ratings", {})
        eng.comparisons = d.get("comparisons", [])
        return eng


# ══════════════════════════════════════════════════════════════════════
# Calibration Learner — model score → user score remapping
# ══════════════════════════════════════════════════════════════════════

class CalibrationLearner:
    """Learns a remapping function: user_score = f(model_score).

    Supports:
      - linear: user_score = slope * model_score + intercept (clamped 1-10)
      - none: identity function (until enough data)

    Trained on pairs of (model_score, user_adjusted_score).
    """

    def __init__(self):
        self._pairs = []  # list of (model_score, user_score)
        self._params = None  # {"type": "linear", "slope": 1.0, "intercept": 0.0}
        self._lock = threading.Lock()

    def add_correction(self, model_score, user_score):
        """Record a user correction: the model gave model_score, user wants user_score."""
        with self._lock:
            self._pairs.append((float(model_score), float(user_score)))
        self._refit()

    def add_corrections(self, pairs):
        """Bulk add (model_score, user_score) pairs."""
        with self._lock:
            for ms, us in pairs:
                self._pairs.append((float(ms), float(us)))
        self._refit()

    def _refit(self):
        """Fit linear regression on collected pairs."""
        with self._lock:
            if len(self._pairs) < 5:
                self._params = None  # not enough data
                return
            n = len(self._pairs)
            sum_x = sum(p[0] for p in self._pairs)
            sum_y = sum(p[1] for p in self._pairs)
            sum_xy = sum(p[0] * p[1] for p in self._pairs)
            sum_x2 = sum(p[0] * p[0] for p in self._pairs)
            denom = n * sum_x2 - sum_x * sum_x
            if abs(denom) < 1e-9:
                self._params = {"type": "linear", "slope": 1.0, "intercept": 0.0}
                return
            slope = (n * sum_xy - sum_x * sum_y) / denom
            intercept = (sum_y - slope * sum_x) / n
            self._params = {"type": "linear", "slope": round(slope, 4), "intercept": round(intercept, 4)}

    def apply(self, model_score):
        """Apply calibration to a model score → user score (1-10)."""
        with self._lock:
            if self._params is None:
                return model_score
            p = self._params
            if p["type"] == "linear":
                calibrated = p["slope"] * model_score + p["intercept"]
                return max(1.0, min(10.0, calibrated))
            return model_score

    def pair_count(self):
        return len(self._pairs)

    def is_ready(self):
        return self._params is not None and len(self._pairs) >= 5

    def to_dict(self):
        with self._lock:
            return {"pairs": list(self._pairs), "params": self._params}

    @classmethod
    def from_dict(cls, d):
        learner = cls()
        learner._pairs = d.get("pairs", [])
        learner._params = d.get("params")
        if learner._params is None and len(learner._pairs) >= 5:
            learner._refit()
        return learner


# ══════════════════════════════════════════════════════════════════════
# Feature Weight Learner — 128-d features → personalized adjustment
# ══════════════════════════════════════════════════════════════════════

class FeatureWeightLearner:
    """Learns a linear weight vector over deep features.

    adjustment = W · features + b
    final_score = α * model_score + (1-α) * adjustment

    Trained via ridge regression on (features, user_score) pairs.
    Uses a simple online SGD approach for incremental learning.
    """

    DIM = 128  # matches extract_deep_feature() output

    def __init__(self, alpha=0.7):
        self.alpha = alpha  # blend ratio: model_score weight
        self.weights = [0.0] * self.DIM  # 128-d weight vector
        self.bias = 0.0
        self._samples = []  # (features_list, user_score) tuples
        self._lock = threading.Lock()

    def add_sample(self, features, user_score):
        """features: list/array of 128 floats; user_score: 1-10."""
        with self._lock:
            if len(features) != self.DIM:
                return
            self._samples.append((list(features), float(user_score)))
            # Auto-fit every 10 samples so the learner stays up-to-date
            n = len(self._samples)
            if n >= 10 and n % 10 == 0:
                self._do_fit()

    def _do_fit(self):
        """Internal: perform ridge regression on accumulated samples.
        Caller must hold self._lock."""
        n = len(self._samples)
        if n < 10:
            return False
        # Simple closed-form ridge regression
        # X: n × 129 (bias column + 128 features), y: n × 1
        X = [[1.0] + s[0] for s in self._samples]  # add bias column
        y = [s[1] for s in self._samples]
        # (X^T X + λI)^(-1) X^T y with λ = 0.01
        import numpy as np
        X_np = np.array(X, dtype=np.float64)
        y_np = np.array(y, dtype=np.float64)
        lam = 0.01
        I = np.eye(X_np.shape[1])
        I[0, 0] = 0  # don't regularize bias
        try:
            w = np.linalg.solve(X_np.T @ X_np + lam * I, X_np.T @ y_np)
            self.bias = float(w[0])
            self.weights = w[1:].tolist()
            return True
        except np.linalg.LinAlgError:
            return False

    def fit(self):
        """Fit weights via ridge regression (L2 regularized).
        Call after accumulating samples; also called automatically
        by add_sample() every 10 samples."""
        with self._lock:
            return self._do_fit()

    def predict(self, features):
        """Compute personalized adjustment for a feature vector."""
        if not self.weights or not any(w != 0 for w in self.weights):
            return 0.0
        if len(features) != self.DIM:
            return 0.0
        dot = self.bias
        for w, f in zip(self.weights, features):
            dot += w * f
        return dot

    def blend(self, model_score, features):
        """Return final_score = α * model_score + (1-α) * adjustment."""
        adj = self.predict(features)
        blended = self.alpha * model_score + (1.0 - self.alpha) * adj
        return max(1.0, min(10.0, blended))

    def sample_count(self):
        return len(self._samples)

    def to_dict(self):
        with self._lock:
            return {
                "alpha": self.alpha,
                "weights": list(self.weights),
                "bias": self.bias,
                "sample_count": len(self._samples),
                "samples": self._samples[-200:],  # keep last 200 samples for incremental retraining
            }

    @classmethod
    def from_dict(cls, d):
        learner = cls(alpha=d.get("alpha", 0.7))
        learner.weights = d.get("weights", [0.0] * cls.DIM)
        learner.bias = d.get("bias", 0.0)
        learner._samples = d.get("samples", [])
        return learner


# ══════════════════════════════════════════════════════════════════════
# Profile Manager — multi-user preference profiles
# ══════════════════════════════════════════════════════════════════════

class ProfileManager:
    """Manages multiple user aesthetic preference profiles."""

    DEFAULT_PROFILE = "default"

    def __init__(self):
        self._lock = threading.Lock()
        self._active = self.DEFAULT_PROFILE
        self._cache = {}  # profile_id -> dict

    def _load(self, profile_id):
        path = _profile_path(profile_id)
        if not os.path.exists(path):
            return self._empty_profile(profile_id)
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            # Restore engines from serialized state
            data["elo_engine"] = EloRatingEngine.from_dict(data.get("elo", {}))
            data["calibration"] = CalibrationLearner.from_dict(data.get("cal", {}))
            data["feature_weights"] = FeatureWeightLearner.from_dict(data.get("fw", {}))
            return data
        except (json.JSONDecodeError, KeyError, IOError):
            return self._empty_profile(profile_id)

    def _empty_profile(self, profile_id):
        return {
            "profile_id": profile_id,
            "name": profile_id,
            "created_at": int(time.time()),
            "updated_at": int(time.time()),
            "elo": {},
            "elo_engine": EloRatingEngine(),
            "cal": {},
            "calibration": CalibrationLearner(),
            "fw": {},
            "feature_weights": FeatureWeightLearner(),
            "reference_features": None,  # centroid of reference photos (128-d list)
            "preferences_text": "",
            "comparison_count": 0,
        }

    def _save(self, profile):
        """Persist profile to disk (serializing engine states)."""
        data = {
            "profile_id": profile["profile_id"],
            "name": profile["name"],
            "created_at": profile["created_at"],
            "updated_at": int(time.time()),
            "elo": profile["elo_engine"].to_dict(),
            "cal": profile["calibration"].to_dict(),
            "fw": profile["feature_weights"].to_dict(),
            "reference_features": profile.get("reference_features"),
            "preferences_text": profile.get("preferences_text", ""),
            "comparison_count": profile["elo_engine"].comparison_count(),
        }
        path = _profile_path(profile["profile_id"])
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)

    # ── Profile CRUD ──

    def list_profiles(self):
        profiles = []
        d = _profile_dir()
        if os.path.isdir(d):
            for name in sorted(os.listdir(d)):
                if name.endswith(".json") and not name.endswith(".tmp"):
                    pid = name[:-5]
                    profiles.append({"profile_id": pid, "name": pid})
        if not profiles:
            profiles.append({"profile_id": self.DEFAULT_PROFILE, "name": "默认"})
        return profiles

    def load(self, profile_id=None):
        with self._lock:
            pid = profile_id or self._active
            if pid not in self._cache:
                self._cache[pid] = self._load(pid)
            self._active = pid
            return self._cache[pid]

    def save(self):
        with self._lock:
            if self._active in self._cache:
                self._save(self._cache[self._active])

    def create(self, profile_id, name=None):
        with self._lock:
            if profile_id in self._cache:
                return None
            p = self._empty_profile(profile_id)
            if name:
                p["name"] = name
            self._cache[profile_id] = p
            self._save(p)
            return p

    def delete(self, profile_id):
        with self._lock:
            if profile_id == self.DEFAULT_PROFILE:
                return False
            path = _profile_path(profile_id)
            if os.path.exists(path):
                os.remove(path)
            self._cache.pop(profile_id, None)
            if self._active == profile_id:
                self._active = self.DEFAULT_PROFILE
            return True

    def get_active(self):
        with self._lock:
            pid = self._active
            if pid not in self._cache:
                self._cache[pid] = self._load(pid)
            return self._cache[pid]


# ══════════════════════════════════════════════════════════════════════
# Feedback Collector — implicit preference signals
# ══════════════════════════════════════════════════════════════════════

class FeedbackCollector:
    """Collects implicit preference signals from user operations.

    Tracks:
      - manual_keep (keep=1 or reject=-1) per photo
      - which photos were selected for export/album
      - scoring threshold changes over time
    """

    def __init__(self, profile_manager=None):
        self.pm = profile_manager or ProfileManager()
        self._events = []  # in-memory buffer
        self._lock = threading.Lock()

    def record_manual_keep(self, path, keep_value, model_score):
        """Record a manual keep/reject action."""
        with self._lock:
            self._events.append({
                "type": "manual_keep",
                "path": path,
                "keep": keep_value,
                "model_score": model_score,
                "time": int(time.time()),
            })
            # Also feed to calibration if both scores available
            if keep_value == 1 and model_score is not None and model_score > 0:
                profile = self.pm.get_active()
                target = min(10.0, model_score + 2.5)
                profile["calibration"].add_correction(model_score, target)
            elif keep_value == -1 and model_score is not None and model_score > 0:
                profile = self.pm.get_active()
                target = max(1.0, model_score - 2.5)
                profile["calibration"].add_correction(model_score, target)

    def record_export_selection(self, paths, threshold):
        """Record which photos were selected for export."""
        with self._lock:
            self._events.append({
                "type": "export_selection",
                "count": len(paths),
                "threshold": threshold,
                "time": int(time.time()),
            })

    def flush(self):
        """Persist collected feedback to the active profile."""
        self.pm.save()

    def event_count(self):
        return len(self._events)

    def get_recent(self, n=50):
        return self._events[-n:]


# ══════════════════════════════════════════════════════════════════════
# Convenience: apply personalization to a model score
# ══════════════════════════════════════════════════════════════════════

# Global singleton — initialized on first use
_profile_manager = None
_feedback_collector = None


def get_profile_manager():
    global _profile_manager
    if _profile_manager is None:
        _profile_manager = ProfileManager()
    return _profile_manager


def get_feedback_collector():
    global _feedback_collector
    if _feedback_collector is None:
        _feedback_collector = FeedbackCollector(get_profile_manager())
    return _feedback_collector


def apply_user_calibration(model_score, features=None):
    """Apply active user profile's personalization to a model score.

    Returns: (calibrated_score, blended_score_or_calibrated)
      - calibrated_score: after calibration remapping (always applied)
      - If features (128-d) provided: blend model score with feature adjustment
    """
    pm = get_profile_manager()
    profile = pm.get_active()
    cal = profile["calibration"]
    calibrated = cal.apply(model_score)
    if features is not None and len(features) == FeatureWeightLearner.DIM:
        fw = profile["feature_weights"]
        if fw.sample_count() >= 10:
            blended = fw.blend(calibrated, features)
            return (calibrated, blended)
    return (calibrated, calibrated)


def register_implicit_feedback(path, model_score, keep_value):
    """Hook called from photo_scorer when user sets manual_keep."""
    try:
        fc = get_feedback_collector()
        fc.record_manual_keep(path, keep_value, model_score)
    except Exception:
        pass  # best-effort, never break the main flow
