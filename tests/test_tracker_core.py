import csv
import json
from dataclasses import dataclass

from tracker_core import TomatoTracker, build_live_state, save_report, write_live_state


@dataclass
class FakeFlag:
    is_suspect: bool
    reason: str
    confidence: float


CONFIG = {
    "class_names": ["green", "half_ripened", "fully_ripened"],
    "harvest_days": {"green": 14, "half_ripened": 6, "fully_ripened": 0},
    "gdd": {"enabled": False},
}


def test_zero_detections_gives_honest_empty_state():
    tracker = TomatoTracker()
    state = build_live_state(tracker, CONFIG["class_names"], CONFIG, {})
    assert state["total_tomatoes"] == 0
    assert state["ready_now"] == 0
    assert state["disease_suspect_count"] == 0
    assert state["tomatoes"] == []
    # every class must still be represented at 0, not omitted
    assert state["counts_by_class"] == {"green": 0, "half_ripened": 0, "fully_ripened": 0}


def test_same_track_id_across_frames_keeps_one_identity():
    tracker = TomatoTracker()
    for frame_idx in range(5):
        tracker.update(track_id=1, class_name="green", frame_idx=frame_idx)
    assert tracker.all_track_ids() == [1]
    assert tracker.counts_by_class()["green"] == 1  # not 5


def test_majority_vote_wins_over_flicker():
    tracker = TomatoTracker()
    tracker.update(1, "green", 0)
    tracker.update(1, "half_ripened", 1)
    tracker.update(1, "half_ripened", 2)
    tracker.update(1, "half_ripened", 3)
    assert tracker.majority_class(1) == "half_ripened"


def test_disease_flag_keeps_highest_confidence_seen():
    tracker = TomatoTracker()
    tracker.update_disease_flag(1, FakeFlag(True, "pale patch", 0.3))
    tracker.update_disease_flag(1, FakeFlag(True, "dark patch", 0.9))
    tracker.update_disease_flag(1, FakeFlag(False, "no anomaly", 0.0))
    assert tracker.disease_flags[1].confidence == 0.9
    assert tracker.disease_flags[1].reason == "dark patch"


def test_multiple_tomatoes_counted_independently():
    tracker = TomatoTracker()
    tracker.update(1, "green", 0)
    tracker.update(2, "fully_ripened", 0)
    tracker.update(3, "fully_ripened", 0)
    counts = tracker.counts_by_class()
    assert counts["green"] == 1
    assert counts["fully_ripened"] == 2
    state = build_live_state(tracker, CONFIG["class_names"], CONFIG, {})
    assert state["total_tomatoes"] == 3
    assert state["ready_now"] == 2  # fully_ripened -> 0 harvest days -> ready today


def test_write_live_state_and_save_report_round_trip(tmp_path):
    tracker = TomatoTracker()
    tracker.update(1, "fully_ripened", 0)
    tracker.update_disease_flag(1, FakeFlag(True, "dark/necrotic patch detected", 0.7))

    state_path = tmp_path / "live_state.json"
    write_live_state(tracker, CONFIG["class_names"], CONFIG, str(state_path), {})
    saved = json.loads(state_path.read_text())
    assert saved["tomatoes"][0]["tomato_id"] == 1
    assert saved["tomatoes"][0]["disease_suspect"] is True
    assert saved["disease_suspect_count"] == 1

    report_path = tmp_path / "report.csv"
    save_report(tracker, CONFIG["class_names"], CONFIG, str(report_path), {})
    with open(report_path) as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 1
    assert rows[0]["tomato_id"] == "1"
    assert rows[0]["disease_suspect"] == "True"
