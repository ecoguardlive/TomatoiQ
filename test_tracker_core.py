import csv
import json
from dataclasses import dataclass

from tracker_core import TomatoTracker, TrackState, build_live_state, save_report, write_live_state


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


# ---------------------------------------------------------------------------
# Track lifecycle (new -> confirmed -> lost -> exited)
# ---------------------------------------------------------------------------

def test_single_observation_confirms_immediately_by_default():
    """Default behaviour (confirm_after_observations=1) must match the old,
    pre-lifecycle behaviour: a single sighting counts, since a one-shot
    source (an uploaded photo) never gets a second frame to confirm from."""
    tracker = TomatoTracker()
    tracker.update(1, "green", 0)
    assert tracker.states[1] == TrackState.CONFIRMED
    assert tracker.counts_by_class()["green"] == 1


def test_unconfirmed_noise_is_not_counted_as_a_tomato():
    tracker = TomatoTracker(confirm_after_observations=3)
    tracker.update(1, "green", 0)  # only seen once -- a glare/leaf-edge blip
    tracker.finalize_frame(0, seen_track_ids=[1])
    assert tracker.states[1] == TrackState.NEW
    assert tracker.counts_by_class() == {}
    assert 1 not in tracker.confirmed_track_ids()


def test_track_confirms_after_enough_observations():
    tracker = TomatoTracker(confirm_after_observations=3)
    for frame_idx in range(3):
        tracker.update(1, "green", frame_idx)
    assert tracker.states[1] == TrackState.CONFIRMED
    assert tracker.counts_by_class()["green"] == 1


def test_missing_track_becomes_lost_then_exited_not_counted_forever():
    """This is the bug the lifecycle fixes: the old code counted every ID
    ever seen, forever, even after the tomato left frame."""
    tracker = TomatoTracker(confirm_after_observations=1,
                             lost_after_missed_frames=2,
                             exit_after_missed_frames=4)
    tracker.update(1, "green", 0)
    assert tracker.currently_visible_track_ids() == [1]

    # missed one frame -- not lost yet
    tracker.finalize_frame(1, seen_track_ids=[])
    assert tracker.states[1] == TrackState.CONFIRMED
    # missed a second frame -- now lost, but still counted in the session total
    tracker.finalize_frame(2, seen_track_ids=[])
    assert tracker.states[1] == TrackState.LOST
    assert tracker.counts_by_class()["green"] == 1
    assert tracker.currently_visible_track_ids() == []

    # keeps missing frames until it exceeds exit_after_missed_frames
    tracker.finalize_frame(3, seen_track_ids=[])
    tracker.finalize_frame(4, seen_track_ids=[])
    assert tracker.states[1] == TrackState.EXITED
    # still part of the historical/session total...
    assert tracker.counts_by_class()["green"] == 1
    assert 1 in tracker.confirmed_track_ids()
    # ...but not "currently in frame"
    assert tracker.currently_visible_track_ids() == []


def test_lost_track_recovers_to_confirmed_on_reappearance():
    tracker = TomatoTracker(confirm_after_observations=1, lost_after_missed_frames=1,
                             exit_after_missed_frames=10)
    tracker.update(1, "green", 0)
    tracker.finalize_frame(1, seen_track_ids=[])
    assert tracker.states[1] == TrackState.LOST

    tracker.update(1, "green", 2)  # ByteTrack re-acquired the same ID
    assert tracker.states[1] == TrackState.CONFIRMED
    assert tracker.missed_frames[1] == 0


def test_from_config_reads_tracking_section():
    config = {"tracking": {"confirm_after_observations": 5,
                            "lost_after_missed_frames": 7,
                            "exit_after_missed_frames": 30}}
    tracker = TomatoTracker.from_config(config)
    assert tracker.confirm_after_observations == 5
    assert tracker.lost_after_missed_frames == 7
    assert tracker.exit_after_missed_frames == 30


def test_from_config_falls_back_to_defaults_when_section_missing():
    tracker = TomatoTracker.from_config({})
    assert tracker.confirm_after_observations == 1


def test_disease_suspects_only_reported_for_confirmed_tracks():
    tracker = TomatoTracker(confirm_after_observations=3)
    tracker.update(1, "green", 0)  # single sighting -- never confirms
    tracker.update_disease_flag(1, FakeFlag(True, "dark patch", 0.9))
    tracker.finalize_frame(0, seen_track_ids=[1])
    assert tracker.disease_suspect_ids() == []


def test_build_live_state_reports_current_vs_historical_counts():
    tracker = TomatoTracker(confirm_after_observations=1, lost_after_missed_frames=1,
                             exit_after_missed_frames=5)
    tracker.update(1, "green", 0)
    tracker.update(2, "fully_ripened", 0)
    tracker.finalize_frame(1, seen_track_ids=[2])  # id 1 goes missing, id 2 stays visible

    state = build_live_state(tracker, CONFIG["class_names"], CONFIG, {})
    assert state["total_tomatoes"] == 2          # historical/session total: both still counted
    assert state["currently_in_frame_count"] == 1  # only id 2 is actually visible right now
    assert state["lifecycle"]["lost"] == 1
    assert state["lifecycle"]["confirmed"] == 1
    ids_in_frame = {t["tomato_id"]: t["currently_in_frame"] for t in state["tomatoes"]}
    assert ids_in_frame[1] is False
    assert ids_in_frame[2] is True
