"""Streamlit entry point for the Ridgeline Quote Desk review portal."""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Callable

import pandas as pd
import streamlit as st

from app.portal import repository as repo


st.set_page_config(page_title="Ridgeline Quote Desk", page_icon="⛰️", layout="wide")


def _run_action(action: Callable[..., None], *args: Any) -> None:
    try:
        action(*args)
    except (ValueError, repo.ConcurrentActionError) as exc:
        st.error(str(exc))
    except Exception as exc:
        st.error("The database action failed. Check the application logs and try again.")
        print(f"Portal database action failed: {type(exc).__name__}: {exc}")
    else:
        st.cache_data.clear()
        st.success("Saved.")
        st.rerun()


def _money(value: Decimal | None) -> str:
    return "—" if value is None else f"${value:,.2f}"


def _table(rows: list[dict[str, Any]], columns: list[str] | None = None) -> None:
    if not rows:
        st.info("No records found.")
        return
    frame = pd.DataFrame(rows)
    if columns:
        frame = frame[[column for column in columns if column in frame.columns]]
    st.dataframe(frame, use_container_width=True, hide_index=True)


@st.cache_data(ttl=10)
def _dashboard() -> dict[str, Any]:
    return repo.dashboard()


@st.cache_data(ttl=10)
def _jobs(review_status: str | None, job_status: str | None, search: str) -> list[dict[str, Any]]:
    return repo.list_jobs(review_status=review_status, job_status=job_status, search=search)


def dashboard_page() -> None:
    st.title("Quote desk")
    st.caption("A live view of extraction, matching, and human review.")
    data = _dashboard()
    totals = data["totals"]
    metrics = st.columns(6)
    for box, label, key in zip(
        metrics,
        ("All jobs", "Awaiting review", "In queue", "Running", "Approved", "Failed"),
        ("total", "pending_review", "pending", "running", "approved", "failed"),
    ):
        box.metric(label, totals[key])
    st.subheader("Recent jobs")
    _table(
        data["recent"],
        ["rfq_id", "file_name", "job_status", "review_status", "attempts", "max_attempts", "created_at", "finished_at"],
    )


def _line_card(job: dict[str, Any], line: dict[str, Any], reviewer: str) -> None:
    flags = ", ".join(line["flags"] or []) or "none"
    confidence = "—" if line["confidence"] is None else f"{float(line['confidence']):.0%}"
    marker = "⚠️" if line["needs_review"] else "✅"
    title = f"{marker} Line {line['line']} · {line['as_written']}"
    with st.expander(title, expanded=bool(line["needs_review"])):
        left, middle, right = st.columns([2, 2, 1])
        left.markdown(
            f"**Matched part:** `{line['effective_part_number'] or 'No match'}`  \n"
            f"**Description:** {line['description'] or '—'}  \n"
            f"**Method:** {line['match_method'] or '—'} · **Confidence:** {confidence}"
        )
        middle.markdown(
            f"**Requested:** {line['quantity'] or '—'} {line['unit'] or ''}  \n"
            f"**Catalog unit:** {line['catalog_unit'] or '—'}  \n"
            f"**Flags:** {flags}"
        )
        right.markdown(
            f"**List price**  \n{_money(line['list_price'])}  \n"
            f"**Warehouse**  \n{line['primary_warehouse'] or '—'}"
        )
        if line["corrected_part_number"]:
            st.info(
                f"Reviewer correction applied: {line['ridgeline_part_number'] or 'no original match'} "
                f"→ {line['corrected_part_number']}"
            )
        if line["note"]:
            st.caption(f"Worker note: {line['note']}")

        if job["review_status"] != "pending_review":
            return
        search = st.text_input(
            "Find a better catalog match",
            value=line["as_written"],
            key=f"search-{job['job_id']}-{line['line']}",
        )
        candidates = repo.search_products(search) if search.strip() else []
        if not candidates:
            st.caption("No catalog candidates found. Try a part number or shorter description.")
            return
        labels = {
            f"{p['part_number']} · {p['description']} · {_money(p['list_price'])}/{p['unit']}": p["part_number"]
            for p in candidates
        }
        with st.form(f"correct-{job['job_id']}-{line['line']}"):
            choice = st.selectbox("Candidate", list(labels))
            submitted = st.form_submit_button("Use this match")
            if submitted:
                _run_action(
                    repo.correct_part,
                    job["job_id"],
                    line["line"],
                    labels[choice],
                    reviewer,
                )


def _decision_panel(job: dict[str, Any], reviewer: str) -> None:
    st.subheader("Decision")
    if job["review_status"] != "pending_review":
        st.info(f"This job is already {job['review_status'].replace('_', ' ')}.")
        return
    if job["job_status"] == "succeeded":
        approve_col, reject_col = st.columns(2)
        with approve_col:
            st.markdown("Approve the reviewed draft for the next downstream step.")
            if st.button("Approve quote", type="primary", use_container_width=True):
                _run_action(repo.approve, job["job_id"], reviewer)
        with reject_col, st.form(f"reject-{job['job_id']}"):
            reason = st.text_area("Rejection reason", placeholder="Describe what must be corrected")
            if st.form_submit_button("Reject quote", use_container_width=True):
                _run_action(repo.reject, job["job_id"], reviewer, reason)
    elif job["job_status"] == "failed":
        st.error(job["escalation_reason"] or job["last_error"] or "The worker could not create a draft.")
        requeue_col, reject_col = st.columns(2)
        with requeue_col:
            if st.button("Requeue job", type="primary", use_container_width=True):
                _run_action(repo.requeue, job["job_id"], reviewer)
        with reject_col, st.form(f"reject-failed-{job['job_id']}"):
            reason = st.text_area("Close without retrying", placeholder="Reason for rejecting this request")
            if st.form_submit_button("Reject job", use_container_width=True):
                _run_action(repo.reject, job["job_id"], reviewer, reason)


def review_page(reviewer: str) -> None:
    st.title("Review queue")
    jobs = _jobs("pending_review", None, "")
    if not jobs:
        st.success("The review queue is clear.")
        return
    labels = {
        f"{job['rfq_id']} · {job['job_status']} · {job['lines_to_review']} flagged": job
        for job in jobs
    }
    selected = st.selectbox("Draft", list(labels))
    job = repo.get_job(labels[selected]["job_id"])
    if not job:
        st.warning("That job no longer exists.")
        return
    quote = repo.get_quote(job["job_id"])
    lines = repo.get_quote_lines(job["job_id"])

    st.subheader(job["rfq_id"])
    detail_cols = st.columns(4)
    detail_cols[0].metric("Pipeline", job["job_status"])
    detail_cols[1].metric("Review", job["review_status"].replace("_", " "))
    detail_cols[2].metric("Attempts", f"{job['attempts']} / {job['max_attempts']}")
    detail_cols[3].metric("Lines", len(lines))

    quote_tab, audit_tab = st.tabs(["Draft quote", "Audit history"])
    with quote_tab:
        if quote:
            customer = quote["catalog_customer_name"] or quote["customer_name"] or "Unknown customer"
            st.markdown(f"**Customer:** {customer}  \n**Pricing tier:** {quote['pricing_tier'] or '—'}")
            for line in lines:
                _line_card(job, line, reviewer)
            if quote["non_item_rows"]:
                st.caption("Non-item rows: " + " · ".join(quote["non_item_rows"]))
        elif job["job_status"] != "failed":
            st.warning("The worker marked this job ready, but no quote record was found.")
        _decision_panel(job, reviewer)
    with audit_tab:
        _table(repo.get_events(job["job_id"]), ["created_at", "event", "actor", "detail"])


@st.fragment(run_every="5s")
def _processing_queue() -> None:
    jobs = repo.list_jobs()
    states = {
        "Ready": [job for job in jobs if job["ready_to_claim"]],
        "Scheduled": [
            job for job in jobs if job["job_status"] == "pending" and not job["ready_to_claim"]
        ],
        "Running": [job for job in jobs if job["job_status"] == "running"],
        "Succeeded": [job for job in jobs if job["job_status"] == "succeeded"],
        "Failed": [job for job in jobs if job["job_status"] == "failed"],
        "Cancelled": [job for job in jobs if job["job_status"] == "cancelled"],
    }
    for column, (state, rows) in zip(st.columns(len(states)), states.items()):
        column.metric(state, len(rows))

    state = st.selectbox("Processing state", ["All", *states])
    states["Ready"].sort(key=lambda job: (-job["priority"], job["created_at"]))
    states["Scheduled"].sort(key=lambda job: job["run_after"])
    visible = [job for rows in states.values() for job in rows] if state == "All" else states[state]
    if not visible:
        st.info("No jobs in this view.")
        return

    labels = {job["job_id"]: label for label, rows in states.items() for job in rows}
    _table([
        {
            "RFQ": job["rfq_id"],
            "File": job["file_name"],
            "Processing": labels[job["job_id"]],
            "Review": job["review_status"].replace("_", " "),
            "Priority": job["priority"],
            "Attempts": f"{job['attempts']} / {job['max_attempts']}",
            "Worker": job["locked_by"] if job["job_status"] == "running" else None,
            "Eligible at": job["run_after"] if job["job_status"] == "pending" else None,
            "Queued": job["created_at"],
            "First started": job["started_at"],
            "Finished": job["finished_at"],
        }
        for job in visible
    ])

    by_id = {job["job_id"]: job for job in visible}
    selected = st.selectbox(
        "Inspect job history",
        list(by_id),
        index=None,
        format_func=lambda job_id: f"{by_id[job_id]['rfq_id']} ({job_id})",
    )
    if selected is not None:
        job = by_id[selected]
        if job["escalation_reason"]:
            st.warning(job["escalation_reason"])
        if job["job_status"] == "running":
            st.caption(f"Worker lease expires at {job['lease_expires_at']:%Y-%m-%d %H:%M:%S %Z}")
        _table(repo.get_events(selected), ["created_at", "event", "actor", "detail"])


def all_jobs_page(reviewer: str) -> None:
    st.title("All jobs")
    filter_col, state_col, search_col = st.columns([1, 1, 2])
    review_choice = filter_col.selectbox(
        "Review status", ["All", "not_ready", "pending_review", "approved", "rejected"]
    )
    job_choice = state_col.selectbox(
        "Job status", ["All", "pending", "running", "succeeded", "failed", "cancelled"]
    )
    search = search_col.text_input("Search RFQ or file name")
    jobs = _jobs(
        None if review_choice == "All" else review_choice,
        None if job_choice == "All" else job_choice,
        search,
    )
    _table(
        jobs,
        ["rfq_id", "file_name", "job_status", "review_status", "line_count", "lines_to_review", "attempts", "created_at", "finished_at"],
    )
    pending = [job for job in jobs if job["job_status"] == "pending"]
    if pending:
        st.divider()
        with st.expander("Cancel an unstarted job"):
            options = {f"{job['rfq_id']} · {job['file_name']}": job["job_id"] for job in pending}
            selected = st.selectbox("Pending job", list(options), key="cancel-job")
            st.warning("Cancellation is final for this queue entry.")
            if st.button("Cancel selected job"):
                _run_action(repo.cancel, options[selected], reviewer)


def main() -> None:
    with st.sidebar:
        st.title("⛰️ Ridgeline")
        st.caption("Quote Desk")
        reviewer = st.text_input("Reviewer", placeholder="Your name")
        page = st.radio("Navigate", ["Dashboard", "Processing queue", "Review queue", "All jobs"])
        st.divider()
        st.caption("Catalog prices are read-only. Every decision is audited.")
    if page == "Dashboard":
        dashboard_page()
    elif page == "Processing queue":
        st.title("Processing queue")
        st.caption("Updates every 5 seconds while this page is open.")
        _processing_queue()
    elif page == "Review queue":
        review_page(reviewer)
    else:
        all_jobs_page(reviewer)


if __name__ == "__main__":
    main()
