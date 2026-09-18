"""
Tomato Harvest Dashboard
-------------------------
A lightweight Streamlit dashboard for the harvest system. Run this
ALONGSIDE tomato_harvest_system.py (in a second terminal) -- the main
script writes live_state.json every few frames, and this dashboard polls
that file and redraws. It never touches the camera or the model directly,
so it stays fast and simple, and you can even open it on a phone on the
same network while the detector runs on your laptop.

Usage:
    Terminal 1: python tomato_harvest_system.py --source 0
    Terminal 2: streamlit run dashboard.py

If you don't have a camera running yet and just want to see the dashboard
render, point it at the sample state file:
    streamlit run dashboard.py -- --state sample_live_state.json
"""

import argparse
import json
import time
from pathlib import Path

import pandas as pd
import streamlit as st


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--state", default="live_state.json",
                         help="Path to the live_state.json file written by tomato_harvest_system.py")
    parser.add_argument("--refresh-seconds", type=float, default=2.0,
                         help="How often to re-read the state file")
    # Streamlit passes its own args through; parse_known_args avoids clashing with them.
    args, _ = parser.parse_known_args()
    return args


def load_state(path):
    p = Path(path)
    if not p.exists():
        return None
    try:
        with open(p, "r") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        # The main script may be mid-write; just skip this refresh cycle
        # rather than crashing the dashboard.
        return None


def render(state, class_names_order=("green", "half_ripened", "fully_ripened"), is_demo=False):
    st.title("🍅 Tomato Harvest Dashboard")

    if is_demo:
        st.warning(
            "⚠ DEMO MODE — showing sample_live_state.json, not a live detector. "
            "This banner is not cosmetic: it is the only thing distinguishing "
            "sample data from a real scan in this view.",
            icon="⚠️",
        )

    if state is None:
        st.warning(
            "No live state file found yet. Start `tomato_harvest_system.py` "
            "in another terminal, or point this dashboard at a sample file "
            "with `--state sample_live_state.json`."
        )
        return

    st.caption(f"Last updated: {state.get('updated_at', 'unknown')}  |  "
               f"Harvest-date method: **{state.get('estimation_method', 'unknown').upper()}**")

    col1, col2, col3 = st.columns(3)
    col1.metric("Total tomatoes tracked", state.get("total_tomatoes", 0))
    col2.metric("Ready to harvest now", state.get("ready_now", 0))
    col3.metric("Flagged for inspection", state.get("disease_suspect_count", 0))

    counts = state.get("counts_by_class", {})
    if counts:
        st.subheader("Ripeness distribution")
        df_counts = pd.DataFrame(
            {"ripeness": list(counts.keys()), "count": list(counts.values())}
        )
        st.bar_chart(df_counts.set_index("ripeness"))

    tomatoes = state.get("tomatoes", [])
    if tomatoes:
        df = pd.DataFrame(tomatoes)

        st.subheader("Ready to harvest")
        ready_df = df[df["ready_now"]] if "ready_now" in df else df.iloc[0:0]
        if len(ready_df):
            st.dataframe(
                ready_df[["tomato_id", "ripeness_class", "estimated_harvest_date"]],
                width='stretch', hide_index=True,
            )
        else:
            st.caption("None yet.")

        st.subheader("Flagged for inspection")
        flagged_df = df[df["disease_suspect"]] if "disease_suspect" in df else df.iloc[0:0]
        if len(flagged_df):
            st.dataframe(
                flagged_df[["tomato_id", "ripeness_class", "disease_reason", "disease_confidence"]],
                width='stretch', hide_index=True,
            )
            st.caption(
                "Heuristic screen, not a diagnosis -- these are worth a human look, "
                "not automatically diseased. See disease_detector.py for details."
            )
        else:
            st.caption("None flagged.")

        with st.expander("Full per-tomato table"):
            st.dataframe(df, width='stretch', hide_index=True)
    else:
        st.info("No tomatoes tracked yet.")


def main():
    args = parse_args()
    st.set_page_config(page_title="Tomato Harvest Dashboard", page_icon="🍅", layout="wide")

    # Hide Streamlit's default chrome (hamburger menu, "Deploy" button, footer)
    # for a cleaner full-screen look when presenting to judges. Purely
    # cosmetic -- doesn't affect any functionality.
    st.markdown(
        """
        <style>
        #MainMenu {visibility: hidden;}
        header {visibility: hidden;}
        footer {visibility: hidden;}
        div[data-testid="stToolbar"] {visibility: hidden;}
        </style>
        """,
        unsafe_allow_html=True,
    )

    placeholder = st.empty()
    is_demo = Path(args.state).name == "sample_live_state.json"
    while True:
        state = load_state(args.state)
        with placeholder.container():
            render(state, is_demo=is_demo)
        time.sleep(args.refresh_seconds)
        st.rerun()


if __name__ == "__main__":
    main()
