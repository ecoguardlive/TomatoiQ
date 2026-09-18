"""
Tomato Harvest Readiness System
--------------------------------
Runs on a live webcam or a video file. Detects tomatoes, classifies ripeness
(green / half_ripened / fully_ripened), tracks each individual tomato across
frames (so it's counted once, not once per frame), estimates a harvest date
per tomato using a growing-degree-day model (or a flat fallback), flags
tomatoes worth a human look for possible disease/damage, and writes a live
state file the Streamlit dashboard (dashboard.py) polls.

Usage:
    Live webcam:
        python tomato_harvest_system.py --source 0

    Video file:
        python tomato_harvest_system.py --source path/to/video.mp4

    Video file, no live window (just process + save report/annotated video):
        python tomato_harvest_system.py --source video.mp4 --headless --save-video output.mp4

Requires: ultralytics, opencv-python, requests  (see requirements.txt)

Note: the tracking/harvest-date/report logic itself lives in tracker_core.py
so it can be unit tested without a camera, a model file, or OpenCV/YOLO
installed. This module wires that logic to the actual camera/YOLO/ByteTrack
pipeline and drawing.
"""

import argparse
from datetime import datetime

import cv2
from ultralytics import YOLO

from disease_detector import analyze_crop, crop_from_frame
from tracker_core import (
    TomatoTracker,
    cached_estimate_harvest_date,
    load_config,
    save_report,
    write_live_state,
)


def draw_hud(frame, counts, class_names, disease_suspect_count):
    """Draws a running totals panel in the top-left corner."""
    x, y = 15, 30
    line_height = 28
    total = sum(counts.values())

    overlay = frame.copy()
    panel_h = line_height * (len(class_names) + 3) + 10
    cv2.rectangle(overlay, (5, 5), (320, panel_h), (0, 0, 0), -1)
    frame[:] = cv2.addWeighted(overlay, 0.55, frame, 0.45, 0)

    cv2.putText(frame, f"Total tomatoes: {total}", (x, y),
                cv2.FONT_HERSHEY_SIMPLEX, 0.65, (255, 255, 255), 2)
    y += line_height
    for cname in class_names:
        c = counts.get(cname, 0)
        cv2.putText(frame, f"{cname}: {c}", (x, y),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 2)
        y += line_height

    ready = counts.get("fully_ripened", 0)
    cv2.putText(frame, f"Ready to harvest NOW: {ready}", (x, y),
                cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 255), 2)
    y += line_height
    cv2.putText(frame, f"Flagged for inspection: {disease_suspect_count}", (x, y),
                cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 120, 255), 2)
    return frame


def draw_box(frame, xyxy, track_id, class_name, harvest_date, color, disease_flag):
    x1, y1, x2, y2 = map(int, xyxy)
    box_color = (0, 140, 255) if (disease_flag and disease_flag.is_suspect) else color
    cv2.rectangle(frame, (x1, y1), (x2, y2), box_color, 2)

    label = f"#{track_id} {class_name}"
    if harvest_date is not None:
        if harvest_date.date() <= datetime.now().date():
            label += "  READY"
        else:
            days_left = (harvest_date.date() - datetime.now().date()).days
            label += f"  ~{days_left}d"
    if disease_flag and disease_flag.is_suspect:
        label += "  [INSPECT]"

    (tw, th), _ = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, 0.5, 1)
    cv2.rectangle(frame, (x1, y1 - th - 8), (x1 + tw + 6, y1), box_color, -1)
    cv2.putText(frame, label, (x1 + 3, y1 - 5),
                cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1)
    return frame


def main():
    parser = argparse.ArgumentParser(description="Tomato Harvest Readiness System")
    parser.add_argument("--source", default="0",
                         help="Webcam index (e.g. 0) or path to a video file")
    parser.add_argument("--config", default="harvest_config.json",
                         help="Path to config JSON file")
    parser.add_argument("--headless", action="store_true",
                         help="Don't open a live display window (useful for batch video processing)")
    parser.add_argument("--save-video", default=None,
                         help="Optional path to save the annotated output video")
    args = parser.parse_args()

    config = load_config(args.config)
    class_names = config["class_names"]
    conf_thresh = config["confidence_threshold"]
    colors = {k: tuple(v) for k, v in config["box_colors_bgr"].items()}
    disease_cfg = config.get("disease_screening", {"enabled": False})
    live_state_path = config.get("live_state_path", "live_state.json")
    live_state_every = config.get("live_state_update_every_n_frames", 10)

    # Webcam index vs video file path
    source = int(args.source) if args.source.isdigit() else args.source

    model = YOLO(config["model_path"])
    tracker = TomatoTracker.from_config(config)
    harvest_date_cache = {}  # ripeness_class -> datetime, computed once per run (see cached_estimate_harvest_date)

    writer = None
    frame_idx = 0

    print(f"Starting detection on source: {source}")
    print(f"Harvest-date method: {'GDD (weather-based)' if config.get('gdd', {}).get('enabled') else 'flat lookup table'}")
    print(f"Disease screening: {'on' if disease_cfg.get('enabled') else 'off'}")
    print("Press 'q' in the video window to stop (if not headless).")

    try:
        results_stream = model.track(
            source=source,
            conf=conf_thresh,
            tracker=config["tracker"],
            persist=True,
            stream=True,
            verbose=False,
        )
    except Exception as exc:
        print(f"[error] Could not open source '{source}': {exc}")
        print("[error] Camera/video unavailable -- no state was fabricated. "
              "Check the camera index/permissions or the video file path.")
        return

    stream_iter = iter(results_stream)
    while True:
        try:
            result = next(stream_iter)
        except StopIteration:
            break
        except Exception as exc:
            # Camera disconnected mid-run (unplugged, permission revoked, driver
            # error, etc). Persist whatever real state we have so far and stop
            # cleanly instead of crashing or silently freezing on stale frames.
            print(f"[error] Camera/stream read failed: {exc}")
            print("[error] Stopping detection. Last known state has been saved; "
                  "no synthetic frames were generated.")
            break

        frame = result.orig_img.copy()

        boxes = result.boxes
        seen_track_ids = []
        if boxes is not None and boxes.id is not None:
            for box, track_id, cls_idx in zip(boxes.xyxy.cpu().numpy(),
                                               boxes.id.cpu().numpy().astype(int),
                                               boxes.cls.cpu().numpy().astype(int)):
                seen_track_ids.append(track_id)
                class_name = class_names[cls_idx]
                tracker.update(track_id, class_name, frame_idx)

                if disease_cfg.get("enabled"):
                    crop = crop_from_frame(frame, box)
                    if crop is not None:
                        flag = analyze_crop(
                            crop,
                            dark_spot_ratio_thresh=disease_cfg.get("dark_spot_ratio_thresh", 0.08),
                            pale_patch_ratio_thresh=disease_cfg.get("pale_patch_ratio_thresh", 0.15),
                        )
                        tracker.update_disease_flag(track_id, flag)

                majority_cls = tracker.majority_class(track_id)
                harvest_date = cached_estimate_harvest_date(majority_cls, config, harvest_date_cache)
                color = colors.get(majority_cls, (255, 255, 255))
                disease_flag = tracker.disease_flags.get(track_id)
                frame = draw_box(frame, box, track_id, majority_cls, harvest_date, color, disease_flag)

        # Advance lost/exited timers for every track NOT seen this frame --
        # must run every frame, including frames with zero detections,
        # otherwise a tomato that leaves frame is counted forever (see
        # TrackState in tracker_core.py).
        tracker.finalize_frame(frame_idx, seen_track_ids)

        counts = tracker.counts_by_class()
        frame = draw_hud(frame, counts, class_names, len(tracker.disease_suspect_ids()))

        if frame_idx % max(1, live_state_every) == 0:
            try:
                write_live_state(tracker, class_names, config, live_state_path, harvest_date_cache)
            except Exception as e:
                print(f"[warn] could not write live state: {e}")

        if writer is None and args.save_video:
            h, w = frame.shape[:2]
            fourcc = cv2.VideoWriter_fourcc(*"mp4v")
            writer = cv2.VideoWriter(args.save_video, fourcc, 20.0, (w, h))
        if writer is not None:
            writer.write(frame)

        if not args.headless:
            cv2.imshow("Tomato Harvest Detection", frame)
            if cv2.waitKey(1) & 0xFF == ord("q"):
                break

        frame_idx += 1

    if writer is not None:
        writer.release()
    if not args.headless:
        cv2.destroyAllWindows()

    write_live_state(tracker, class_names, config, live_state_path, harvest_date_cache)
    save_report(tracker, class_names, config, config["output_report_path"], harvest_date_cache)


if __name__ == "__main__":
    main()
