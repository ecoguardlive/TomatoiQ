"""Pure tracking/estimation logic, kept free of cv2/ultralytics imports.

Split out of tomato_harvest_system.py so the actual decision logic --
majority-vote ripeness per track, harvest-date dispatch, live-state/report
shaping -- can be unit tested without a camera, a model file, or OpenCV
installed. tomato_harvest_system.py imports everything it needs from here
and adds the camera loop, drawing, and YOLO/ByteTrack calls on top.
"""

from __future__ import annotations

import csv
import json
import os
from collections import defaultdict, Counter
from datetime import datetime, timedelta

from growing_degree_days import estimate_harvest_date_gdd


def load_config(config_path):
    with open(config_path, "r") as f:
        return json.load(f)


def estimate_harvest_date_flat(ripeness_class, harvest_days_map, from_date=None):
    """Legacy flat lookup: fixed days from now, used as a fallback when GDD
    is disabled or the weather lookup fails."""
    from_date = from_date or datetime.now()
    days = harvest_days_map.get(ripeness_class, None)
    if days is None:
        return None
    return from_date + timedelta(days=days)


def estimate_harvest_date(ripeness_class, config, from_date=None):
    """Dispatches to the GDD model if enabled, else the flat table.
    Always returns a datetime or None (unknown class), same contract either
    way, so callers don't need to care which method is active."""
    gdd_cfg = config.get("gdd", {})
    if gdd_cfg.get("enabled"):
        return estimate_harvest_date_gdd(
            ripeness_class,
            gdd_cfg["gdd_required"],
            base_temp_c=gdd_cfg["base_temp_c"],
            from_date=from_date,
            lat=gdd_cfg.get("latitude"),
            lon=gdd_cfg.get("longitude"),
            assumed_avg_temp_c=gdd_cfg.get("assumed_avg_temp_c", 27.0),
            cap_c=gdd_cfg.get("cap_c"),
        )
    return estimate_harvest_date_flat(ripeness_class, config["harvest_days"], from_date)


def cached_estimate_harvest_date(ripeness_class, config, cache):
    """Same contract as estimate_harvest_date, memoized per ripeness class
    for the life of the cache dict passed in (avoids one weather API call
    per tracked tomato per frame -- see docstring history in the original
    module for why this matters for real-time performance)."""
    if ripeness_class in cache:
        return cache[ripeness_class]
    result = estimate_harvest_date(ripeness_class, config)
    cache[ripeness_class] = result
    return result


class TrackState:
    """Lifecycle a ByteTrack ID moves through. A raw tracker ID is not, by
    itself, a real tomato: a single spurious detection (glare, a leaf edge)
    gets an ID too. This state machine is what turns "an ID ByteTrack
    handed us" into "a physical tomato we're confident exists":

        NEW ---(seen >= confirm_after_observations times)---> CONFIRMED
        CONFIRMED ---(missed lost_after_missed_frames frames)---> LOST
        LOST ---(seen again)---> CONFIRMED
        LOST ---(missed exit_after_missed_frames frames total)---> EXITED

    Without this, the previous implementation treated "every ID ByteTrack
    has ever handed out" as a permanent tomato: counts only ever grew, a
    one-frame glare blip counted the same as a real fruit, and a tomato
    that left frame and came back under a new ByteTrack ID silently became
    two tomatoes instead of one re-acquired one.
    """
    NEW = "new"
    CONFIRMED = "confirmed"
    LOST = "lost"
    EXITED = "exited"


class TomatoTracker:
    """Keeps per-track-ID history of observed ripeness classes so we can
    assign each physical tomato a single, stable 'majority class' instead of
    flickering between classes frame to frame. Also keeps a running record
    of the worst (most confident) disease flag seen for each track, and the
    lifecycle state (see TrackState) used to decide which track IDs
    represent real, confirmed tomatoes.
    """

    def __init__(self, confirm_after_observations=1, lost_after_missed_frames=20,
                 exit_after_missed_frames=90):
        """confirm_after_observations=1 (the default) preserves the original
        behaviour of counting a tomato from its first sighting -- correct
        for single-shot sources like an uploaded photo, where there is only
        ever one 'frame' to judge from. A live camera feed with many frames
        available can raise this (see harvest_config.json's "tracking"
        section) to filter out single-frame noise before it's ever counted.
        """
        self.track_classes = defaultdict(Counter)   # track_id -> Counter({class_name: count})
        self.first_seen_frame = {}                  # track_id -> frame index
        self.last_seen_frame = {}                    # track_id -> frame index
        self.disease_flags = {}                      # track_id -> DiseaseFlag (best seen)
        self.observation_counts = defaultdict(int)   # track_id -> number of times update() was called
        self.missed_frames = defaultdict(int)        # track_id -> consecutive frames since last seen
        self.states = {}                             # track_id -> TrackState

        self.confirm_after_observations = max(1, int(confirm_after_observations))
        self.lost_after_missed_frames = max(1, int(lost_after_missed_frames))
        self.exit_after_missed_frames = max(self.lost_after_missed_frames + 1, int(exit_after_missed_frames))

    @classmethod
    def from_config(cls, config):
        """Reads the optional "tracking" section of harvest_config.json.
        Falls back to the safe single-observation defaults above if the
        section (or the whole config) doesn't define it."""
        cfg = (config or {}).get("tracking", {})
        return cls(
            confirm_after_observations=cfg.get("confirm_after_observations", 1),
            lost_after_missed_frames=cfg.get("lost_after_missed_frames", 20),
            exit_after_missed_frames=cfg.get("exit_after_missed_frames", 90),
        )

    def update(self, track_id, class_name, frame_idx):
        """Call once per detected box, every frame it's seen in."""
        self.track_classes[track_id][class_name] += 1
        self.observation_counts[track_id] += 1
        self.missed_frames[track_id] = 0
        if track_id not in self.first_seen_frame:
            self.first_seen_frame[track_id] = frame_idx
            self.states[track_id] = TrackState.NEW
        self.last_seen_frame[track_id] = frame_idx

        if self.states[track_id] != TrackState.CONFIRMED:
            if self.observation_counts[track_id] >= self.confirm_after_observations:
                self.states[track_id] = TrackState.CONFIRMED

    def finalize_frame(self, frame_idx, seen_track_ids):
        """Call once per processed frame (even a frame with zero detections)
        with the set of track IDs actually seen in that frame. Advances the
        missed-frame counter for every other known, not-yet-exited track and
        transitions it to LOST or EXITED as thresholds are crossed. This is
        what makes tracks eventually stop being counted instead of
        persisting forever once ByteTrack has actually lost them."""
        seen = set(seen_track_ids)
        for tid, state in self.states.items():
            if tid in seen or state == TrackState.EXITED:
                continue
            self.missed_frames[tid] += 1
            if self.missed_frames[tid] >= self.exit_after_missed_frames:
                self.states[tid] = TrackState.EXITED
            elif self.missed_frames[tid] >= self.lost_after_missed_frames:
                self.states[tid] = TrackState.LOST

    def update_disease_flag(self, track_id, flag):
        current = self.disease_flags.get(track_id)
        if current is None or flag.confidence > current.confidence:
            self.disease_flags[track_id] = flag

    def majority_class(self, track_id):
        counter = self.track_classes.get(track_id)
        if not counter:
            return None
        return counter.most_common(1)[0][0]

    def all_track_ids(self):
        """Every ByteTrack ID ever observed, regardless of lifecycle state.
        Mainly useful for debugging/audits -- prefer confirmed_track_ids()
        or currently_visible_track_ids() for anything user-facing."""
        return list(self.track_classes.keys())

    def confirmed_track_ids(self):
        """Real, noise-filtered tomatoes tracked this session: every track
        that reached CONFIRMED at least once, whether or not it's still in
        frame right now. This is the session/historical total -- a tomato
        that the camera panned away from doesn't stop existing."""
        return [tid for tid, s in self.states.items() if s != TrackState.NEW]

    def currently_visible_track_ids(self):
        """Tracks actually seen in the most recently finalized frame --
        the 'right now' count, as opposed to the session-cumulative total
        from confirmed_track_ids()."""
        return [tid for tid in self.confirmed_track_ids() if self.missed_frames.get(tid, 0) == 0]

    def lifecycle_counts(self):
        counts = Counter(self.states.values())
        return {state: counts.get(state, 0) for state in
                (TrackState.NEW, TrackState.CONFIRMED, TrackState.LOST, TrackState.EXITED)}

    def counts_by_class(self):
        """Unique, confirmed tomato count per class, based on each track's
        majority class. Session-cumulative (see confirmed_track_ids)."""
        counts = Counter()
        for tid in self.confirmed_track_ids():
            counts[self.majority_class(tid)] += 1
        return counts

    def disease_suspect_ids(self):
        confirmed = set(self.confirmed_track_ids())
        return [tid for tid, flag in self.disease_flags.items() if flag.is_suspect and tid in confirmed]


def build_live_state(tracker: TomatoTracker, class_names, config, harvest_date_cache):
    """Shapes the current tracker state into the JSON structure pwa_server.py
    and dashboard.py read. Pure function (no file I/O) so it's testable."""
    counts = tracker.counts_by_class()
    confirmed = set(tracker.confirmed_track_ids())
    visible_now = set(tracker.currently_visible_track_ids())
    rows = []
    for tid in confirmed:
        cls = tracker.majority_class(tid)
        harvest_date = cached_estimate_harvest_date(cls, config, harvest_date_cache)
        flag = tracker.disease_flags.get(tid)
        rows.append({
            "tomato_id": int(tid),
            "ripeness_class": cls,
            "estimated_harvest_date": harvest_date.strftime("%Y-%m-%d") if harvest_date else "unknown",
            "ready_now": bool(harvest_date and harvest_date.date() <= datetime.now().date()),
            "disease_suspect": bool(flag and flag.is_suspect),
            "disease_reason": flag.reason if flag else None,
            "disease_confidence": flag.confidence if flag else 0.0,
            "currently_in_frame": tid in visible_now,
        })

    return {
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "total_tomatoes": sum(counts.values()),
        "counts_by_class": {c: counts.get(c, 0) for c in class_names},
        "ready_now": counts.get("fully_ripened", 0),
        "disease_suspect_count": len(tracker.disease_suspect_ids()),
        "tomatoes": rows,
        "estimation_method": "gdd" if config.get("gdd", {}).get("enabled") else "flat",
        # additive fields -- current vs. historical, and the raw lifecycle
        # breakdown that makes the distinction auditable rather than implicit.
        "currently_in_frame_count": len(visible_now),
        "lifecycle": tracker.lifecycle_counts(),
    }


def write_live_state(tracker, class_names, config, path, harvest_date_cache):
    """Atomically publish a snapshot so API readers never observe half JSON."""
    state = build_live_state(tracker, class_names, config, harvest_date_cache)
    target = os.fspath(path)
    temporary = f"{target}.tmp"
    with open(temporary, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(temporary, target)


def save_report(tracker, class_names, config, output_path, harvest_date_cache):
    rows = []
    for tid in tracker.confirmed_track_ids():
        cls = tracker.majority_class(tid)
        harvest_date = cached_estimate_harvest_date(cls, config, harvest_date_cache)
        flag = tracker.disease_flags.get(tid)
        rows.append({
            "tomato_id": tid,
            "ripeness_class": cls,
            "estimated_harvest_date": harvest_date.strftime("%Y-%m-%d") if harvest_date else "unknown",
            "frames_tracked": sum(tracker.track_classes[tid].values()),
            "disease_suspect": bool(flag and flag.is_suspect),
            "disease_reason": flag.reason if flag else "",
        })

    with open(output_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["tomato_id", "ripeness_class",
                                                "estimated_harvest_date", "frames_tracked",
                                                "disease_suspect", "disease_reason"])
        writer.writeheader()
        writer.writerows(rows)

    counts = tracker.counts_by_class()
    print("\n" + "=" * 50)
    print("HARVEST SUMMARY REPORT")
    print("=" * 50)
    total = sum(counts.values())
    print(f"Total unique tomatoes tracked: {total}")
    for cname in class_names:
        print(f"  {cname}: {counts.get(cname, 0)}")
    print(f"Flagged for inspection: {len(tracker.disease_suspect_ids())}")
    print(f"\nDetailed per-tomato report saved to: {output_path}")
    print("=" * 50)
