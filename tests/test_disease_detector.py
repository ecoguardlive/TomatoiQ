import numpy as np

from disease_detector import analyze_crop, crop_from_frame


def _solid_bgr(height, width, bgr):
    frame = np.zeros((height, width, 3), dtype=np.uint8)
    frame[:, :] = bgr
    return frame


def test_empty_crop_is_not_flagged():
    flag = analyze_crop(None)
    assert flag.is_suspect is False
    assert flag.confidence == 0.0


def test_too_small_crop_is_not_assessed():
    tiny = _solid_bgr(5, 5, (0, 0, 255))  # 25 px < min_crop_pixels default (400)
    flag = analyze_crop(tiny)
    assert flag.is_suspect is False
    assert "too small" in flag.reason


def test_healthy_saturated_red_is_not_flagged():
    healthy = _solid_bgr(40, 40, (30, 30, 210))  # saturated red, no dark/pale patches
    flag = analyze_crop(healthy)
    assert flag.is_suspect is False


def test_dark_patch_triggers_suspect_flag():
    # Near-black, low-saturation -> should hit the dark_mask branch
    dark = _solid_bgr(40, 40, (10, 10, 10))
    flag = analyze_crop(dark, dark_spot_ratio_thresh=0.08)
    assert flag.is_suspect is True
    assert "dark" in flag.reason
    assert 0.0 < flag.confidence <= 1.0


def test_pale_patch_triggers_suspect_flag():
    # Bright, low-saturation gray -> should hit the pale_mask branch
    pale = _solid_bgr(40, 40, (200, 200, 200))
    flag = analyze_crop(pale, pale_patch_ratio_thresh=0.15)
    assert flag.is_suspect is True
    assert "pale" in flag.reason


def test_confidence_is_bounded_at_one():
    fully_dark = _solid_bgr(40, 40, (0, 0, 0))
    flag = analyze_crop(fully_dark, dark_spot_ratio_thresh=0.08)
    assert flag.confidence <= 1.0


def test_crop_from_frame_clamps_to_bounds():
    frame = np.zeros((100, 100, 3), dtype=np.uint8)
    # box extends past the frame edge on both sides
    crop = crop_from_frame(frame, (-10, -10, 200, 200))
    assert crop is not None
    assert crop.shape[0] == 100 and crop.shape[1] == 100


def test_crop_from_frame_returns_none_for_degenerate_box():
    frame = np.zeros((100, 100, 3), dtype=np.uint8)
    assert crop_from_frame(frame, (50, 50, 40, 40)) is None
