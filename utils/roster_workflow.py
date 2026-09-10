# utils/roster_workflow.py
"""
Phase 2 roster approval workflow, change application, versioning, and production sync.
"""

from __future__ import annotations

import json
import uuid
from datetime import date, time, timedelta
from typing import Any

from utils.roster_helpers import (
    FULL_DAY_HOURS,
    HALF_DAY_HOURS,
    assigned_hours_for_roster_day,
    day_shift_times,
    get_eligible_employees,
    half_day_hours_from_roster_day,
    implied_full_day_hours,
    is_admin_or_super_admin,
    is_org_holiday_day,
    load_user_monthly_tracker_baseline,
    now_str,
    parse_date,
    parse_month_year,
    sync_to_user_monthly_tracker,
    write_audit_log,
    cap_month_goals_to_universal_working_days,
)
from utils.json_utils import dumps_json_safe
from utils.roster_metrics import (
    build_month_snapshot,
    apply_active_leaves_to_days,
    iter_dates_inclusive,
    recalculate_metrics_from_days_and_leaves,
)


EDITABLE_STATUSES = frozenset({"Draft", "Approved"})
SUBMITTABLE_STATUSES = frozenset({"Draft", "Approved"})
LOCKABLE_STATUSES = frozenset({"Draft", "Approved"})


def cancel_pending_requests_for_roster(
    cursor,
    roster_month_id: int,
    performed_by: int,
    *,
    note: str = "Cancelled because roster was locked",
) -> int:
    now = now_str()
    cursor.execute(
        """
        SELECT request_id
        FROM roster_change_request
        WHERE roster_month_id=%s AND status='Pending' AND is_active=1
        """,
        (int(roster_month_id),),
    )
    rows = cursor.fetchall() or []
    for row in rows:
        cursor.execute(
            """
            UPDATE roster_change_request
            SET status='Cancelled due to Withdrawal',
                reviewer_comment=%s,
                reviewed_by=%s,
                reviewed_date=%s,
                batch_id=NULL
            WHERE request_id=%s
            """,
            (note, int(performed_by), now, int(row["request_id"])),
        )
    return len(rows)


def count_pending_change_requests_for_month(cursor, month_year: str) -> int:
    cursor.execute(
        """
        SELECT COUNT(*) AS cnt
        FROM roster_change_request rcr
        JOIN roster_month rm ON rm.roster_month_id = rcr.roster_month_id
        WHERE rm.month_year=%s AND rm.is_active=1
          AND rcr.is_active=1 AND rcr.status='Pending'
        """,
        (str(month_year).strip(),),
    )
    row = cursor.fetchone() or {}
    return int(row.get("cnt") or 0)


def lock_roster_month_record(cursor, roster_month_id: int, locked_by: int) -> bool:
    """Lock one roster month unless already Locked or Pending Approval blocked separately."""
    now = now_str()
    cursor.execute(
        """
        UPDATE roster_month
        SET status='Locked', locked_by=%s, locked_date=%s, updated_date=%s
        WHERE roster_month_id=%s AND is_active=1 AND status != 'Locked'
        """,
        (int(locked_by), now, now, int(roster_month_id)),
    )
    return cursor.rowcount > 0


def unlock_roster_month_record(cursor, roster_month_id: int) -> bool:
    """Unlock back to Draft or Approved depending on approval history."""
    cursor.execute(
        """
        SELECT approved_by, last_approved_by
        FROM roster_month
        WHERE roster_month_id=%s AND is_active=1 AND status='Locked'
        """,
        (int(roster_month_id),),
    )
    row = cursor.fetchone()
    if not row:
        return False

    restore_status = (
        "Approved"
        if row.get("approved_by") or row.get("last_approved_by")
        else "Draft"
    )
    now = now_str()
    cursor.execute(
        """
        UPDATE roster_month
        SET status=%s, locked_by=NULL, locked_date=NULL, updated_date=%s
        WHERE roster_month_id=%s AND is_active=1 AND status='Locked'
        """,
        (restore_status, now, int(roster_month_id)),
    )
    return cursor.rowcount > 0


def can_create_change_requests(status: str) -> bool:
    return (status or "").strip() in EDITABLE_STATUSES


def is_month_locked(status: str) -> bool:
    return (status or "").strip().lower() == "locked"


def can_approve_reject(role_name: str) -> bool:
    return is_admin_or_super_admin(role_name)


def get_roster_month(cursor, roster_month_id: int, active_only: bool = True) -> dict | None:
    sql = """
        SELECT rm.*, u.user_name, LOWER(TRIM(r.role_name)) AS employee_role_name,
               locker.user_name AS locked_by_name
        FROM roster_month rm
        JOIN tfs_user u ON u.user_id = rm.user_id
        JOIN user_role r ON r.role_id = u.role_id
        LEFT JOIN tfs_user locker ON locker.user_id = rm.locked_by
        WHERE rm.roster_month_id=%s
    """
    params: list[Any] = [int(roster_month_id)]
    if active_only:
        sql += " AND rm.is_active=1"
    cursor.execute(sql, tuple(params))
    return cursor.fetchone()


def get_roster_days(cursor, roster_month_id: int, active_only: bool = True) -> list[dict]:
    sql = """
        SELECT *
        FROM roster_day
        WHERE roster_month_id=%s
    """
    params: list[Any] = [int(roster_month_id)]
    if active_only:
        sql += " AND is_active=1"
    sql += " ORDER BY roster_date ASC"
    cursor.execute(sql, tuple(params))
    return cursor.fetchall() or []


def get_roster_leaves(cursor, roster_month_id: int, active_only: bool = True) -> list[dict]:
    sql = """
        SELECT *
        FROM roster_leave
        WHERE roster_month_id=%s
    """
    params: list[Any] = [int(roster_month_id)]
    if active_only:
        sql += " AND is_active=1"
    sql += " ORDER BY start_date ASC"
    cursor.execute(sql, tuple(params))
    return cursor.fetchall() or []


def assert_manager_scope(cursor, logged_in_user_id: int, role_name: str, roster_month: dict) -> str | None:
    if is_admin_or_super_admin(role_name):
        return None
    year, month = parse_month_year(roster_month["month_year"])
    allowed = get_eligible_employees(
        cursor,
        logged_in_user_id,
        role_name,
        year,
        month,
        user_id=int(roster_month["user_id"]),
    )
    if not allowed:
        return "You do not have permission to manage this employee roster"
    return None


def create_change_request(
    cursor,
    *,
    roster_month_id: int,
    user_id: int,
    change_type: str,
    change_payload: dict,
    submitted_by: int,
    batch_id: str | None = None,
) -> int:
    now = now_str()
    cursor.execute(
        """
        INSERT INTO roster_change_request (
            roster_month_id, user_id, change_type, change_payload, batch_id,
            status, submitted_by, submitted_date, is_active
        ) VALUES (%s,%s,%s,%s,%s,'Pending',%s,%s,1)
        """,
        (
            int(roster_month_id),
            int(user_id),
            change_type,
            json.dumps(change_payload),
            batch_id,
            int(submitted_by),
            now,
        ),
    )
    request_id = cursor.lastrowid
    write_audit_log(
        cursor,
        roster_month_id=int(roster_month_id),
        user_id=int(user_id),
        action="CHANGE_REQUEST_CREATED",
        entity_type="roster_change_request",
        entity_id=int(request_id),
        old_value=None,
        new_value={"change_type": change_type, "change_payload": change_payload, "batch_id": batch_id},
        performed_by=int(submitted_by),
        approval_status="Pending",
    )
    return int(request_id)


def _leave_request_key(change_type: str, payload: dict) -> tuple:
    start = (payload.get("start_date") or "")[:10]
    end = (payload.get("end_date") or "")[:10]
    leave_type = (payload.get("leave_type") or "").strip().lower()
    if change_type == "LEAVE_UPDATE":
        return (change_type, start, end, leave_type, str(payload.get("leave_id") or ""))
    return (change_type, start, end, leave_type)


def _parse_payload(raw) -> dict:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}


def _date_only(value) -> str:
    if value is None:
        return ""
    s = str(value).strip()
    if not s:
        return ""
    # Handle ISO / MySQL datetime strings
    s = s.replace("T", " ").split(" ")[0]
    return s[:10]


def _ranges_overlap(a_start: str, a_end: str, b_start: str, b_end: str) -> bool:
    a_s, a_e = _date_only(a_start), _date_only(a_end)
    b_s, b_e = _date_only(b_start), _date_only(b_end)
    if not a_s or not a_e or not b_s or not b_e:
        return False
    return a_s <= b_e and b_s <= a_e


def find_matching_pending_leave_requests(
    cursor,
    *,
    roster_month_id: int,
    change_type: str,
    change_payload: dict,
) -> list[dict]:
    """
    Find pending leave requests for this roster month that should be updated
    instead of creating a duplicate.

    Matching rules:
    - LEAVE_UPDATE with leave_id → same leave_id
    - Otherwise → any pending LEAVE_ADD / LEAVE_UPDATE whose dates overlap
      (covers Excel leave then panel edit for affect_target / leave_type)
    """
    start = _date_only(change_payload.get("start_date"))
    end = _date_only(change_payload.get("end_date"))
    leave_id = change_payload.get("leave_id")

    cursor.execute(
        """
        SELECT request_id, batch_id, change_type, change_payload
        FROM roster_change_request
        WHERE roster_month_id=%s
          AND change_type IN ('LEAVE_ADD', 'LEAVE_UPDATE')
          AND status='Pending' AND is_active=1
        ORDER BY request_id DESC
        """,
        (int(roster_month_id),),
    )
    rows = cursor.fetchall() or []
    matches: list[dict] = []

    for row in rows:
        payload = _parse_payload(row.get("change_payload"))
        row_leave_id = payload.get("leave_id")
        row_start = _date_only(payload.get("start_date"))
        row_end = _date_only(payload.get("end_date"))

        # Prefer exact leave_id match when updating an existing leave
        if change_type == "LEAVE_UPDATE" and leave_id and row_leave_id is not None:
            if str(row_leave_id) == str(leave_id):
                matches.append(row)
                continue

        # Same / overlapping dates → treat as the same leave request
        if start and end and _ranges_overlap(start, end, row_start, row_end):
            matches.append(row)

    return matches


def find_active_leave_overlapping_dates(
    cursor,
    *,
    roster_month_id: int,
    start_date: str,
    end_date: str,
) -> dict | None:
    """Find an active leave that exactly matches or overlaps the given date range."""
    start = (start_date or "")[:10]
    end = (end_date or "")[:10]
    if not start or not end:
        return None
    # Prefer exact match first (same start/end)
    cursor.execute(
        """
        SELECT *
        FROM roster_leave
        WHERE roster_month_id=%s AND is_active=1
          AND DATE(start_date)=DATE(%s) AND DATE(end_date)=DATE(%s)
        ORDER BY leave_id DESC
        LIMIT 1
        """,
        (int(roster_month_id), start, end),
    )
    row = cursor.fetchone()
    if row:
        return row
    return None


def create_or_update_leave_request(
    cursor,
    *,
    roster_month_id: int,
    user_id: int,
    change_type: str,
    change_payload: dict,
    submitted_by: int,
) -> tuple[int, bool]:
    """
    Upsert a pending leave change request.
    If LEAVE_ADD targets dates that already have an active leave, convert to LEAVE_UPDATE
    so fields like affect_target can be corrected on the existing leave.
    If a pending Excel/panel leave already covers the same dates, UPDATE that request
    instead of inserting a second approval for the same day.
    Returns (request_id, created_new).
    """
    payload = dict(change_payload or {})
    payload["start_date"] = _date_only(payload.get("start_date"))
    payload["end_date"] = _date_only(payload.get("end_date"))
    effective_type = change_type

    if effective_type == "LEAVE_ADD" and not payload.get("leave_id"):
        existing = find_active_leave_overlapping_dates(
            cursor,
            roster_month_id=roster_month_id,
            start_date=str(payload.get("start_date") or ""),
            end_date=str(payload.get("end_date") or ""),
        )
        if existing:
            effective_type = "LEAVE_UPDATE"
            payload["leave_id"] = int(existing["leave_id"])

    matches = find_matching_pending_leave_requests(
        cursor,
        roster_month_id=roster_month_id,
        change_type=effective_type,
        change_payload=payload,
    )

    if matches:
        keep = matches[0]
        keep_id = int(keep["request_id"])
        keep_payload = _parse_payload(keep.get("change_payload"))
        # Preserve leave_id if the pending row already had one
        if not payload.get("leave_id") and keep_payload.get("leave_id"):
            payload["leave_id"] = keep_payload.get("leave_id")
            effective_type = "LEAVE_UPDATE"
        # Keep LEAVE_ADD if leave does not exist yet (typical Excel → panel edit)
        if effective_type == "LEAVE_ADD" and payload.get("leave_id"):
            effective_type = "LEAVE_UPDATE"
        if keep.get("change_type") == "LEAVE_ADD" and not payload.get("leave_id"):
            effective_type = "LEAVE_ADD"

        now = now_str()
        cursor.execute(
            """
            UPDATE roster_change_request
            SET change_type=%s, change_payload=%s, submitted_by=%s, submitted_date=%s
            WHERE request_id=%s
            """,
            (
                effective_type,
                json.dumps(payload),
                int(submitted_by),
                now,
                keep_id,
            ),
        )
        for dup in matches[1:]:
            cursor.execute(
                """
                UPDATE roster_change_request
                SET status='Cancelled due to Withdrawal',
                    reviewer_comment='Duplicate leave request removed automatically',
                    reviewed_by=%s,
                    reviewed_date=%s
                WHERE request_id=%s
                """,
                (int(submitted_by), now, int(dup["request_id"])),
            )
        write_audit_log(
            cursor,
            roster_month_id=int(roster_month_id),
            user_id=int(user_id),
            action="CHANGE_REQUEST_UPDATED",
            entity_type="roster_change_request",
            entity_id=int(keep_id),
            old_value={"change_type": keep.get("change_type"), "change_payload": keep_payload},
            new_value={"change_type": effective_type, "change_payload": payload},
            performed_by=int(submitted_by),
            approval_status="Pending",
            notes="Updated existing pending leave instead of creating a duplicate",
        )
        return keep_id, False

    return (
        create_change_request(
            cursor,
            roster_month_id=roster_month_id,
            user_id=user_id,
            change_type=effective_type,
            change_payload=payload,
            submitted_by=submitted_by,
            batch_id=None,
        ),
        True,
    )


def dedupe_change_request_rows(rows: list[dict]) -> list[dict]:
    """Hide duplicate pending leave rows (same employee, dates, status, batch)."""
    seen: set[tuple] = set()
    deduped: list[dict] = []

    for row in sorted(rows, key=lambda r: int(r.get("request_id") or 0), reverse=True):
        change_type = row.get("change_type") or ""
        if change_type not in ("LEAVE_ADD", "LEAVE_UPDATE"):
            deduped.append(row)
            continue

        payload = row.get("change_payload") or {}
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except Exception:
                payload = {}

        key = (
            int(row.get("user_id") or 0),
            change_type,
            _leave_request_key(change_type, payload),
            row.get("status") or "",
            row.get("batch_id") or "",
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(row)

    deduped.sort(
        key=lambda r: (
            r.get("submitted_date") or "",
            int(r.get("request_id") or 0),
        ),
        reverse=True,
    )
    return deduped


def cancel_duplicate_pending_leave_requests(
    cursor,
    *,
    approved_request: dict,
    reviewed_by: int,
) -> int:
    """When one leave request is approved, cancel other pending duplicates."""
    change_type = approved_request.get("change_type")
    if change_type not in ("LEAVE_ADD", "LEAVE_UPDATE"):
        return 0

    payload = approved_request.get("change_payload") or {}
    if isinstance(payload, str):
        payload = json.loads(payload)

    start = (payload.get("start_date") or "")[:10]
    end = (payload.get("end_date") or "")[:10]
    if not start or not end:
        return 0

    now = now_str()
    keep_id = int(approved_request["request_id"])
    roster_month_id = int(approved_request["roster_month_id"])

    if change_type == "LEAVE_UPDATE":
        leave_id = payload.get("leave_id")
        cursor.execute(
            """
            UPDATE roster_change_request
            SET status='Cancelled due to Withdrawal',
                reviewer_comment='Duplicate leave update — auto-cancelled after approval',
                reviewed_by=%s, reviewed_date=%s
            WHERE roster_month_id=%s AND change_type='LEAVE_UPDATE'
              AND status='Pending' AND is_active=1 AND request_id != %s
              AND JSON_UNQUOTE(JSON_EXTRACT(change_payload, '$.leave_id')) = %s
            """,
            (reviewed_by, now, roster_month_id, keep_id, str(leave_id)),
        )
    else:
        cursor.execute(
            """
            UPDATE roster_change_request
            SET status='Cancelled due to Withdrawal',
                reviewer_comment='Duplicate leave request — auto-cancelled after approval',
                reviewed_by=%s, reviewed_date=%s
            WHERE roster_month_id=%s AND change_type='LEAVE_ADD'
              AND status='Pending' AND is_active=1 AND request_id != %s
              AND JSON_UNQUOTE(JSON_EXTRACT(change_payload, '$.start_date')) = %s
              AND JSON_UNQUOTE(JSON_EXTRACT(change_payload, '$.end_date')) = %s
            """,
            (reviewed_by, now, roster_month_id, keep_id, start, end),
        )

    return cursor.rowcount


def weekoff_swap_preview(cursor, roster_month_id: int, new_week_off_dates: list[str]) -> dict:
    days = get_roster_days(cursor, roster_month_id)
    if not days:
        return {"changes": [], "message": "No roster days found"}

    new_offs = {parse_date(d) for d in new_week_off_dates if parse_date(d)}
    changes = []

    for day in days:
        d = parse_date(day.get("roster_date"))
        if not d:
            continue
        current_type = (day.get("day_type") or "").strip()
        if current_type in ("Left", "PreJoin"):
            continue
        currently_off = current_type == "WeekOff"
        should_off = d in new_offs

        if currently_off == should_off:
            continue

        if should_off:
            new_type = "WeekOff"
        elif day.get("holiday_id"):
            new_type = "Holiday"
        else:
            new_type = "Working"

        changes.append(
            {
                "roster_date": d.isoformat(),
                "current_day_type": day.get("day_type"),
                "proposed_day_type": new_type,
            }
        )

    return {
        "roster_month_id": int(roster_month_id),
        "proposed_week_off_dates": sorted(d.isoformat() for d in new_offs),
        "changes": changes,
        "requires_confirmation": True,
    }


def _parse_time_value(value) -> time | None:
    if value is None or value == "":
        return None
    if isinstance(value, time):
        return value
    s = str(value).strip()
    if len(s) >= 5:
        parts = s.split(":")
        return time(int(parts[0]), int(parts[1]), int(parts[2]) if len(parts) > 2 else 0)
    return None


def _daily_full_hours_for_roster_month(cursor, roster_month: dict) -> float:
    """Per-day full hours for this person (tracker baseline, else 9)."""
    user_id = roster_month.get("user_id")
    month_year = roster_month.get("month_year")
    if user_id and month_year:
        baseline = load_user_monthly_tracker_baseline(cursor, int(user_id), str(month_year))
        hrs = float(baseline.get("daily_full_hours") or 0)
        if hrs > 0:
            return hrs
    return FULL_DAY_HOURS


def _sync_temp_qc_assigned_for_day(
    cursor,
    *,
    user_id: int,
    roster_date: date,
    day_type: str,
    working_type: str | None,
    is_half_leave: bool = False,
) -> None:
    """
    Keep temp_qc.assigned_hours in sync for this person/date so billable + tracker
    pick up Leave → Working (and the reverse) without waiting for the morning cron.
    """
    cursor.execute(
        "SELECT user_tenure FROM tfs_user WHERE user_id=%s LIMIT 1",
        (int(user_id),),
    )
    user_row = cursor.fetchone() or {}
    hours = assigned_hours_for_roster_day(
        user_row.get("user_tenure"),
        day_type=day_type,
        working_type=working_type or "Full",
        has_roster_day=True,
        is_half_leave=is_half_leave,
    )
    date_str = roster_date.isoformat()
    now = now_str()
    cursor.execute(
        """
        INSERT INTO temp_qc (user_id, assigned_hours, date, updated_date)
        VALUES (%s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE
            assigned_hours = VALUES(assigned_hours),
            updated_date = VALUES(updated_date)
        """,
        (int(user_id), float(hours), date_str, now),
    )


def apply_day_update(cursor, roster_month: dict, payload: dict) -> None:
    roster_month_id = int(roster_month["roster_month_id"])
    roster_date = parse_date(payload.get("roster_date"))
    if not roster_date:
        raise ValueError("roster_date is required")

    cursor.execute(
        """
        SELECT * FROM roster_day
        WHERE roster_month_id=%s AND roster_date=%s AND is_active=1
        """,
        (roster_month_id, roster_date.isoformat()),
    )
    day = cursor.fetchone()
    if not day:
        raise ValueError("Roster day not found")

    old_day_type = (day.get("day_type") or "").strip()
    old_leave_id = day.get("leave_id")

    day_type = payload.get("day_type", day.get("day_type"))
    working_type = payload.get("working_type", day.get("working_type"))
    shift = payload.get("shift", day.get("shift", "DAY"))

    if is_org_holiday_day(day):
        day_type_try = (day_type or "").strip()
        wt_try = (working_type or "Full").strip()
        if day_type_try == "WeekOff":
            day_type = "Holiday"
        if day_type_try not in ("Left",) and (day_type_try == "Leave" or wt_try == "Half"):
            raise ValueError(
                "Leave or half day cannot be added on a Holiday. "
                "Set a working day or night shift if this person must work."
            )

    current_hours = float(day.get("working_hours") or FULL_DAY_HOURS)
    old_working_type = (day.get("working_type") or "Full").strip()
    if working_type == "Half":
        working_hours = half_day_hours_from_roster_day(old_working_type, current_hours)
    elif working_type == "Full":
        if old_working_type == "Half":
            working_hours = implied_full_day_hours(old_working_type, current_hours)
        else:
            working_hours = float(payload.get("working_hours") or current_hours)
    else:
        working_hours = float(payload.get("working_hours", current_hours))

    shift_start = _parse_time_value(payload.get("shift_start", day.get("shift_start")))
    shift_end = _parse_time_value(payload.get("shift_end", day.get("shift_end")))

    if day_type == "Working" and shift == "DAY" and not shift_start:
        shift_start, shift_end = day_shift_times(roster_month.get("employee_role_name", "agent"))

    # Restoring Working/WeekOff must also drop leave coverage. Otherwise
    # refresh_roster_month_metrics / list API re-apply Leave from roster_leave
    # and calendar + target look unchanged after approve.
    day_type_norm = (day_type or "").strip()
    if day_type_norm == "Left":
        working_type = "Full"
        working_hours = 0.0
        shift_start = None
        shift_end = None

    clear_leave = day_type_norm in ("Working", "WeekOff", "Holiday", "Left") and (
        old_day_type == "Leave" or bool(old_leave_id)
    )
    # Also clear if any active leave still covers this date (DB day may already be Working)
    if day_type_norm in ("Working", "WeekOff", "Holiday", "Left") and not clear_leave:
        cursor.execute(
            """
            SELECT leave_id FROM roster_leave
            WHERE roster_month_id=%s AND is_active=1
              AND DATE(start_date) <= DATE(%s)
              AND DATE(end_date) >= DATE(%s)
            LIMIT 1
            """,
            (roster_month_id, roster_date.isoformat(), roster_date.isoformat()),
        )
        clear_leave = cursor.fetchone() is not None

    # Leave → Working: ensure this person gets that day's assigned hours back
    restoring_to_working = day_type_norm == "Working" and (
        old_day_type in ("Leave", "Holiday", "WeekOff", "Left") or clear_leave
    )
    if restoring_to_working:
        wt = (working_type or "Full").strip() or "Full"
        working_type = wt
        full_hrs = _daily_full_hours_for_roster_month(cursor, roster_month)
        if wt == "Half":
            if float(working_hours or 0) <= 0:
                working_hours = round(full_hrs / 2, 2)
        else:
            # Full day for this employee (recover from half-leave hours if needed)
            recovered = implied_full_day_hours(old_working_type, current_hours)
            payload_hrs = payload.get("working_hours")
            try:
                payload_hrs_f = float(payload_hrs) if payload_hrs is not None else 0.0
            except (TypeError, ValueError):
                payload_hrs_f = 0.0
            if payload_hrs_f > 0 and old_working_type != "Half":
                working_hours = payload_hrs_f
            elif recovered > 0:
                working_hours = recovered
            else:
                working_hours = full_hrs

    now = now_str()
    try:
        cursor.execute(
            """
            UPDATE roster_day
            SET day_type=%s, shift=%s, shift_start=%s, shift_end=%s,
                working_type=%s, working_hours=%s,
                leave_id=%s,
                updated_date=%s
            WHERE roster_day_id=%s
            """,
            (
                day_type_norm or day_type,
                shift,
                shift_start.strftime("%H:%M:%S") if shift_start else None,
                shift_end.strftime("%H:%M:%S") if shift_end else None,
                working_type,
                working_hours,
                None if clear_leave else old_leave_id,
                now,
                int(day["roster_day_id"]),
            ),
        )
    except Exception as e:
        raw = str(e).lower()
        if day_type_norm == "Left" and (
            "day_type" in raw or "truncated" in raw or "1265" in raw or "data truncated" in raw
        ):
            raise ValueError(
                "Cannot save Left until the database allows that day type. "
                "Run python scripts/sync_roster_schema.py on the server, then approve again."
            ) from e
        raise

    if clear_leave:
        _remove_date_from_leave_coverage(
            cursor,
            roster_month_id,
            roster_date,
            leave_id=int(old_leave_id) if old_leave_id else None,
        )

    # Sync assigned hours for this person on this date (billable / tracker)
    if roster_month.get("user_id") and day_type_norm in (
        "Working",
        "Leave",
        "WeekOff",
        "Holiday",
        "Left",
    ):
        _sync_temp_qc_assigned_for_day(
            cursor,
            user_id=int(roster_month["user_id"]),
            roster_date=roster_date,
            day_type=day_type_norm,
            working_type=working_type,
            is_half_leave=False,
        )

    apply_rest = payload.get("apply_through_month_end")
    if day_type_norm == "Left" and (
        apply_rest is True or str(apply_rest).strip() in ("1", "true", "True")
    ):
        end = parse_date(roster_month.get("roster_end_date"))
        next_d = roster_date + timedelta(days=1)
        while end and next_d <= end:
            rest = dict(payload)
            rest["roster_date"] = next_d.isoformat()
            rest["day_type"] = "Left"
            rest["apply_through_month_end"] = 0
            apply_day_update(cursor, roster_month, rest)
            next_d += timedelta(days=1)


def _remove_date_from_one_leave(
    cursor,
    roster_month_id: int,
    roster_date: date,
    leave: dict,
) -> None:
    """Shrink / split / deactivate a single leave so roster_date is no longer covered."""
    start = parse_date(leave.get("start_date"))
    end = parse_date(leave.get("end_date"))
    if not start or not end:
        return
    if not (start <= roster_date <= end):
        return

    now = now_str()
    lid = int(leave["leave_id"])

    # Single-day leave covering this date → deactivate
    if start == end:
        cursor.execute(
            """
            UPDATE roster_leave SET is_active=0, updated_date=%s
            WHERE leave_id=%s
            """,
            (now, lid),
        )
        return

    # Shrink from start
    if roster_date == start:
        new_start = roster_date + timedelta(days=1)
        cursor.execute(
            """
            UPDATE roster_leave SET start_date=%s, updated_date=%s
            WHERE leave_id=%s
            """,
            (new_start.isoformat(), now, lid),
        )
        return

    # Shrink from end
    if roster_date == end:
        new_end = roster_date - timedelta(days=1)
        cursor.execute(
            """
            UPDATE roster_leave SET end_date=%s, updated_date=%s
            WHERE leave_id=%s
            """,
            (new_end.isoformat(), now, lid),
        )
        return

    # Middle of a multi-day leave → shrink original to left side, insert right side
    left_end = roster_date - timedelta(days=1)
    right_start = roster_date + timedelta(days=1)
    cursor.execute(
        """
        UPDATE roster_leave SET end_date=%s, updated_date=%s
        WHERE leave_id=%s
        """,
        (left_end.isoformat(), now, lid),
    )
    cursor.execute(
        """
        INSERT INTO roster_leave (
            roster_month_id, leave_type, start_date, end_date, reason,
            is_rostered, affect_target, is_half_day, is_active,
            created_by, created_date, updated_date
        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,1,%s,%s,%s)
        """,
        (
            int(roster_month_id),
            leave.get("leave_type") or "",
            right_start.isoformat(),
            end.isoformat(),
            leave.get("reason"),
            int(leave.get("is_rostered") or 1),
            int(leave.get("affect_target") or 0),
            int(leave.get("is_half_day") or 0),
            leave.get("created_by"),
            now,
            now,
        ),
    )
    new_leave_id = int(cursor.lastrowid)
    cursor.execute(
        """
        UPDATE roster_day
        SET leave_id=%s, updated_date=%s
        WHERE roster_month_id=%s AND is_active=1 AND leave_id=%s
          AND DATE(roster_date) BETWEEN DATE(%s) AND DATE(%s)
        """,
        (
            new_leave_id,
            now,
            int(roster_month_id),
            lid,
            right_start.isoformat(),
            end.isoformat(),
        ),
    )


def _remove_date_from_leave_coverage(
    cursor,
    roster_month_id: int,
    roster_date: date,
    leave_id: int | None = None,
) -> None:
    """
    When a Leave day is changed back to Working/WeekOff, remove that date from
    every overlapping roster_leave so refresh_metrics cannot re-apply Leave.
    """
    leaves: list[dict] = []
    seen_ids: set[int] = set()

    if leave_id:
        cursor.execute(
            """
            SELECT * FROM roster_leave
            WHERE leave_id=%s AND roster_month_id=%s AND is_active=1
            LIMIT 1
            """,
            (int(leave_id), int(roster_month_id)),
        )
        row = cursor.fetchone()
        if row:
            leaves.append(row)
            seen_ids.add(int(row["leave_id"]))

    cursor.execute(
        """
        SELECT * FROM roster_leave
        WHERE roster_month_id=%s AND is_active=1
          AND DATE(start_date) <= DATE(%s)
          AND DATE(end_date) >= DATE(%s)
        ORDER BY leave_id ASC
        """,
        (int(roster_month_id), roster_date.isoformat(), roster_date.isoformat()),
    )
    for row in cursor.fetchall() or []:
        lid = int(row["leave_id"])
        if lid not in seen_ids:
            leaves.append(row)
            seen_ids.add(lid)

    for leave in leaves:
        _remove_date_from_one_leave(cursor, roster_month_id, roster_date, leave)


def apply_weekoff_swap(cursor, roster_month: dict, payload: dict) -> None:
    for change in payload.get("changes") or []:
        apply_day_update(
            cursor,
            roster_month,
            {
                "roster_date": change.get("roster_date"),
                "day_type": change.get("proposed_day_type"),
            },
        )


def _assert_leave_not_on_holiday(cursor, roster_month_id: int, start, end) -> None:
    start_d = parse_date(start)
    end_d = parse_date(end)
    if not start_d or not end_d:
        return
    cursor.execute(
        """
        SELECT roster_date, day_type, holiday_id
        FROM roster_day
        WHERE roster_month_id=%s AND is_active=1
          AND DATE(roster_date) BETWEEN DATE(%s) AND DATE(%s)
        """,
        (int(roster_month_id), start_d.isoformat(), end_d.isoformat()),
    )
    for row in cursor.fetchall() or []:
        if is_org_holiday_day(row):
            raise ValueError(
                "Leave or half day cannot be added on a Holiday. "
                "Set a working day or night shift if this person must work."
            )


def apply_leave_add(cursor, roster_month: dict, payload: dict, created_by: int) -> int:
    roster_month_id = int(roster_month["roster_month_id"])
    _assert_leave_not_on_holiday(
        cursor, roster_month_id, payload.get("start_date"), payload.get("end_date")
    )
    now = now_str()
    cursor.execute(
        """
        INSERT INTO roster_leave (
            roster_month_id, leave_type, start_date, end_date, reason,
            is_rostered, affect_target, is_half_day, is_active,
            created_by, created_date, updated_date
        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,1,%s,%s,%s)
        """,
        (
            roster_month_id,
            payload.get("leave_type", ""),
            payload.get("start_date"),
            payload.get("end_date"),
            payload.get("reason"),
            int(payload.get("is_rostered", 1)),
            int(payload.get("affect_target", 0)),
            int(payload.get("is_half_day", 0)),
            int(created_by),
            now,
            now,
        ),
    )
    leave_id = int(cursor.lastrowid)
    _apply_leave_to_days(cursor, roster_month_id, leave_id, payload)
    return leave_id


def apply_leave_update(cursor, roster_month: dict, payload: dict) -> None:
    leave_id = int(payload["leave_id"])
    roster_month_id = int(roster_month["roster_month_id"])
    _assert_leave_not_on_holiday(
        cursor, roster_month_id, payload.get("start_date"), payload.get("end_date")
    )
    now = now_str()

    cursor.execute(
        """
        UPDATE roster_leave
        SET leave_type=%s, start_date=%s, end_date=%s, reason=%s,
            is_rostered=%s, affect_target=%s, is_half_day=%s, updated_date=%s
        WHERE leave_id=%s AND roster_month_id=%s AND is_active=1
        """,
        (
            payload.get("leave_type", ""),
            payload.get("start_date"),
            payload.get("end_date"),
            payload.get("reason"),
            int(payload.get("is_rostered", 1)),
            int(payload.get("affect_target", 0)),
            int(payload.get("is_half_day", 0)),
            now,
            int(leave_id),
            roster_month_id,
        ),
    )
    _clear_leave_from_days(cursor, roster_month_id, leave_id)
    _apply_leave_to_days(cursor, roster_month_id, leave_id, payload)


def apply_leave_delete(cursor, roster_month: dict, payload: dict) -> None:
    leave_id = int(payload["leave_id"])
    roster_month_id = int(roster_month["roster_month_id"])
    now = now_str()
    cursor.execute(
        """
        UPDATE roster_leave SET is_active=0, updated_date=%s
        WHERE leave_id=%s AND roster_month_id=%s
        """,
        (now, leave_id, roster_month_id),
    )
    _clear_leave_from_days(cursor, roster_month_id, leave_id)


def _clear_leave_from_days(cursor, roster_month_id: int, leave_id: int) -> None:
    cursor.execute(
        """
        SELECT rd.roster_day_id, rd.roster_date, rd.holiday_id,
               rd.working_type, rd.working_hours, rl.is_half_day,
               rm.user_id, rm.month_year
        FROM roster_day rd
        JOIN roster_month rm ON rm.roster_month_id = rd.roster_month_id
        LEFT JOIN roster_leave rl ON rl.leave_id = rd.leave_id
        WHERE rd.roster_month_id=%s AND rd.leave_id=%s AND rd.is_active=1
        """,
        (roster_month_id, int(leave_id)),
    )
    rows = cursor.fetchall() or []
    now = now_str()
    for row in rows:
        d = parse_date(row.get("roster_date"))
        if not d:
            continue
        if row.get("holiday_id"):
            new_type = "Holiday"
        elif d.weekday() >= 5:
            new_type = "WeekOff"
        else:
            new_type = "Working"

        # Restore full day when the leave was half-day (working_type was flipped to Half)
        was_half_leave = bool(int(row.get("is_half_day") or 0))
        working_type = row.get("working_type") or "Full"
        working_hours = row.get("working_hours")
        if was_half_leave and (working_type or "").strip() == "Half":
            working_type = "Full"
            try:
                working_hours = round(float(working_hours or HALF_DAY_HOURS) * 2, 2)
            except (TypeError, ValueError):
                working_hours = FULL_DAY_HOURS

        if new_type == "Working" and (working_hours is None or float(working_hours or 0) <= 0):
            baseline = load_user_monthly_tracker_baseline(
                cursor, int(row["user_id"]), str(row.get("month_year") or "")
            )
            working_hours = float(baseline.get("daily_full_hours") or FULL_DAY_HOURS)
            working_type = "Full"

        cursor.execute(
            """
            UPDATE roster_day
            SET day_type=%s, leave_id=NULL, working_type=%s, working_hours=%s, updated_date=%s
            WHERE roster_day_id=%s
            """,
            (
                new_type,
                working_type,
                working_hours,
                now,
                int(row["roster_day_id"]),
            ),
        )

        if row.get("user_id") and new_type in ("Working", "WeekOff", "Holiday"):
            _sync_temp_qc_assigned_for_day(
                cursor,
                user_id=int(row["user_id"]),
                roster_date=d,
                day_type=new_type,
                working_type=working_type,
                is_half_leave=False,
            )


def _apply_leave_to_days(cursor, roster_month_id: int, leave_id: int, payload: dict) -> None:
    start = parse_date(payload.get("start_date"))
    end = parse_date(payload.get("end_date"))
    if not start or not end:
        raise ValueError("Invalid leave dates")

    cursor.execute(
        "SELECT user_id FROM roster_month WHERE roster_month_id=%s LIMIT 1",
        (int(roster_month_id),),
    )
    month_row = cursor.fetchone() or {}
    user_id = month_row.get("user_id")

    is_half = bool(int(payload.get("is_half_day") or 0))
    now = now_str()
    for d in iter_dates_inclusive(start, end):
        date_str = d.isoformat()
        if is_half:
            cursor.execute(
                """
                SELECT roster_day_id, working_type, working_hours
                FROM roster_day
                WHERE roster_month_id=%s
                  AND DATE(roster_date)=DATE(%s)
                  AND is_active=1
                  AND day_type IN ('Working', 'Leave')
                LIMIT 1
                """,
                (int(roster_month_id), date_str),
            )
            day_row = cursor.fetchone()
            if not day_row:
                continue
            half_hours = half_day_hours_from_roster_day(
                day_row.get("working_type"), day_row.get("working_hours")
            )
            cursor.execute(
                """
                UPDATE roster_day
                SET day_type='Leave',
                    leave_id=%s,
                    working_type='Half',
                    working_hours=%s,
                    updated_date=%s
                WHERE roster_day_id=%s
                """,
                (int(leave_id), half_hours, now, int(day_row["roster_day_id"])),
            )
            if user_id:
                _sync_temp_qc_assigned_for_day(
                    cursor,
                    user_id=int(user_id),
                    roster_date=d,
                    day_type="Leave",
                    working_type="Half",
                    is_half_leave=True,
                )
        else:
            cursor.execute(
                """
                UPDATE roster_day
                SET day_type='Leave', leave_id=%s, updated_date=%s
                WHERE roster_month_id=%s
                  AND DATE(roster_date)=DATE(%s)
                  AND is_active=1
                  AND day_type IN ('Working', 'Leave')
                """,
                (int(leave_id), now, int(roster_month_id), date_str),
            )
            if user_id:
                _sync_temp_qc_assigned_for_day(
                    cursor,
                    user_id=int(user_id),
                    roster_date=d,
                    day_type="Leave",
                    working_type="Full",
                    is_half_leave=False,
                )


def apply_extra_hours_update(cursor, roster_month: dict, payload: dict) -> None:
    roster_month_id = int(roster_month["roster_month_id"])
    extra = float(payload.get("extra_assigned_hours", 0))
    cursor.execute(
        """
        UPDATE roster_month
        SET extra_assigned_hours=%s, updated_date=%s
        WHERE roster_month_id=%s
        """,
        (extra, now_str(), roster_month_id),
    )
    cursor.execute(
        """
        UPDATE user_monthly_tracker
        SET extra_assigned_hours=%s
        WHERE user_id=%s AND month_year=%s AND is_active=1
        """,
        (extra, int(roster_month["user_id"]), str(roster_month["month_year"]).strip()),
    )


def apply_change_request(cursor, roster_month: dict, request_row: dict, performed_by: int) -> None:
    change_type = request_row.get("change_type")
    payload = request_row.get("change_payload")
    if isinstance(payload, str):
        payload = json.loads(payload)

    if change_type == "DAY_UPDATE":
        apply_day_update(cursor, roster_month, payload)
    elif change_type == "WEEKOFF_SWAP":
        apply_weekoff_swap(cursor, roster_month, payload)
    elif change_type == "LEAVE_ADD":
        apply_leave_add(cursor, roster_month, payload, performed_by)
    elif change_type == "LEAVE_UPDATE":
        apply_leave_update(cursor, roster_month, payload)
    elif change_type == "LEAVE_DELETE":
        apply_leave_delete(cursor, roster_month, payload)
    elif change_type == "EXTRA_HOURS_UPDATE":
        apply_extra_hours_update(cursor, roster_month, payload)
    else:
        raise ValueError(f"Unsupported change_type: {change_type}")


def refresh_roster_month_metrics(cursor, roster_month_id: int) -> dict:
    leaves = get_roster_leaves(cursor, roster_month_id)
    for leave in leaves:
        if not int(leave.get("is_active", 1)):
            continue
        _apply_leave_to_days(
            cursor,
            int(roster_month_id),
            int(leave["leave_id"]),
            {
                "start_date": leave.get("start_date"),
                "end_date": leave.get("end_date"),
                "is_half_day": leave.get("is_half_day") or 0,
            },
        )
    days = get_roster_days(cursor, roster_month_id)
    days = apply_active_leaves_to_days(days, leaves)
    metrics = recalculate_metrics_from_days_and_leaves(days, leaves)
    cursor.execute(
        """
        UPDATE roster_month
        SET calendar_working_days=%s,
            target_working_days=%s,
            monthly_target_hours=%s,
            updated_date=%s
        WHERE roster_month_id=%s
        """,
        (
            metrics["calendar_working_days"],
            metrics["target_working_days"],
            metrics["monthly_target_hours"],
            now_str(),
            int(roster_month_id),
        ),
    )
    return metrics


def reconcile_month_goals_to_universal(cursor, month_year: str, performed_by: int) -> dict:
    """
    Recalculate every active roster in the month and write User Monthly Goal
    from those metrics (week-offs do not reduce target; extras do not raise it).
    """
    cursor.execute(
        """
        SELECT roster_month_id
        FROM roster_month
        WHERE is_active=1 AND month_year=%s
        """,
        (str(month_year).strip(),),
    )
    ids = [int(r["roster_month_id"]) for r in (cursor.fetchall() or [])]
    synced = 0
    for roster_month_id in ids:
        refresh_roster_month_metrics(cursor, roster_month_id)
        roster_month = get_roster_month(cursor, roster_month_id)
        if not roster_month:
            continue
        sync_to_user_monthly_tracker(
            cursor,
            roster_month,
            "Monthly goal anchored to universal working days",
            int(performed_by),
            write_audit=False,
        )
        synced += 1
    cap_result = cap_month_goals_to_universal_working_days(cursor, month_year)
    cap_result["goals_synced"] = synced
    return cap_result


def save_version_snapshot(
    cursor,
    roster_month: dict,
    days: list[dict],
    leaves: list[dict],
    approved_by: int,
    reviewer_comment: str,
) -> int:
    snapshot = build_month_snapshot(roster_month, days, leaves)
    now = now_str()
    cursor.execute(
        """
        INSERT INTO roster_version_snapshot (
            roster_month_id, roster_version, snapshot_json,
            approved_by, approved_date, reviewer_comment,
            production_synced_at, created_date
        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
        """,
        (
            int(roster_month["roster_month_id"]),
            int(roster_month["roster_version"]),
            dumps_json_safe(snapshot),
            int(approved_by),
            now,
            reviewer_comment,
            roster_month.get("production_synced_at"),
            now,
        ),
    )
    return int(cursor.lastrowid)


def finalize_approved_cycle(
    cursor,
    roster_month_id: int,
    approved_by: int,
    cycle_reviewer_comment: str,
) -> dict:
    roster_month = get_roster_month(cursor, roster_month_id)
    if not roster_month:
        raise ValueError("Roster month not found")

    refresh_roster_month_metrics(cursor, roster_month_id)
    roster_month = get_roster_month(cursor, roster_month_id)

    new_version = int(roster_month.get("roster_version") or 1) + 1
    now = now_str()

    cursor.execute(
        """
        UPDATE roster_month
        SET roster_version=%s, status='Approved',
            last_approved_by=%s, last_approved_date=%s,
            updated_date=%s
        WHERE roster_month_id=%s
        """,
        (new_version, int(approved_by), now, now, int(roster_month_id)),
    )
    roster_month = get_roster_month(cursor, roster_month_id)
    days = get_roster_days(cursor, roster_month_id)
    leaves = get_roster_leaves(cursor, roster_month_id)

    sync_to_user_monthly_tracker(cursor, roster_month, cycle_reviewer_comment, approved_by)
    roster_month = get_roster_month(cursor, roster_month_id)

    version_id = save_version_snapshot(
        cursor, roster_month, days, leaves, approved_by, cycle_reviewer_comment
    )

    write_audit_log(
        cursor,
        roster_month_id=int(roster_month_id),
        user_id=int(roster_month["user_id"]),
        action="ROSTER_VERSION_APPROVED",
        entity_type="roster_version_snapshot",
        entity_id=version_id,
        old_value={"previous_version": new_version - 1},
        new_value={"roster_version": new_version},
        performed_by=int(approved_by),
        approval_status="Approved",
        notes=cycle_reviewer_comment,
    )

    return {
        "roster_month_id": int(roster_month_id),
        "roster_version": new_version,
        "version_id": version_id,
    }


def get_pending_requests_for_batch(cursor, batch_id: str) -> list[dict]:
    cursor.execute(
        """
        SELECT *
        FROM roster_change_request
        WHERE batch_id=%s AND is_active=1
        ORDER BY request_id ASC
        """,
        (batch_id,),
    )
    rows = cursor.fetchall() or []
    for row in rows:
        if isinstance(row.get("change_payload"), str):
            try:
                row["change_payload"] = json.loads(row["change_payload"])
            except Exception:
                pass
    return rows


def batch_processing_complete(cursor, batch_id: str) -> bool:
    cursor.execute(
        """
        SELECT COUNT(*) AS cnt
        FROM roster_change_request
        WHERE batch_id=%s AND is_active=1 AND status='Pending'
        """,
        (batch_id,),
    )
    row = cursor.fetchone() or {}
    return int(row.get("cnt") or 0) == 0


def count_batch_approved(cursor, batch_id: str) -> int:
    cursor.execute(
        """
        SELECT COUNT(*) AS cnt
        FROM roster_change_request
        WHERE batch_id=%s AND is_active=1 AND status='Approved'
        """,
        (batch_id,),
    )
    return int((cursor.fetchone() or {}).get("cnt") or 0)


def new_batch_id() -> str:
    return str(uuid.uuid4())
