"""Phone camera tomato detection web app.

This app uses the browser camera permission on the device that opens it. When
hosted on an HTTPS service, a phone can capture a tomato image directly and
receive ripeness detections without a laptop running the camera.

Run locally:
    python -m streamlit run mobile_app.py
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import cv2
import numpy as np
import pandas as pd
import streamlit as st
from ultralytics import YOLO

from disease_detector import analyze_crop, crop_from_frame

PROJECT_DIR = Path(__file__).resolve().parent
CONFIG_PATH = PROJECT_DIR / "harvest_config.json"


@st.cache_data
def load_config() -> dict:
    with CONFIG_PATH.open(encoding="utf-8") as config_file:
        return json.load(config_file)


@st.cache_resource(show_spinner="Loading the tomato detection model…")
def load_model(model_path: str) -> YOLO:
    return YOLO(model_path)


def estimate_harvest_date(ripeness: str, config: dict) -> str:
    """Return an offline, transparent estimate for one photo result.

    Live weather/GDD forecasting remains available in the desktop tracker.
    A browser photo should return quickly and reliably, so it uses the
    configured fallback table rather than making a network request per scan.
    """
    days = config.get("harvest_days", {}).get(ripeness)
    if days is None:
        return "Unknown"
    return (datetime.now() + timedelta(days=days)).date().isoformat()


def annotate_and_detect(image_bytes: bytes, config: dict) -> tuple[np.ndarray, list[dict]]:
    image_array = np.frombuffer(image_bytes, dtype=np.uint8)
    frame = cv2.imdecode(image_array, cv2.IMREAD_COLOR)
    if frame is None:
        raise ValueError("The captured image could not be read. Please take another photo.")

    model = load_model(str(PROJECT_DIR / config["model_path"]))
    result = model.predict(
        source=frame,
        conf=float(config.get("confidence_threshold", 0.25)),
        verbose=False,
    )[0]

    class_names = config.get("class_names", [])
    colors = {name: tuple(value) for name, value in config.get("box_colors_bgr", {}).items()}
    disease_config = config.get("disease_screening", {})
    detections: list[dict] = []

    if result.boxes is not None:
        for index, box in enumerate(result.boxes):
            xyxy = box.xyxy[0].cpu().numpy()
            class_index = int(box.cls[0].item())
            ripeness = class_names[class_index] if class_index < len(class_names) else str(class_index)
            confidence = float(box.conf[0].item())
            x1, y1, x2, y2 = map(int, xyxy)

            flag_reason = "Not assessed"
            suspect = False
            if disease_config.get("enabled", False):
                crop = crop_from_frame(frame, xyxy)
                flag = analyze_crop(
                    crop,
                    dark_spot_ratio_thresh=disease_config.get("dark_spot_ratio_thresh", 0.08),
                    pale_patch_ratio_thresh=disease_config.get("pale_patch_ratio_thresh", 0.15),
                )
                suspect = flag.is_suspect
                flag_reason = flag.reason

            color = (0, 140, 255) if suspect else colors.get(ripeness, (255, 255, 255))
            label = f"{ripeness.replace('_', ' ')} {confidence:.0%}"
            if suspect:
                label += " | inspect"
            cv2.rectangle(frame, (x1, y1), (x2, y2), color, 3)
            cv2.putText(
                frame, label, (x1, max(24, y1 - 9)), cv2.FONT_HERSHEY_SIMPLEX,
                0.6, color, 2, cv2.LINE_AA,
            )
            detections.append({
                "Tomato": index + 1,
                "Ripeness": ripeness.replace("_", " ").title(),
                "Confidence": round(confidence * 100, 1),
                "Estimated harvest": estimate_harvest_date(ripeness, config),
                "Inspection needed": "Yes" if suspect else "No",
                "Screening note": flag_reason,
            })

    return cv2.cvtColor(frame, cv2.COLOR_BGR2RGB), detections


st.set_page_config(page_title="Tomato scanner", page_icon="🍅", layout="centered")
st.title("Tomato scanner")
st.caption("Use your phone camera to check tomato ripeness in one photo.")
st.info("Allow camera access, take a clear photo of the fruit, then select Analyze photo.", icon=":material/photo_camera:")

config = load_config()
st.session_state.setdefault("last_result", None)

with st.form("camera_scan", border=False):
    photo = st.camera_input("Take a tomato photo", key="tomato_camera")
    submitted = st.form_submit_button("Analyze photo", type="primary", icon=":material/search:", width="stretch")

if submitted:
    if photo is None:
        st.warning("Take a photo before running detection.")
    else:
        result_slot = st.container()
        with result_slot.skeleton():
            try:
                annotated_image, detections = annotate_and_detect(photo.getvalue(), config)
                st.session_state.last_result = (annotated_image, detections)
            except Exception as error:
                st.session_state.last_result = None
                st.error(f"Detection could not run: {error}")

if st.session_state.last_result is not None:
    annotated_image, detections = st.session_state.last_result
    st.image(annotated_image, caption="Detection result", width="stretch")

    if detections:
        table = pd.DataFrame(detections)
        total = len(table)
        ripe = int((table["Ripeness"] == "Fully Ripened").sum())
        inspect = int((table["Inspection needed"] == "Yes").sum())
        with st.container(horizontal=True):
            st.metric("Tomatoes detected", total, border=True)
            st.metric("Ready to harvest", ripe, border=True)
            st.metric("Needs inspection", inspect, border=True)
        st.subheader("Scan results")
        st.dataframe(table, hide_index=True, width="stretch")
        st.caption("Inspection notes are a visual screening heuristic, not a disease diagnosis.")
    else:
        st.info("No tomatoes were detected. Move closer, improve lighting, and try again.")

with st.expander("How to get the best result"):
    st.markdown("""
- Photograph one or a few tomatoes in bright, even light.
- Keep the tomato large and in focus. Avoid heavy shadows and glare.
- This version analyzes a captured photo. It avoids continuous phone video processing so it is reliable on standard web hosting.
- The app detects **green**, **half ripened**, and **fully ripened** tomatoes. Inspection flags are prompts for a human check, not medical or crop-disease diagnoses.
""")
