"""
Disease / anomaly flagging for tracked tomatoes
------------------------------------------------
IMPORTANT CONTEXT -- read this before demoing "disease detection":

Your `best.pt` model was trained on exactly 3 classes (green, half_ripened,
fully_ripened). It has never seen a diseased tomato, so it cannot classify
disease -- adding a 4th output class requires retraining on a labeled
diseased-tomato dataset (see the "path to a real model" section at the
bottom of this file).

What this module gives you *without* retraining is a lightweight,
color/texture heuristic that flags a tracked tomato as "worth a human
look" -- it looks for patterns strongly associated with common tomato fruit
problems, on the already-cropped bounding box the tracker gives you each
frame:

  - Blossom-end rot / anthracnose-style patches: dark, low-saturation
    blotches on what is otherwise a red/orange/green fruit.
  - Sunscald / early blight lesions: pale, desaturated patches with a
    grayish or tan cast, distinct from healthy skin.

This is a *screening* heuristic, not a diagnosis. It will have false
positives (a sun glare, a leaf shadow, a stem-end shadow can all trigger
it) and false negatives (early-stage disease may look identical to a
healthy fruit). Frame it in your hackathon pitch as "flags fruit for
inspection," not "diagnoses disease" -- that framing is both more honest
and, frankly, a more defensible engineering claim.

For a real hackathon "wow" upgrade path after judging: collect ~100-200
photos of diseased tomatoes (PlantVillage's tomato dataset is a public,
commonly-used starting point), add a 4th class to your data.yaml
(`diseased`), and retrain -- the rest of this codebase (tracking, majority
voting, dashboard) already generalizes to N classes with no changes beyond
`class_names` and `box_colors_bgr` in harvest_config.json.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import cv2
import numpy as np


@dataclass
class DiseaseFlag:
    is_suspect: bool
    reason: str
    confidence: float  # 0.0-1.0, a heuristic score, not a calibrated probability


def analyze_crop(bgr_crop: np.ndarray,
                  dark_spot_ratio_thresh: float = 0.08,
                  pale_patch_ratio_thresh: float = 0.15,
                  min_crop_pixels: int = 400) -> DiseaseFlag:
    """Runs the heuristic on a single cropped tomato image (BGR, as OpenCV
    gives you). Returns a DiseaseFlag. Designed to run every frame on every
    tracked box without noticeably affecting FPS (it's a handful of cheap
    OpenCV ops on a small crop, no additional model inference).
    """
    if bgr_crop is None or bgr_crop.size == 0:
        return DiseaseFlag(False, "empty crop", 0.0)

    h, w = bgr_crop.shape[:2]
    if h * w < min_crop_pixels:
        return DiseaseFlag(False, "crop too small to assess", 0.0)

    hsv = cv2.cvtColor(bgr_crop, cv2.COLOR_BGR2HSV)
    h_ch, s_ch, v_ch = cv2.split(hsv)
    total_px = h * w

    # Dark/necrotic-looking pixels: low value, low-to-mid saturation.
    dark_mask = (v_ch < 70) & (s_ch < 120)
    dark_ratio = float(np.count_nonzero(dark_mask)) / total_px

    # Pale/desaturated patches: high value, low saturation (grayish-tan,
    # unlike the saturated red/orange/green of healthy tomato skin).
    pale_mask = (v_ch > 150) & (s_ch < 60)
    pale_ratio = float(np.count_nonzero(pale_mask)) / total_px

    if dark_ratio >= dark_spot_ratio_thresh:
        confidence = min(1.0, dark_ratio / (dark_spot_ratio_thresh * 3))
        return DiseaseFlag(True, "dark/necrotic patch detected", round(confidence, 2))

    if pale_ratio >= pale_patch_ratio_thresh:
        confidence = min(1.0, pale_ratio / (pale_patch_ratio_thresh * 3))
        return DiseaseFlag(True, "pale/scald-like patch detected", round(confidence, 2))

    return DiseaseFlag(False, "no anomaly detected", 0.0)


def crop_from_frame(frame: np.ndarray, xyxy) -> Optional[np.ndarray]:
    """Safely slices a bounding box crop out of a full frame, clamping to
    frame bounds (tracker boxes can occasionally extend slightly past the
    frame edge)."""
    x1, y1, x2, y2 = map(int, xyxy)
    h, w = frame.shape[:2]
    x1, x2 = max(0, x1), min(w, x2)
    y1, y2 = max(0, y1), min(h, y2)
    if x2 <= x1 or y2 <= y1:
        return None
    return frame[y1:y2, x1:x2]


# ---------------------------------------------------------------------------
# Path to a real (trained) disease class, when you have time/data for it:
#
# 1. Gather diseased-tomato images (PlantVillage tomato subset, or your own
#    photos of blighted/rotten fruit).
# 2. Label them in the same COCO/YOLO format as the notebook already uses,
#    adding a 4th class id for "diseased".
# 3. In Tomoto.ipynb, add 'diseased' to `final_classes` and retrain with
#    `nc: 4` in data.yaml -- everything else in the training cell is
#    unchanged.
# 4. Update harvest_config.json's "class_names" and "box_colors_bgr" to
#    include "diseased". Once that's done, this heuristic module becomes
#    optional -- you can keep it running alongside the trained class as a
#    second opinion, or drop it.
# ---------------------------------------------------------------------------
