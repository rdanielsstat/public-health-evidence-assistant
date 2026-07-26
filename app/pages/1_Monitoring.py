# app/pages/1_Monitoring.py
"""Monitoring dashboard.
 
Reads the query_log and feedback tables the main app writes, and renders
aggregate charts: usage over time, mode distribution, citation validity by
mode (the headline — grounded modes ~1.0, no_retrieval ~0.0), feedback split,
latency by mode, and token cost. All data is produced by real app usage; no
synthetic rows.
"""
 
from __future__ import annotations
 
import sys
from pathlib import Path
 
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
 
import pandas as pd
import streamlit as st
 
from monitoring.store import load_feedback, load_query_log
 
st.set_page_config(page_title="Monitoring", page_icon="📊", layout="wide")
 
st.title("Monitoring dashboard")
st.caption("Live usage, retrieval quality, and feedback from logged queries.")
 
log = load_query_log()
fb = load_feedback()
 
if log.empty:
    st.info(
        "No queries logged yet. Ask questions on the main page, then return "
        "here — the charts populate from real usage."
    )
    st.stop()
 
# Ensure datetime dtype for time-based grouping.
log["created_at"] = pd.to_datetime(log["created_at"], errors="coerce")
 
# ---------------------------------------------------------------- headline metrics
 
total_queries = len(log)
overall_validity = log["citation_validity"].mean(skipna=True)
grounded = log[log["mode"] != "no_retrieval"]
grounded_validity = grounded["citation_validity"].mean(skipna=True)
n_feedback = len(fb)
 
c1, c2, c3, c4 = st.columns(4)
c1.metric("Total queries", total_queries)
c2.metric(
    "Citation validity (grounded)",
    "n/a" if pd.isna(grounded_validity) else f"{grounded_validity:.0%}",
)
c3.metric(
    "Citation validity (all modes)",
    "n/a" if pd.isna(overall_validity) else f"{overall_validity:.0%}",
)
c4.metric("Feedback responses", n_feedback)
 
st.divider()
 
# ---------------------------------------------------------------- 1. queries over time
 
st.subheader("Queries over time")
by_day = (
    log.set_index("created_at")
    .resample("D")
    .size()
    .rename("queries")
    .to_frame()
)
st.bar_chart(by_day, height=220)
 
col_left, col_right = st.columns(2)
 
# ---------------------------------------------------------------- 2. mode distribution
 
with col_left:
    st.subheader("Queries by mode")
    by_mode = log["mode"].value_counts().rename_axis("mode").to_frame("queries")
    st.bar_chart(by_mode, height=260)
 
# ---------------------------------------------------------------- 3. citation validity by mode
 
with col_right:
    st.subheader("Citation validity by mode")
    st.caption("Fraction of cited PMIDs that exist in the corpus.")
    val_by_mode = (
        log.groupby("mode")["citation_validity"]
        .mean()
        .rename("validity")
        .to_frame()
        .sort_values("validity", ascending=False)
    )
    st.bar_chart(val_by_mode, height=260)
    st.caption(
        "no_retrieval typically sits near 0: fluent answers, fabricated sources."
    )
 
col_left2, col_right2 = st.columns(2)
 
# ---------------------------------------------------------------- 4. feedback split
 
with col_left2:
    st.subheader("User feedback")
    if fb.empty:
        st.caption("No feedback yet. Use the 👍 / 👎 buttons on the main page.")
    else:
        pos = int((fb["rating"] == 1).sum())
        neg = int((fb["rating"] == -1).sum())
        fb_df = pd.DataFrame(
            {"count": [pos, neg]}, index=["👍 up", "👎 down"]
        )
        st.bar_chart(fb_df, height=240)
        ratio = pos / (pos + neg) if (pos + neg) else 0
        st.caption(f"{pos} up / {neg} down ({ratio:.0%} positive)")
 
# ---------------------------------------------------------------- 5. latency by mode
 
with col_right2:
    st.subheader("Median latency by mode (ms)")
    lat = (
        log.groupby("mode")["latency_ms"]
        .median()
        .rename("median_ms")
        .to_frame()
        .sort_values("median_ms")
    )
    st.bar_chart(lat, height=240)
    st.caption("The agent is slower: routing + per-subquestion retrieval.")
 
# ---------------------------------------------------------------- 6. token cost
 
st.subheader("Token usage over time")
st.caption("Prompt + completion tokens per day, a proxy for API cost.")
tokens = log.copy()
tokens["total_tokens"] = tokens["prompt_tokens"] + tokens["completion_tokens"]
tok_by_day = (
    tokens.set_index("created_at")["total_tokens"].resample("D").sum().to_frame()
)
st.area_chart(tok_by_day, height=240)
 
st.divider()
 
# ---------------------------------------------------------------- raw recent activity
 
with st.expander("Recent queries (raw)"):
    st.dataframe(
        log[["created_at", "mode", "question", "n_cited", "n_valid_cited", "latency_ms"]].head(25),
        use_container_width=True,
        hide_index=True,
    )