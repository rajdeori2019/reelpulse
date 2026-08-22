"""Fit the VVS weights against your own verified Instagram numbers.

This closes the hybrid loop. The global leaderboard is built from proxy signals
and therefore carries assumptions baked into `config/weights.yaml`. Your own
account, via the Graph API, returns numbers Meta itself computed. Ridge
regression from your reels' craft/performance features onto their real view
counts tells you which components actually predict outcomes *for you*.

Two guardrails, because small-n fitting is where analytics tools usually start
lying:
  * below `min_samples` reels it refuses to fit and says why;
  * weights are shrunk toward the shipped defaults in proportion to how little
    data you have, so twelve reels nudge the config rather than replacing it.
"""
from __future__ import annotations

import logging
from pathlib import Path

import numpy as np
import yaml
from sklearn.linear_model import RidgeCV
from sklearn.preprocessing import StandardScaler

from ..config import CONFIG_DIR, load_weights
from .features import caption_shape, detect_hooks, detect_topic, duration_bucket
from .score import COMPONENTS

log = logging.getLogger("reelpulse")

MIN_SAMPLES = 12


def _row(media: dict) -> tuple[list[float], float] | None:
    metrics = media.get("metrics", {})
    views = float(metrics.get("views") or 0)
    if views <= 0:
        return None

    caption = media.get("caption") or ""
    shape = caption_shape(caption)
    hooks = detect_hooks(caption)

    reach = float(metrics.get("reach") or 0)
    likes = float(metrics.get("likes") or metrics.get("like_count") or 0)
    comments = float(metrics.get("comments") or metrics.get("comments_count") or 0)
    shares = float(metrics.get("shares") or 0)
    saved = float(metrics.get("saved") or 0)
    watch = float(metrics.get("ig_reels_avg_watch_time") or 0)

    features = [
        np.log10(reach + 1),                          # magnitude proxy
        watch / 1000.0,                               # velocity proxy (avg watch s)
        0.0,                                          # acceleration: n/a offline
        1.0,                                          # breadth: own account only
        (likes + 3 * comments) / views,               # engagement quality
        (shares + saved) / max(views / 100_000, 1e-6),  # share ratio
        0.0,                                          # topic momentum: n/a offline
        1.0,                                          # recency: normalised out
        float(shape["caption_words"]),
        float(shape["hashtag_count"]),
        1.0 if shape["has_question"] else 0.0,
        1.0 if shape["has_cta"] else 0.0,
        1.0 if hooks[0] != "none_detected" else 0.0,
    ]
    return features, float(np.log10(views + 1))


def calibrate(own_media: list[dict], *, write: bool = True,
              min_samples: int = MIN_SAMPLES) -> dict:
    reels = [m for m in own_media if m.get("is_reel")]
    rows = [r for r in (_row(m) for m in reels) if r]

    if len(rows) < min_samples:
        return {
            "fitted": False,
            "samples": len(rows),
            "reason": (f"Need at least {min_samples} of your own reels with a "
                       f"`views` insight to fit weights; found {len(rows)}. "
                       "Keep posting and re-run — the shipped defaults are used "
                       "until then."),
            "weights": load_weights(),
        }

    features = np.array([r[0] for r in rows])
    targets = np.array([r[1] for r in rows])

    scaler = StandardScaler()
    scaled = scaler.fit_transform(features)
    model = RidgeCV(alphas=np.logspace(-2, 3, 24))
    model.fit(scaled, targets)

    r2 = float(model.score(scaled, targets))
    coefs = np.abs(model.coef_[:len(COMPONENTS)])
    if coefs.sum() > 0:
        coefs = coefs / coefs.sum() * len(COMPONENTS) * 0.9

    defaults = load_weights()
    # Shrinkage: 12 samples -> mostly defaults; 60+ -> mostly fitted.
    trust = min(len(rows) / 60.0, 0.85)

    fitted = {}
    for i, name in enumerate(COMPONENTS):
        default = defaults.get(name, 1.0)
        fitted[name] = round(float((1 - trust) * default + trust * coefs[i]), 3)

    result = {
        "fitted": True,
        "samples": len(rows),
        "r2_in_sample": round(r2, 3),
        "shrinkage_trust": round(trust, 2),
        "weights": fitted,
        "previous_weights": defaults,
        "caveat": ("In-sample R^2 on a small, self-selected set of your own "
                   "reels. Treat it as a nudge, not a law. It says nothing "
                   "about causation."),
    }

    if write:
        path = Path(CONFIG_DIR) / "weights.yaml"
        existing = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        existing["weights"] = fitted
        existing.setdefault("_calibration", {}).update({
            "samples": len(rows), "r2_in_sample": round(r2, 3),
            "shrinkage_trust": round(trust, 2),
        })
        path.write_text(yaml.safe_dump(existing, sort_keys=False), encoding="utf-8")
        log.info("wrote calibrated weights to %s", path)

    return result
