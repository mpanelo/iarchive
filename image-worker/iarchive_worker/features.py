"""Explainable luminance metrics. Darkness alone never produces a deletion decision."""

import numpy as np
from PIL import Image
import imagehash


def measure(image) -> dict:
    sample = image.copy()
    sample.thumbnail((256, 256), Image.Resampling.LANCZOS)
    luminance = np.asarray(sample.convert("L"), dtype=np.float64)
    p50, p95 = np.percentile(luminance, [50, 95])
    near_black = float(np.mean(luminance < 16))
    dx = np.abs(np.diff(luminance, axis=1))
    dy = np.abs(np.diff(luminance, axis=0))
    edges = float((dx.mean() if dx.size else 0) + (dy.mean() if dy.size else 0)) / 2
    if min(luminance.shape) >= 3:
        laplacian = (-4 * luminance[1:-1, 1:-1] + luminance[:-2, 1:-1] + luminance[2:, 1:-1] + luminance[1:-1, :-2] + luminance[1:-1, 2:])
        sharpness = float(np.var(laplacian))
    else:
        sharpness = 0.0
    darkness = max(0.0, 1.0 - float(p95) / 40.0)
    texture = max(0.0, 1.0 - edges / 8.0)
    score = darkness * near_black * texture
    return {
        "phash": str(imagehash.phash(image, hash_size=8)),
        "width": image.width,
        "height": image.height,
        "p50": float(p50),
        "p95": float(p95),
        "near_black_share": near_black,
        "edge_energy": edges,
        "sharpness": sharpness,
        "dark_candidate_score": score,
        "dark_reason": f"p95={p95:.1f}/255; pixels below 16={near_black:.1%}; edge energy={edges:.2f}. Night scenes, portraits, concerts and screenshots may be meaningful; review only.",
    }
