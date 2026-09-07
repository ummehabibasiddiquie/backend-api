# routes/roster_workflow.py
# Phase 2 workflow routes — registered on roster_bp from routes.roster

from __future__ import annotations

import io
import json
from datetime import date, datetime

from flask import request, send_file

from config import get_db_connection
from utils.response import api_response
from utils.roster_excel import (
    build_month_workbook,
    build_template_workbook,
    day_to_excel_label,
    excel_label_to_change,
    adjust_excel_change_for_holiday,
    is_noop_change,
    match_employee_name,
    monday_of_week,
    month_years_for_dates,
    parse_roster_excel,
    resolve_roster_month_for_date,
    week_dates,
    weeks_in_month,
)
from utils.roster_helpers import (
    can_lock_roster_month,
    roster_lock_not_before_message,
    month_calendar_has_lock,
    month_calendar_lock_message,
    can_manage_roster_employees,
    get_eligible_employees,
    get_excel_roster_employees,
    get_role_context,
    is_admin_or_super_admin,
    is_super_admin,
    is_self_read_only_roster_role,
    can_lock_unlock_roster,
    month_year_label,
    parse_date,
    parse_month_year,
    require_logged_in_user,
    write_audit_log,
    now_str,
)
from utils.roster_workflow import (
    EDITABLE_STATUSES,
    SUBMITTABLE_STATUSES,
    apply_change_request,
    assert_manager_scope,
    _assert_leave_not_on_holiday,
    batch_processing_complete,
    can_approve_reject,
    can_create_change_requests,
    count_batch_approved,
    create_change_request,
    create_or_update_leave_request,
    dedupe_change_request_rows,
    lock_roster_month_record,
    unlock_roster_month_record,
    count_pending_change_requests_for_month,
    cancel_pending_requests_for_roster,
    cancel_duplicate_pending_leave_requests,
    finalize_approved_cycle,
    get_pending_requests_for_batch,
    get_roster_days,
    get_roster_leaves,
    get_roster_month,
    is_month_locked,
    new_batch_id,
    refresh_roster_month_metrics,
    weekoff_swap_preview,
)
from utils.roster_week_lock import (
    annotate_weeks_with_locks,
    get_week_lock,
    list_week_locks,
    lock_week,
    lock_weeks_touched_by_requests,
    unlock_week,
    week_lock_message_for_change,
    week_lock_message_for_dates,
    week_meta_for_date,
    week_meta_for_number,
    weeks_touched_by_requests,
    month_has_pending_submitted_requests,
    weeks_from_approved_requests_for_months,
    dates_from_change_request,
)
from utils.roster_week_email import (
    send_weekly_roster_after_approval,
    send_roster_approval_needed_email,
)

from routes.roster import roster_bp


def _require_logged_in_user(data: dict) -> tuple[int | None, dict | None]:
    """Wrapper around shared utility for consistency."""
    user_id, err = require_logged_in_user(data)
    if err:
        return None, api_response(err.get("status", 400), err.get("error", "Authentication required"))
    return user_id, None


def _load_request(cursor, request_id: int) -> dict | None:
    cursor.execute(
        "SELECT * FROM roster_change_request WHERE request_id=%s AND is_active=1",
        (int(request_id),),
    )
    row = cursor.fetchone()
    if row and isinstance(row.get("change_payload"), str):
        try:
            row["change_payload"] = json.loads(row["change_payload"])
        except Exception:
            pass
    return row


def _validate_month_editable(cursor, roster_month: dict) -> tuple[bool, str]:
    month_year = (roster_month.get("month_year") or "").strip()
    if month_year:
        lock_msg = month_calendar_lock_message(cursor, month_year)
        if lock_msg:
            return False, lock_msg
    if is_month_locked(roster_month.get("status", "")):
        locker = (roster_month.get("locked_by_name") or "").strip() or "an administrator"
        return False, f"This roster is locked by {locker} and cannot be changed"
    if not can_create_change_requests(roster_month.get("status", "")):
        return False, f"Changes not allowed in status {roster_month.get('status')}"
    return True, ""


def _validate_change_dates_editable(
    cursor,
    roster_month: dict,
    change_type: str,
    change_payload: dict | None,
) -> tuple[bool, str]:
    """Month-level + per-week lock checks for a specific change."""
    ok, msg = _validate_month_editable(cursor, roster_month)
    if not ok:
        return False, msg
    month_year = (roster_month.get("month_year") or "").strip()
    if month_year:
        week_msg = week_lock_message_for_change(
            cursor, month_year, change_type, change_payload or {}
        )
        if week_msg:
            return False, week_msg
    return True, ""


@roster_bp.route("/weekoff/swap_preview", methods=["POST"])
def roster_weekoff_swap_preview():
    data = request.get_json(silent=True) or {}
    logged_in_user_id, err = _require_logged_in_user(data)
    if err:
        return err

    roster_month_id = data.get("roster_month_id")
    new_week_off_dates = data.get("week_off_dates") or []
    if not roster_month_id:
        return api_response(400, "roster_month_id is required")

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        ctx = get_role_context(cursor, logged_in_user_id)
        role_name = ctx.get("user_role_name", "")
        if not can_manage_roster_employees(role_name):
            return api_response(403, "You do not have permission to manage rosters")

        roster_month = get_roster_month(cursor, int(roster_month_id))
        if not roster_month:
            return api_response(404, "Roster month not found")

        scope_err = assert_manager_scope(cursor, logged_in_user_id, role_name, roster_month)
        if scope_err:
            return api_response(403, scope_err)

        ok, msg = _validate_month_editable(cursor, roster_month)
        if not ok:
            return api_response(400, msg)

        month_year = (roster_month.get("month_year") or "").strip()
        if month_year:
            week_msg = week_lock_message_for_dates(
                cursor,
                month_year,
                [parse_date(d) for d in (new_week_off_dates or []) if parse_date(d)],
            )
            if week_msg:
                return api_response(400, week_msg)

        preview = weekoff_swap_preview(cursor, int(roster_month_id), new_week_off_dates)
        return api_response(200, "Week-off swap preview generated", preview)
    except Exception as e:
        return api_response(500, f"Week-off swap preview failed: {str(e)}")
    finally:
        cursor.close()
        conn.close()


@roster_bp.route("/change_request/create", methods=["POST"])
def roster_create_change_request():
    data = request.get_json(silent=True) or {}
    logged_in_user_id, err = _require_logged_in_user(data)
    if err:
        return err

    roster_month_id = data.get("roster_month_id")
    change_type = (data.get("change_type") or "").strip()
    change_payload = data.get("change_payload") or {}

    if not roster_month_id or not change_type:
        return api_response(400, "roster_month_id and change_type are required")

    allowed_types = {
        "DAY_UPDATE",
        "WEEKOFF_SWAP",
        "LEAVE_ADD",
        "LEAVE_UPDATE",
        "LEAVE_DELETE",
        "EXTRA_HOURS_UPDATE",
    }
    if change_type not in allowed_types:
        return api_response(400, f"Invalid change_type. Allowed: {sorted(allowed_types)}")

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        ctx = get_role_context(cursor, logged_in_user_id)
        role_name = ctx.get("user_role_name", "")
        if not can_manage_roster_employees(role_name):
            return api_response(403, "You do not have permission to manage rosters")

        roster_month = get_roster_month(cursor, int(roster_month_id))
        if not roster_month:
            return api_response(404, "Roster month not found")

        cursor.execute(
            "SELECT joining_date, user_name FROM tfs_user WHERE user_id=%s LIMIT 1",
            (int(roster_month["user_id"]),),
        )
        emp_row = cursor.fetchone() or {}
        if not emp_row.get("joining_date"):
            return api_response(
                400,
                f"Cannot edit roster for {emp_row.get('user_name') or 'this employee'}: "
                "joining date is not set",
            )

        scope_err = assert_manager_scope(cursor, logged_in_user_id, role_name, roster_month)
        if scope_err:
            return api_response(403, scope_err)

        ok, msg = _validate_change_dates_editable(
            cursor, roster_month, change_type, change_payload
        )
        if not ok:
            return api_response(400, msg)

        if roster_month.get("status") == "Pending Approval":
            return api_response(400, "Withdraw submission before creating new change requests")

        if change_type in ("LEAVE_ADD", "LEAVE_UPDATE"):
            try:
                _assert_leave_not_on_holiday(
                    cursor,
                    int(roster_month_id),
                    change_payload.get("start_date"),
                    change_payload.get("end_date"),
                )
            except ValueError as ve:
                return api_response(400, str(ve))
            request_id, created = create_or_update_leave_request(
                cursor,
                roster_month_id=int(roster_month_id),
                user_id=int(roster_month["user_id"]),
                change_type=change_type,
                change_payload=change_payload,
                submitted_by=logged_in_user_id,
            )
            conn.commit()
            message = "Change request created" if created else "Existing pending leave request updated"
            return api_response(201 if created else 200, message, {"request_id": request_id})

        request_id = create_change_request(
            cursor,
            roster_month_id=int(roster_month_id),
            user_id=int(roster_month["user_id"]),
            change_type=change_type,
            change_payload=change_payload,
            submitted_by=logged_in_user_id,
            batch_id=None,
        )
        conn.commit()
        return api_response(201, "Change request created", {"request_id": request_id})
    except Exception as e:
        conn.rollback()
        return api_response(500, f"Create change request failed: {str(e)}")
    finally:
        cursor.close()
        conn.close()


@roster_bp.route("/change_request/withdraw_draft", methods=["POST"])
def roster_withdraw_draft_change_request():
    """
    Withdraw a draft change request (Pending, not yet submitted in a batch)
    so a mistaken leave/week-off request can be removed before submit.
    """
    data = request.get_json(silent=True) or {}
    logged_in_user_id, err = _require_logged_in_user(data)
    if err:
        return err

    request_id = data.get("request_id")
    if not request_id:
        return api_response(400, "request_id is required")

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        ctx = get_role_context(cursor, logged_in_user_id)
        role_name = ctx.get("user_role_name", "")
        if not can_manage_roster_employees(role_name):
            return api_response(403, "You do not have permission to withdraw draft requests")

        cursor.execute(
            """
            SELECT rcr.*, rm.month_year, rm.user_id AS roster_user_id, rm.status AS roster_status
            FROM roster_change_request rcr
            JOIN roster_month rm ON rm.roster_month_id = rcr.roster_month_id
            WHERE rcr.request_id=%s AND rcr.is_active=1
            LIMIT 1
            """,
            (int(request_id),),
        )
        req = cursor.fetchone()
        if not req:
            return api_response(404, "Change request not found")

        if req.get("status") != "Pending":
            return api_response(400, "Only pending draft requests can be withdrawn")
        if req.get("batch_id"):
            return api_response(
                400,
                "This request is already submitted for approval. Use Withdraw Submission instead.",
            )
        if int(req.get("submitted_by") or 0) != int(logged_in_user_id) and not is_admin_or_super_admin(
            role_name
        ):
            return api_response(403, "You can only withdraw draft requests you created")

        roster_month = get_roster_month(cursor, int(req["roster_month_id"]))
        if roster_month:
            scope_err = assert_manager_scope(cursor, logged_in_user_id, role_name, roster_month)
            if scope_err and not is_admin_or_super_admin(role_name):
                return api_response(403, scope_err)

        now = now_str()
        cursor.execute(
            """
            UPDATE roster_change_request
            SET is_active=0,
                status='Cancelled due to Withdrawal',
                reviewer_comment=%s,
                reviewed_by=%s,
                reviewed_date=%s,
                batch_id=NULL
            WHERE request_id=%s AND is_active=1
            """,
            (
                data.get("withdraw_comment") or "Draft request withdrawn before submit",
                logged_in_user_id,
                now,
                int(request_id),
            ),
        )

        write_audit_log(
            cursor,
            roster_month_id=int(req["roster_month_id"]),
            user_id=int(req["user_id"]),
            action="CHANGE_REQUEST_DRAFT_WITHDRAWN",
            entity_type="roster_change_request",
            entity_id=int(request_id),
            old_value={"status": "Pending", "batch_id": None},
            new_value={"status": "Cancelled due to Withdrawal", "is_active": 0},
            performed_by=logged_in_user_id,
            notes=data.get("withdraw_comment"),
        )

        conn.commit()
        return api_response(
            200,
            "Draft change request withdrawn",
            {"request_id": int(request_id)},
        )
    except Exception as e:
        conn.rollback()
        return api_response(500, f"Draft withdraw failed: {str(e)}")
    finally:
        cursor.close()
        conn.close()


@roster_bp.route("/submit", methods=["POST"])
def roster_submit_batch():
    """PM/AM/Admin submit one batch for employees in scope (M)."""
    data = request.get_json(silent=True) or {}
    logged_in_user_id, err = _require_logged_in_user(data)
    if err:
        return err

    month_year = (data.get("month_year") or "").strip()
    if not month_year:
        return api_response(400, "month_year is required")

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        ctx = get_role_context(cursor, logged_in_user_id)
        role_name = ctx.get("user_role_name", "")
        if not can_manage_roster_employees(role_name):
            return api_response(403, "You do not have permission to submit rosters")

        lock_msg = month_calendar_lock_message(cursor, month_year)
        if lock_msg:
            return api_response(400, lock_msg)

        year, month = parse_month_year(month_year)
        employees = get_eligible_employees(cursor, logged_in_user_id, role_name, year, month)
        employee_ids = [int(e["user_id"]) for e in employees]

        if not employee_ids:
            return api_response(400, "No employees in scope for submission")

        placeholders = ",".join(["%s"] * len(employee_ids))
        cursor.execute(
            f"""
            SELECT rcr.request_id, rcr.roster_month_id
            FROM roster_change_request rcr
            JOIN roster_month rm ON rm.roster_month_id = rcr.roster_month_id
            WHERE rm.month_year=%s
              AND rm.is_active=1
              AND rm.user_id IN ({placeholders})
              AND rm.status IN ('Draft', 'Approved')
              AND rcr.status='Pending'
              AND rcr.is_active=1
              AND (rcr.batch_id IS NULL OR rcr.batch_id='')
            """,
            tuple([month_year, *employee_ids]),
        )
        pending_rows = cursor.fetchall() or []
        if not pending_rows:
            return api_response(400, "No pending change requests to submit in scope")

        batch_id = new_batch_id()
        now = now_str()
        request_ids = [int(r["request_id"]) for r in pending_rows]
        month_ids = sorted({int(r["roster_month_id"]) for r in pending_rows})

        req_placeholders = ",".join(["%s"] * len(request_ids))
        cursor.execute(
            f"""
            UPDATE roster_change_request
            SET batch_id=%s, submitted_by=%s, submitted_date=%s
            WHERE request_id IN ({req_placeholders})
            """,
            tuple([batch_id, logged_in_user_id, now, *request_ids]),
        )

        month_placeholders = ",".join(["%s"] * len(month_ids))
        cursor.execute(
            f"""
            UPDATE roster_month
            SET status='Pending Approval',
                submitted_by=%s,
                submitted_date=%s,
                updated_date=%s
            WHERE roster_month_id IN ({month_placeholders})
              AND is_active=1
              AND status IN ('Draft', 'Approved')
            """,
            tuple([logged_in_user_id, now, now, *month_ids]),
        )

        attached = len(request_ids)

        write_audit_log(
            cursor,
            roster_month_id=None,
            user_id=None,
            action="ROSTER_BATCH_SUBMITTED",
            entity_type="roster_change_request",
            entity_id=None,
            old_value=None,
            new_value={"batch_id": batch_id, "month_year": month_year, "request_count": attached},
            performed_by=logged_in_user_id,
            approval_status="Pending",
            notes=data.get("submitter_comment"),
        )

        conn.commit()

        notify = {}
        try:
            notify = send_roster_approval_needed_email(
                cursor,
                month_year=month_year,
                submitted_by=logged_in_user_id,
                request_count=attached,
            )
        except Exception as mail_err:
            print(f"[roster approval email] submit notify failed: {mail_err}", flush=True)
            notify = {"sent": False, "reason": str(mail_err)}

        message = "Roster batch submitted for approval"
        if notify.get("sent"):
            message = "Roster submitted for approval. Admin and Super Admin have been notified."

        return api_response(
            200,
            message,
            {"batch_id": batch_id, "request_count": attached, "approval_notify": notify},
        )
    except Exception as e:
        conn.rollback()
        return api_response(500, f"Submit failed: {str(e)}")
    finally:
        cursor.close()
        conn.close()


@roster_bp.route("/withdraw", methods=["POST"])
def roster_withdraw_batch():
    """B3 — withdraw submission back to Draft; cancel pending batch requests (W1 default)."""
    data = request.get_json(silent=True) or {}
    logged_in_user_id, err = _require_logged_in_user(data)
    if err:
        return err

    batch_id = (data.get("batch_id") or "").strip()
    month_year = (data.get("month_year") or "").strip()
    if not batch_id and not month_year:
        return api_response(400, "batch_id or month_year is required")

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        ctx = get_role_context(cursor, logged_in_user_id)
        role_name = ctx.get("user_role_name", "")
        if not can_manage_roster_employees(role_name):
            return api_response(403, "You do not have permission to withdraw submissions")

        now = now_str()

        if batch_id:
            cursor.execute(
                """
                SELECT request_id, roster_month_id
                FROM roster_change_request
                WHERE batch_id=%s AND status='Pending' AND is_active=1
                  AND submitted_by=%s
                """,
                (batch_id, logged_in_user_id),
            )
        else:
            cursor.execute(
                """
                SELECT rcr.request_id, rcr.roster_month_id
                FROM roster_change_request rcr
                JOIN roster_month rm ON rm.roster_month_id = rcr.roster_month_id
                WHERE rm.month_year=%s AND rm.status='Pending Approval'
                  AND rcr.status='Pending' AND rcr.is_active=1
                  AND rcr.submitted_by=%s
                  AND rcr.batch_id IS NOT NULL AND rcr.batch_id != ''
                """,
                (month_year, logged_in_user_id),
            )

        rows = cursor.fetchall() or []
        if not rows:
            conn.rollback()
            return api_response(
                403,
                "You can only withdraw submissions that you submitted for approval",
            )

        request_ids = [int(row["request_id"]) for row in rows]
        month_ids = sorted({int(row["roster_month_id"]) for row in rows})
        withdraw_comment = data.get("withdraw_comment") or "Withdrawn by submitter"

        req_placeholders = ",".join(["%s"] * len(request_ids))
        cursor.execute(
            f"""
            UPDATE roster_change_request
            SET status='Cancelled due to Withdrawal',
                reviewer_comment=%s,
                reviewed_by=%s,
                reviewed_date=%s,
                batch_id=NULL
            WHERE request_id IN ({req_placeholders})
            """,
            tuple([withdraw_comment, logged_in_user_id, now, *request_ids]),
        )

        month_placeholders = ",".join(["%s"] * len(month_ids))
        cursor.execute(
            f"""
            UPDATE roster_month
            SET status='Draft', submitted_by=NULL, submitted_date=NULL, updated_date=%s
            WHERE roster_month_id IN ({month_placeholders})
              AND status='Pending Approval'
              AND submitted_by=%s
            """,
            tuple([now, *month_ids, logged_in_user_id]),
        )

        write_audit_log(
            cursor,
            roster_month_id=None,
            user_id=None,
            action="ROSTER_BATCH_WITHDRAWN",
            entity_type="roster_change_request",
            entity_id=None,
            old_value={"batch_id": batch_id or month_year},
            new_value={"cancelled_requests": len(rows)},
            performed_by=logged_in_user_id,
            notes=data.get("withdraw_comment"),
        )

        conn.commit()
        return api_response(200, "Submission withdrawn to Draft", {"cancelled_requests": len(rows)})
    except Exception as e:
        conn.rollback()
        return api_response(500, f"Withdraw failed: {str(e)}")
    finally:
        cursor.close()
        conn.close()


def _process_batch_completion(cursor, batch_id: str, reviewer_comment: str, logged_in_user_id: int) -> list[dict]:
    if not batch_processing_complete(cursor, batch_id):
        return []

    cursor.execute(
        """
        SELECT DISTINCT roster_month_id
        FROM roster_change_request
        WHERE batch_id=%s AND status='Approved' AND is_active=1
        """,
        (batch_id,),
    )
    approved_month_ids = {int(r["roster_month_id"]) for r in (cursor.fetchall() or [])}

    finalized = []
    for mid in approved_month_ids:
        result = finalize_approved_cycle(cursor, mid, logged_in_user_id, reviewer_comment)
        cursor.execute(
            """
            UPDATE roster_change_request
            SET roster_version_applied=%s
            WHERE batch_id=%s AND roster_month_id=%s AND status='Approved'
            """,
            (result["roster_version"], batch_id, mid),
        )
        finalized.append(result)

    cursor.execute(
        """
        SELECT DISTINCT rm.roster_month_id
        FROM roster_month rm
        JOIN roster_change_request rcr ON rcr.roster_month_id = rm.roster_month_id
        WHERE rcr.batch_id=%s AND rm.status='Pending Approval'
        """,
        (batch_id,),
    )
    now = now_str()
    for row in cursor.fetchall() or []:
        mid = int(row["roster_month_id"])
        if mid in approved_month_ids:
            cursor.execute(
                "UPDATE roster_month SET status='Approved', updated_date=%s WHERE roster_month_id=%s",
                (now, mid),
            )
        else:
            cursor.execute(
                "UPDATE roster_month SET status='Draft', updated_date=%s WHERE roster_month_id=%s",
                (now, mid),
            )

    return finalized


def _merge_week_lists(*lists: list[dict] | None) -> list[dict]:
    seen: set[tuple[str, int]] = set()
    out: list[dict] = []
    for weeks in lists:
        for week in weeks or []:
            key = (str(week.get("month_year") or ""), int(week.get("week_number") or 0))
            if not key[0] or key[1] <= 0 or key in seen:
                continue
            seen.add(key)
            out.append(week)
    return out


def _send_roster_email_after_review(
    cursor,
    *,
    month_years: list[str],
    fallback_requests: list[dict],
    months_by_id: dict[int, dict],
    locked_weeks: list[dict] | None,
    logged_in_user_id: int,
    role_name: str,
) -> list[dict]:
    """
    Do not email while the month still has pending items on the approval queue.
    After the admin finishes (queue empty), send one mail per affected week.
    """
    months = [str(m).strip() for m in (month_years or []) if str(m).strip()]
    if not months:
        return [{"skipped": True, "sent": False, "reason": "No month on requests"}]
    if month_has_pending_submitted_requests(cursor, months):
        return [
            {
                "skipped": True,
                "sent": False,
                "deferred": True,
                "reason": "Weekly roster email waits until all pending requests are approved or rejected",
            }
        ]
    weeks = _merge_week_lists(
        weeks_from_approved_requests_for_months(cursor, months),
        weeks_touched_by_requests(fallback_requests, months_by_id),
        locked_weeks,
    )
    if not weeks:
        return [{"skipped": True, "sent": False, "reason": "No weeks found to email"}]
    return send_weekly_roster_after_approval(
        cursor,
        weeks=weeks,
        logged_in_user_id=logged_in_user_id,
        role_name=role_name,
    )


def _approve_single_request(
    cursor,
    *,
    req: dict,
    logged_in_user_id: int,
    reviewer_comment: str | None,
    roster_month: dict | None = None,
    refresh_metrics: bool = True,
) -> str | None:
    """
    Apply + mark one pending submitted request as Approved.
    Returns batch_id (if any) for later batch completion.

    For bulk approve, pass refresh_metrics=False and refresh each
    touched roster_month once after the loop.
    """
    if req.get("status") != "Pending":
        raise ValueError(f"Request {req.get('request_id')} is not pending (status={req.get('status')})")
    if not req.get("batch_id"):
        raise ValueError(f"Request {req.get('request_id')} has not been submitted for approval yet")

    if roster_month is None:
        roster_month = get_roster_month(cursor, int(req["roster_month_id"]))
    if not roster_month:
        raise ValueError(f"Roster month not found for request {req.get('request_id')}")

    request_id = int(req["request_id"])
    batch_id = req.get("batch_id")
    now = now_str()

    apply_change_request(cursor, roster_month, req, logged_in_user_id)
    cancel_duplicate_pending_leave_requests(
        cursor, approved_request=req, reviewed_by=logged_in_user_id
    )
    if refresh_metrics:
        refresh_roster_month_metrics(cursor, int(req["roster_month_id"]))

    cursor.execute(
        """
        UPDATE roster_change_request
        SET status='Approved', reviewer_comment=%s, rejection_reason=NULL,
            reviewed_by=%s, reviewed_date=%s, applied_at=%s
        WHERE request_id=%s
        """,
        (reviewer_comment, logged_in_user_id, now, now, request_id),
    )

    write_audit_log(
        cursor,
        roster_month_id=int(req["roster_month_id"]),
        user_id=int(req["user_id"]),
        action="CHANGE_REQUEST_APPROVED",
        entity_type="roster_change_request",
        entity_id=request_id,
        old_value={"status": "Pending"},
        new_value={"status": "Approved", "reviewer_comment": reviewer_comment},
        performed_by=logged_in_user_id,
        approval_status="Approved",
        notes=reviewer_comment,
    )
    return batch_id


def _load_requests_by_ids(cursor, request_ids: list[int]) -> dict[int, dict]:
    """Prefetch many change requests in one query."""
    if not request_ids:
        return {}
    placeholders = ",".join(["%s"] * len(request_ids))
    cursor.execute(
        f"""
        SELECT *
        FROM roster_change_request
        WHERE is_active=1 AND request_id IN ({placeholders})
        """,
        tuple(int(x) for x in request_ids),
    )
    out: dict[int, dict] = {}
    for row in cursor.fetchall() or []:
        if isinstance(row.get("change_payload"), str):
            try:
                row["change_payload"] = json.loads(row["change_payload"])
            except Exception:
                pass
        out[int(row["request_id"])] = row
    return out


def _load_roster_months_by_ids(cursor, roster_month_ids: list[int]) -> dict[int, dict]:
    if not roster_month_ids:
        return {}
    unique = list(dict.fromkeys(int(x) for x in roster_month_ids))
    placeholders = ",".join(["%s"] * len(unique))
    cursor.execute(
        f"""
        SELECT *
        FROM roster_month
        WHERE roster_month_id IN ({placeholders})
        """,
        tuple(unique),
    )
    return {int(r["roster_month_id"]): r for r in (cursor.fetchall() or [])}


@roster_bp.route("/requests/approve", methods=["POST"])
def roster_approve_request():
    data = request.get_json(silent=True) or {}
    logged_in_user_id, err = _require_logged_in_user(data)
    if err:
        return err

    request_id = data.get("request_id")
    reviewer_comment = (data.get("reviewer_comment") or "").strip() or None
    if not request_id:
        return api_response(400, "request_id is required")

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        ctx = get_role_context(cursor, logged_in_user_id)
        role_name = ctx.get("user_role_name", "")
        if not can_approve_reject(role_name):
            return api_response(403, "Only Admin or Super Admin can approve requests")

        req = _load_request(cursor, int(request_id))
        if not req:
            return api_response(404, "Change request not found")

        batch_id = _approve_single_request(
            cursor,
            req=req,
            logged_in_user_id=logged_in_user_id,
            reviewer_comment=reviewer_comment,
        )

        finalized = []
        if batch_id:
            finalized = _process_batch_completion(
                cursor, batch_id, reviewer_comment, logged_in_user_id
            )

        roster_month = get_roster_month(cursor, int(req["roster_month_id"]))
        locked_weeks = []
        if roster_month:
            locked_weeks = lock_weeks_touched_by_requests(
                cursor,
                requests=[req],
                months_by_id={int(roster_month["roster_month_id"]): roster_month},
                locked_by=logged_in_user_id,
            )
            for lw in locked_weeks:
                write_audit_log(
                    cursor,
                    roster_month_id=int(roster_month["roster_month_id"]),
                    user_id=int(req["user_id"]),
                    action="WEEK_LOCKED",
                    entity_type="roster_week_lock",
                    entity_id=int(lw["week_number"]),
                    old_value=None,
                    new_value=lw,
                    performed_by=logged_in_user_id,
                    notes=f"Auto-locked {lw.get('label')} after approval",
                )

        conn.commit()

        email_results = []
        if roster_month:
            try:
                email_results = _send_roster_email_after_review(
                    cursor,
                    month_years=[roster_month.get("month_year") or ""],
                    fallback_requests=[req],
                    months_by_id={int(roster_month["roster_month_id"]): roster_month},
                    locked_weeks=locked_weeks,
                    logged_in_user_id=logged_in_user_id,
                    role_name=role_name,
                )
            except Exception as mail_err:
                print(f"[roster weekly email] approve mail failed: {mail_err}", flush=True)

        return api_response(
            200,
            "Change request approved",
            {
                "request_id": int(request_id),
                "finalized_cycles": finalized,
                "locked_weeks": locked_weeks,
                "weekly_roster_emails": email_results,
            },
        )
    except ValueError as e:
        conn.rollback()
        return api_response(400, str(e))
    except Exception as e:
        conn.rollback()
        return api_response(500, f"Approve failed: {str(e)}")
    finally:
        cursor.close()
        conn.close()


@roster_bp.route("/requests/approve_bulk", methods=["POST"])
def roster_approve_bulk():
    """
    Admin/Super Admin bulk approve.
    Body:
      - request_ids: [..]  approve selected
      - OR approve_all: true + month_year  approve every pending submitted request for the month
    Optional: reviewer_comment
    """
    data = request.get_json(silent=True) or {}
    logged_in_user_id, err = _require_logged_in_user(data)
    if err:
        return err

    reviewer_comment = (data.get("reviewer_comment") or "").strip() or None
    approve_all = bool(data.get("approve_all", False))
    month_year = (data.get("month_year") or "").strip()
    raw_ids = data.get("request_ids") or []

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        ctx = get_role_context(cursor, logged_in_user_id)
        role_name = ctx.get("user_role_name", "")
        if not can_approve_reject(role_name):
            return api_response(403, "Only Admin or Super Admin can approve requests")

        request_ids: list[int] = []
        if approve_all:
            if not month_year:
                return api_response(400, "month_year is required for approve_all")
            cursor.execute(
                """
                SELECT rcr.request_id
                FROM roster_change_request rcr
                JOIN roster_month rm ON rm.roster_month_id = rcr.roster_month_id
                WHERE rcr.is_active=1
                  AND rcr.status='Pending'
                  AND rcr.batch_id IS NOT NULL AND rcr.batch_id != ''
                  AND rm.month_year=%s
                ORDER BY rcr.request_id ASC
                """,
                (month_year,),
            )
            request_ids = [int(r["request_id"]) for r in (cursor.fetchall() or [])]
        else:
            try:
                request_ids = [int(x) for x in raw_ids]
            except (TypeError, ValueError):
                return api_response(400, "request_ids must be a list of integers")
            request_ids = list(dict.fromkeys(request_ids))

        if not request_ids:
            return api_response(400, "No pending requests to approve")

        reqs_by_id = _load_requests_by_ids(cursor, request_ids)
        month_ids = [
            int(r["roster_month_id"])
            for r in reqs_by_id.values()
            if r.get("roster_month_id") is not None
        ]
        months_by_id = _load_roster_months_by_ids(cursor, month_ids)

        approved = 0
        failed: list[dict] = []
        batch_ids: set[str] = set()
        approved_reqs: list[dict] = []
        touched_month_ids: set[int] = set()

        for rid in request_ids:
            try:
                req = reqs_by_id.get(int(rid))
                if not req:
                    failed.append({"request_id": rid, "reason": "Not found"})
                    continue
                mid = int(req["roster_month_id"])
                batch_id = _approve_single_request(
                    cursor,
                    req=req,
                    logged_in_user_id=logged_in_user_id,
                    reviewer_comment=reviewer_comment,
                    roster_month=months_by_id.get(mid),
                    refresh_metrics=False,
                )
                if batch_id:
                    batch_ids.add(batch_id)
                approved_reqs.append(req)
                touched_month_ids.add(mid)
                approved += 1
            except Exception as row_err:
                failed.append({"request_id": rid, "reason": str(row_err)})

        # One metrics refresh per touched roster month (not per request)
        for mid in touched_month_ids:
            try:
                refresh_roster_month_metrics(cursor, mid)
            except Exception:
                pass

        finalized = []
        for batch_id in batch_ids:
            finalized.extend(
                _process_batch_completion(
                    cursor, batch_id, reviewer_comment, logged_in_user_id
                )
            )

        locked_weeks = lock_weeks_touched_by_requests(
            cursor,
            requests=approved_reqs,
            months_by_id=months_by_id,
            locked_by=logged_in_user_id,
        )
        for lw in locked_weeks:
            # Audit against first touched month in that month_year if available
            sample_mid = next(
                (
                    mid
                    for mid, rm in months_by_id.items()
                    if (rm.get("month_year") or "") == lw.get("month_year")
                ),
                None,
            )
            sample_req = next(
                (
                    r
                    for r in approved_reqs
                    if sample_mid is not None
                    and int(r.get("roster_month_id") or 0) == int(sample_mid)
                ),
                approved_reqs[0] if approved_reqs else None,
            )
            if sample_req and sample_mid is not None:
                write_audit_log(
                    cursor,
                    roster_month_id=int(sample_mid),
                    user_id=int(sample_req.get("user_id") or 0),
                    action="WEEK_LOCKED",
                    entity_type="roster_week_lock",
                    entity_id=int(lw["week_number"]),
                    old_value=None,
                    new_value=lw,
                    performed_by=logged_in_user_id,
                    notes=f"Auto-locked {lw.get('label')} after bulk approval",
                )

        conn.commit()

        email_results = []
        if approved_reqs:
            try:
                email_results = _send_roster_email_after_review(
                    cursor,
                    month_years=[
                        (months_by_id.get(mid) or {}).get("month_year") or ""
                        for mid in touched_month_ids
                    ],
                    fallback_requests=approved_reqs,
                    months_by_id=months_by_id,
                    locked_weeks=locked_weeks,
                    logged_in_user_id=logged_in_user_id,
                    role_name=role_name,
                )
            except Exception as mail_err:
                print(f"[roster weekly email] bulk approve mail failed: {mail_err}", flush=True)

        return api_response(
            200,
            f"Approved {approved} request(s)",
            {
                "approved": approved,
                "failed": failed,
                "finalized_cycles": finalized,
                "locked_weeks": locked_weeks,
                "weekly_roster_emails": email_results,
            },
        )
    except Exception as e:
        conn.rollback()
        return api_response(500, f"Bulk approve failed: {str(e)}")
    finally:
        cursor.close()
        conn.close()


@roster_bp.route("/requests/reject", methods=["POST"])
def roster_reject_request():
    data = request.get_json(silent=True) or {}
    logged_in_user_id, err = _require_logged_in_user(data)
    if err:
        return err

    request_id = data.get("request_id")
    reviewer_comment = (data.get("reviewer_comment") or "").strip()
    if not request_id:
        return api_response(400, "request_id is required")
    if not reviewer_comment:
        return api_response(400, "reviewer_comment is required")

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        ctx = get_role_context(cursor, logged_in_user_id)
        role_name = ctx.get("user_role_name", "")
        if not can_approve_reject(role_name):
            return api_response(403, "Only Admin or Super Admin can reject requests")

        req = _load_request(cursor, int(request_id))
        if not req:
            return api_response(404, "Change request not found")
        if req.get("status") != "Pending":
            return api_response(400, "Request is not pending")
        if not req.get("batch_id"):
            return api_response(400, "This change has not been submitted for approval yet")

        batch_id = req.get("batch_id")
        now = now_str()

        cursor.execute(
            """
            UPDATE roster_change_request
            SET status='Rejected', reviewer_comment=%s, rejection_reason=%s,
                reviewed_by=%s, reviewed_date=%s
            WHERE request_id=%s
            """,
            (reviewer_comment, reviewer_comment, logged_in_user_id, now, int(request_id)),
        )

        write_audit_log(
            cursor,
            roster_month_id=int(req["roster_month_id"]),
            user_id=int(req["user_id"]),
            action="CHANGE_REQUEST_REJECTED",
            entity_type="roster_change_request",
            entity_id=int(request_id),
            old_value={"status": "Pending"},
            new_value={"status": "Rejected", "reviewer_comment": reviewer_comment},
            performed_by=logged_in_user_id,
            approval_status="Rejected",
            notes=reviewer_comment,
        )

        if batch_id:
            _process_batch_completion(cursor, batch_id, reviewer_comment, logged_in_user_id)

        roster_month = get_roster_month(cursor, int(req["roster_month_id"]))
        conn.commit()

        email_results = []
        if roster_month:
            try:
                email_results = _send_roster_email_after_review(
                    cursor,
                    month_years=[roster_month.get("month_year") or ""],
                    fallback_requests=[req],
                    months_by_id={int(roster_month["roster_month_id"]): roster_month},
                    locked_weeks=None,
                    logged_in_user_id=logged_in_user_id,
                    role_name=role_name,
                )
            except Exception as mail_err:
                print(f"[roster weekly email] reject mail failed: {mail_err}", flush=True)

        return api_response(
            200,
            "Change request rejected",
            {"request_id": int(request_id), "weekly_roster_emails": email_results},
        )
    except Exception as e:
        conn.rollback()
        return api_response(500, f"Reject failed: {str(e)}")
    finally:
        cursor.close()
        conn.close()


@roster_bp.route("/requests/list", methods=["POST"])
def roster_list_requests():
    data = request.get_json(silent=True) or {}
    logged_in_user_id, err = _require_logged_in_user(data)
    if err:
        return err

    status = (data.get("status") or "").strip()
    batch_id = (data.get("batch_id") or "").strip()
    month_year = (data.get("month_year") or "").strip()

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        ctx = get_role_context(cursor, logged_in_user_id)
        role_name = ctx.get("user_role_name", "")

        where = "WHERE rcr.is_active=1"
        params: list = []

        if status:
            where += " AND rcr.status=%s"
            params.append(status)
        if batch_id:
            where += " AND rcr.batch_id=%s"
            params.append(batch_id)
        if month_year:
            where += " AND rm.month_year=%s"
            params.append(month_year)

        if is_self_read_only_roster_role(role_name):
            if not month_year:
                return api_response(400, "month_year is required for agent roster requests")
            where += " AND rcr.user_id=%s"
            params.append(logged_in_user_id)
            where += """
              AND (
                (rcr.batch_id IS NOT NULL AND rcr.batch_id != '')
                OR rcr.status IN ('Approved', 'Rejected', 'Cancelled due to Withdrawal')
              )
            """
        elif not is_admin_or_super_admin(role_name):
            year, month = parse_month_year(month_year) if month_year else (0, 0)
            if month_year:
                scoped = get_eligible_employees(cursor, logged_in_user_id, role_name, year, month)
                scoped_ids = [int(e["user_id"]) for e in scoped]
                if not scoped_ids:
                    return api_response(200, "Requests fetched", [])
                ph = ",".join(["%s"] * len(scoped_ids))
                where += f" AND rcr.user_id IN ({ph})"
                params.extend(scoped_ids)
            where += " AND rcr.submitted_by=%s"
            params.append(logged_in_user_id)

        cursor.execute(
            f"""
            SELECT rcr.*, rm.month_year, u.user_name,
                   sub.user_name AS submitted_by_name,
                   rev.user_name AS reviewed_by_name
            FROM roster_change_request rcr
            JOIN roster_month rm ON rm.roster_month_id = rcr.roster_month_id
            JOIN tfs_user u ON u.user_id = rcr.user_id
            LEFT JOIN tfs_user sub ON sub.user_id = rcr.submitted_by
            LEFT JOIN tfs_user rev ON rev.user_id = rcr.reviewed_by
            {where}
            ORDER BY rcr.submitted_date DESC, rcr.request_id DESC
            """,
            tuple(params),
        )
        rows = cursor.fetchall() or []
        rows = dedupe_change_request_rows(rows)
        for row in rows:
            if isinstance(row.get("change_payload"), str):
                try:
                    row["change_payload"] = json.loads(row["change_payload"])
                except Exception:
                    pass
        return api_response(200, "Requests fetched successfully", rows)
    except Exception as e:
        return api_response(500, f"List requests failed: {str(e)}")
    finally:
        cursor.close()
        conn.close()


@roster_bp.route("/leave/list", methods=["POST"])
def roster_list_leaves():
    data = request.get_json(silent=True) or {}
    logged_in_user_id, err = _require_logged_in_user(data)
    if err:
        return err

    roster_month_id = data.get("roster_month_id")
    if not roster_month_id:
        return api_response(400, "roster_month_id is required")

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        ctx = get_role_context(cursor, logged_in_user_id)
        role_name = ctx.get("user_role_name", "")

        roster_month = get_roster_month(cursor, int(roster_month_id))
        if not roster_month:
            return api_response(404, "Roster month not found")

        if is_self_read_only_roster_role(role_name):
            if int(roster_month["user_id"]) != logged_in_user_id:
                return api_response(403, "You can only view your own roster")
        else:
            scope_err = assert_manager_scope(cursor, logged_in_user_id, role_name, roster_month)
            if scope_err and not is_admin_or_super_admin(role_name):
                return api_response(403, scope_err)

        leaves = get_roster_leaves(cursor, int(roster_month_id))
        for leave in leaves:
            for key in ("start_date", "end_date"):
                if leave.get(key):
                    leave[key] = leave[key].isoformat()
        return api_response(200, "Leaves fetched successfully", leaves)
    except Exception as e:
        return api_response(500, f"List leaves failed: {str(e)}")
    finally:
        cursor.close()
        conn.close()


@roster_bp.route("/lock", methods=["POST"])
def roster_lock_month():
    """Lock all agent rosters for a month (Admin/Super Admin)."""
    data = request.get_json(silent=True) or {}
    logged_in_user_id, err = _require_logged_in_user(data)
    if err:
        return err

    month_year = (data.get("month_year") or "").strip()
    if not month_year:
        return api_response(400, "month_year is required")

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        ctx = get_role_context(cursor, logged_in_user_id)
        if not can_lock_unlock_roster(ctx.get("user_role_name", "")):
            return api_response(403, "Only Admin or Super Admin can lock roster months")

        if not can_lock_roster_month(month_year):
            return api_response(400, roster_lock_not_before_message(month_year))

        pending_count = count_pending_change_requests_for_month(cursor, month_year)
        if pending_count > 0:
            return api_response(
                400,
                f"Cannot lock: {pending_count} pending request(s) must be approved, rejected, or withdrawn first.",
                {"pending_count": pending_count},
            )

        cursor.execute(
            """
            SELECT roster_month_id, user_id, status
            FROM roster_month
            WHERE month_year=%s AND is_active=1 AND status != 'Locked'
            """,
            (month_year,),
        )
        rows = cursor.fetchall() or []
        if not rows:
            return api_response(400, "All rosters for this month are already locked")

        locked = 0
        for row in rows:
            rid = int(row["roster_month_id"])
            if lock_roster_month_record(cursor, rid, logged_in_user_id):
                locked += 1
                write_audit_log(
                    cursor,
                    roster_month_id=rid,
                    user_id=int(row["user_id"]),
                    action="ROSTER_LOCKED",
                    entity_type="roster_month",
                    entity_id=rid,
                    old_value={"status": row.get("status")},
                    new_value={"status": "Locked", "locked_by": logged_in_user_id},
                    performed_by=logged_in_user_id,
                    notes=f"Month lock for {month_year}",
                )

        conn.commit()
        return api_response(
            200,
            f"Locked {locked} roster calendar(s) for {month_year}",
            {"locked_count": locked, "month_year": month_year},
        )
    except Exception as e:
        conn.rollback()
        return api_response(500, f"Lock failed: {str(e)}")
    finally:
        cursor.close()
        conn.close()


@roster_bp.route("/unlock", methods=["POST"])
def roster_unlock_month():
    """
    Unlock roster month(s).
    - Prefer month_year: unlocks ALL Locked calendars for that month (mirrors /lock).
    - Or roster_month_id: unlocks a single employee roster.
    """
    data = request.get_json(silent=True) or {}
    logged_in_user_id, err = _require_logged_in_user(data)
    if err:
        return err

    month_year = (data.get("month_year") or "").strip()
    roster_month_id = data.get("roster_month_id")
    if not month_year and not roster_month_id:
        return api_response(400, "month_year or roster_month_id is required")

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        ctx = get_role_context(cursor, logged_in_user_id)
        if not can_lock_unlock_roster(ctx.get("user_role_name", "")):
            return api_response(403, "Only Admin or Super Admin can unlock roster months")

        # Month-wide unlock (same scope as Lock)
        if month_year:
            cursor.execute(
                """
                SELECT roster_month_id, user_id, approved_by, last_approved_by
                FROM roster_month
                WHERE month_year=%s AND is_active=1 AND status='Locked'
                """,
                (month_year,),
            )
            rows = cursor.fetchall() or []
            if not rows:
                return api_response(400, f"No locked rosters found for {month_year}")

            unlocked = 0
            for row in rows:
                rid = int(row["roster_month_id"])
                if unlock_roster_month_record(cursor, rid):
                    unlocked += 1
                    restored = (
                        "Approved"
                        if row.get("approved_by") or row.get("last_approved_by")
                        else "Draft"
                    )
                    write_audit_log(
                        cursor,
                        roster_month_id=rid,
                        user_id=int(row["user_id"]),
                        action="ROSTER_UNLOCKED",
                        entity_type="roster_month",
                        entity_id=rid,
                        old_value={"status": "Locked"},
                        new_value={"status": restored},
                        performed_by=logged_in_user_id,
                        notes=f"Month unlock for {month_year}",
                    )

            conn.commit()
            return api_response(
                200,
                f"Unlocked {unlocked} roster calendar(s) for {month_year}",
                {"unlocked_count": unlocked, "month_year": month_year},
            )

        # Single-roster unlock (legacy)
        roster_month = get_roster_month(cursor, int(roster_month_id))
        if not roster_month:
            return api_response(404, "Roster month not found")

        if not unlock_roster_month_record(cursor, int(roster_month_id)):
            return api_response(400, "Roster month not found or not locked")

        restored = (
            "Approved"
            if roster_month.get("approved_by") or roster_month.get("last_approved_by")
            else "Draft"
        )
        write_audit_log(
            cursor,
            roster_month_id=int(roster_month_id),
            user_id=int(roster_month["user_id"]),
            action="ROSTER_UNLOCKED",
            entity_type="roster_month",
            entity_id=int(roster_month_id),
            old_value={"status": "Locked", "locked_by": roster_month.get("locked_by")},
            new_value={"status": restored},
            performed_by=logged_in_user_id,
        )

        conn.commit()
        return api_response(200, f"Roster month unlocked to {restored}")
    except Exception as e:
        conn.rollback()
        return api_response(500, f"Unlock failed: {str(e)}")
    finally:
        cursor.close()
        conn.close()


@roster_bp.route("/week/lock", methods=["POST"])
def roster_lock_week():
    """Admin locks a single Mon–Sun week so managers cannot edit it."""
    data = request.get_json(silent=True) or {}
    logged_in_user_id, err = _require_logged_in_user(data)
    if err:
        return err

    month_year = (data.get("month_year") or "").strip()
    week_number = data.get("week_number")
    if not month_year:
        return api_response(400, "month_year is required")
    if week_number is None:
        return api_response(400, "week_number is required")

    try:
        week_number = int(week_number)
    except (TypeError, ValueError):
        return api_response(400, "week_number must be an integer")

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        ctx = get_role_context(cursor, logged_in_user_id)
        if not can_lock_unlock_roster(ctx.get("user_role_name", "")):
            return api_response(403, "Only Admin or Super Admin can lock roster weeks")

        meta = week_meta_for_number(month_year, week_number)
        if not meta:
            return api_response(400, f"Week {week_number} is not valid for {month_year}")

        existing = get_week_lock(cursor, month_year, week_number)
        if existing:
            return api_response(400, f"Week {week_number} is already locked for {month_year}")

        lock_week(
            cursor,
            month_year=month_year,
            week_number=week_number,
            week_start=meta.get("week_start"),
            week_end=meta.get("week_end"),
            locked_by=logged_in_user_id,
        )

        cursor.execute(
            """
            SELECT roster_month_id, user_id
            FROM roster_month
            WHERE month_year=%s AND is_active=1
            ORDER BY roster_month_id ASC
            LIMIT 1
            """,
            (month_year,),
        )
        sample = cursor.fetchone()
        if sample:
            write_audit_log(
                cursor,
                roster_month_id=int(sample["roster_month_id"]),
                user_id=int(sample["user_id"]),
                action="WEEK_LOCKED",
                entity_type="roster_week_lock",
                entity_id=week_number,
                old_value=None,
                new_value={
                    "week_number": week_number,
                    "week_start": str(meta.get("week_start") or ""),
                    "week_end": str(meta.get("week_end") or ""),
                },
                performed_by=logged_in_user_id,
                notes=f"Manually locked Week {week_number} for {month_year}",
            )

        conn.commit()
        label = meta.get("label") or f"Week {week_number}"
        return api_response(
            200,
            f"{label} locked for {month_year}",
            {
                "month_year": month_year,
                "week_number": week_number,
                "week_locks": list_week_locks(cursor, month_year),
            },
        )
    except ValueError as e:
        conn.rollback()
        return api_response(400, str(e))
    except Exception as e:
        conn.rollback()
        return api_response(500, f"Week lock failed: {str(e)}")
    finally:
        cursor.close()
        conn.close()


@roster_bp.route("/week/unlock", methods=["POST"])
def roster_unlock_week():
    """Admin unlocks a single Mon–Sun week so managers can edit it again."""
    data = request.get_json(silent=True) or {}
    logged_in_user_id, err = _require_logged_in_user(data)
    if err:
        return err

    month_year = (data.get("month_year") or "").strip()
    week_number = data.get("week_number")
    if not month_year:
        return api_response(400, "month_year is required")
    if week_number is None:
        return api_response(400, "week_number is required")

    try:
        week_number = int(week_number)
    except (TypeError, ValueError):
        return api_response(400, "week_number must be an integer")

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        ctx = get_role_context(cursor, logged_in_user_id)
        if not can_lock_unlock_roster(ctx.get("user_role_name", "")):
            return api_response(403, "Only Admin or Super Admin can unlock roster weeks")

        existing = get_week_lock(cursor, month_year, week_number)
        if not existing:
            return api_response(400, f"Week {week_number} is not locked for {month_year}")

        unlock_week(cursor, month_year, week_number)

        # Audit against any active roster in the month (if present)
        cursor.execute(
            """
            SELECT roster_month_id, user_id
            FROM roster_month
            WHERE month_year=%s AND is_active=1
            ORDER BY roster_month_id ASC
            LIMIT 1
            """,
            (month_year,),
        )
        sample = cursor.fetchone()
        if sample:
            write_audit_log(
                cursor,
                roster_month_id=int(sample["roster_month_id"]),
                user_id=int(sample["user_id"]),
                action="WEEK_UNLOCKED",
                entity_type="roster_week_lock",
                entity_id=week_number,
                old_value={
                    "week_number": week_number,
                    "locked_by": existing.get("locked_by"),
                    "locked_date": str(existing.get("locked_date") or ""),
                },
                new_value={"status": "Unlocked"},
                performed_by=logged_in_user_id,
                notes=f"Unlocked Week {week_number} for {month_year}",
            )

        conn.commit()
        meta = week_meta_for_date(
            month_year, parse_date(existing.get("week_start"))
        )
        label = (meta or {}).get("label") or f"Week {week_number}"
        return api_response(
            200,
            f"{label} unlocked for {month_year}",
            {
                "month_year": month_year,
                "week_number": week_number,
                "week_locks": list_week_locks(cursor, month_year),
            },
        )
    except Exception as e:
        conn.rollback()
        return api_response(500, f"Week unlock failed: {str(e)}")
    finally:
        cursor.close()
        conn.close()


@roster_bp.route("/week/locks", methods=["POST"])
def roster_list_week_locks():
    """List locked weeks for a month."""
    data = request.get_json(silent=True) or {}
    logged_in_user_id, err = _require_logged_in_user(data)
    if err:
        return err

    month_year = (data.get("month_year") or "").strip()
    if not month_year:
        return api_response(400, "month_year is required")

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        ctx = get_role_context(cursor, logged_in_user_id)
        role_name = ctx.get("user_role_name", "")
        if not (
            can_manage_roster_employees(role_name)
            or is_admin_or_super_admin(role_name)
            or is_self_read_only_roster_role(role_name)
        ):
            return api_response(403, "You do not have permission to view week locks")

        locks = list_week_locks(cursor, month_year)
        return api_response(200, "Week locks fetched", {"week_locks": locks})
    except Exception as e:
        return api_response(500, f"Failed to list week locks: {str(e)}")
    finally:
        cursor.close()
        conn.close()


@roster_bp.route("/notify_approval", methods=["POST"])
def roster_notify_approval():
    """
    Super Admin only. Email Admin/Super Admin that pending submitted
    roster requests are waiting. Optional week_number limits to that week.
    """
    data = request.get_json(silent=True) or {}
    logged_in_user_id, err = _require_logged_in_user(data)
    if err:
        return err

    month_year = (data.get("month_year") or "").strip()
    if not month_year:
        return api_response(400, "month_year is required")

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        ctx = get_role_context(cursor, logged_in_user_id)
        role_name = ctx.get("user_role_name", "")
        if not is_super_admin(role_name):
            return api_response(403, "Only Super Admin can send the approval notification email")

        week_number = data.get("week_number")
        week_meta = None
        if week_number is not None and str(week_number).strip() != "":
            try:
                week_meta = week_meta_for_number(month_year, int(week_number))
            except (TypeError, ValueError):
                week_meta = None
            if not week_meta:
                return api_response(400, f"Week {week_number} was not found for {month_year}")

        cursor.execute(
            """
            SELECT rcr.request_id, rcr.change_type, rcr.change_payload
            FROM roster_change_request rcr
            JOIN roster_month rm ON rm.roster_month_id = rcr.roster_month_id
            WHERE rcr.is_active=1
              AND rcr.status='Pending'
              AND rcr.batch_id IS NOT NULL AND TRIM(rcr.batch_id) != ''
              AND rm.is_active=1
              AND rm.month_year=%s
            """,
            (month_year,),
        )
        rows = cursor.fetchall() or []
        if week_meta:
            ws = parse_date(week_meta.get("week_start"))
            we = parse_date(week_meta.get("week_end"))
            matching = []
            for row in rows:
                dates = dates_from_change_request(row)
                if not dates:
                    continue
                if any(ws and we and ws <= d <= we for d in dates):
                    matching.append(row)
            rows = matching

        if not rows:
            scope = f"Week {week_meta.get('week_number')}" if week_meta else month_year
            return api_response(
                400,
                f"No pending submitted requests to notify for {scope}",
            )

        week_label = None
        if week_meta:
            week_label = week_meta.get("label") or f"Week {week_meta.get('week_number')}"

        notify = send_roster_approval_needed_email(
            cursor,
            month_year=month_year,
            submitted_by=logged_in_user_id,
            request_count=len(rows),
            week_label=week_label,
        )
        if not notify.get("sent"):
            return api_response(
                400,
                notify.get("reason") or "Approval notification email was not sent",
                {"approval_notify": notify, "pending_count": len(rows)},
            )
        return api_response(
            200,
            "Approval notification emailed to Admin and Super Admin",
            {"approval_notify": notify, "pending_count": len(rows)},
        )
    except Exception as e:
        return api_response(500, f"Approval notify failed: {str(e)}")
    finally:
        cursor.close()
        conn.close()


@roster_bp.route("/week/email", methods=["POST"])
def roster_email_week():
    """Admin resend of the weekly roster HTML mail for one week."""
    data = request.get_json(silent=True) or {}
    logged_in_user_id, err = _require_logged_in_user(data)
    if err:
        return err

    month_year = (data.get("month_year") or "").strip()
    week_number = data.get("week_number")
    if not month_year:
        return api_response(400, "month_year is required")
    try:
        wn = int(week_number)
    except (TypeError, ValueError):
        return api_response(400, "week_number is required")

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        ctx = get_role_context(cursor, logged_in_user_id)
        role_name = ctx.get("user_role_name", "")
        if not is_admin_or_super_admin(role_name):
            return api_response(403, "Only Admin or Super Admin can send weekly roster email")

        year, month = parse_month_year(month_year)
        match = next(
            (w for w in weeks_in_month(year, month) if int(w.get("week_number") or 0) == wn),
            None,
        )
        if not match:
            return api_response(400, f"Week {wn} not found for {month_year}")

        results = send_weekly_roster_after_approval(
            cursor,
            weeks=[match],
            logged_in_user_id=logged_in_user_id,
            role_name=role_name,
        )
        sent = any(r.get("sent") for r in results)
        reason = next((r.get("reason") for r in results if r.get("reason")), None)
        if not sent:
            return api_response(400, reason or "Weekly roster email was not sent", {"weekly_roster_emails": results})
        return api_response(200, "Weekly roster email sent", {"weekly_roster_emails": results})
    except ValueError as e:
        return api_response(400, str(e))
    except Exception as e:
        return api_response(500, f"Weekly roster email failed: {str(e)}")
    finally:
        cursor.close()
        conn.close()


@roster_bp.route("/versions/list", methods=["POST"])
def roster_list_versions():
    data = request.get_json(silent=True) or {}
    logged_in_user_id, err = _require_logged_in_user(data)
    if err:
        return err

    roster_month_id = data.get("roster_month_id")
    if not roster_month_id:
        return api_response(400, "roster_month_id is required")

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        ctx = get_role_context(cursor, logged_in_user_id)
        role_name = ctx.get("user_role_name", "")

        roster_month = get_roster_month(cursor, int(roster_month_id))
        if not roster_month:
            return api_response(404, "Roster month not found")

        if is_self_read_only_roster_role(role_name):
            if int(roster_month["user_id"]) != logged_in_user_id:
                return api_response(403, "You can only view your own roster history")

        cursor.execute(
            """
            SELECT version_id, roster_month_id, roster_version, approved_by,
                   approved_date, reviewer_comment, production_synced_at, created_date
            FROM roster_version_snapshot
            WHERE roster_month_id=%s
            ORDER BY roster_version ASC
            """,
            (int(roster_month_id),),
        )
        rows = cursor.fetchall() or []
        for row in rows:
            for key in ("approved_date", "production_synced_at", "created_date"):
                if row.get(key):
                    row[key] = str(row[key])
        return api_response(200, "Version history fetched", rows)
    except Exception as e:
        return api_response(500, f"List versions failed: {str(e)}")
    finally:
        cursor.close()
        conn.close()


@roster_bp.route("/versions/detail", methods=["POST"])
def roster_version_detail():
    data = request.get_json(silent=True) or {}
    logged_in_user_id, err = _require_logged_in_user(data)
    if err:
        return err

    version_id = data.get("version_id")
    if not version_id:
        return api_response(400, "version_id is required")

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute(
            "SELECT * FROM roster_version_snapshot WHERE version_id=%s",
            (int(version_id),),
        )
        row = cursor.fetchone()
        if not row:
            return api_response(404, "Version not found")

        if isinstance(row.get("snapshot_json"), str):
            row["snapshot_json"] = json.loads(row["snapshot_json"])

        return api_response(200, "Version detail fetched", row)
    except Exception as e:
        return api_response(500, f"Version detail failed: {str(e)}")
    finally:
        cursor.close()
        conn.close()


@roster_bp.route("/audit/list", methods=["POST"])
def roster_audit_list():
    data = request.get_json(silent=True) or {}
    logged_in_user_id, err = _require_logged_in_user(data)
    if err:
        return err

    roster_month_id = data.get("roster_month_id")

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        ctx = get_role_context(cursor, logged_in_user_id)
        role_name = ctx.get("user_role_name", "")

        where = "WHERE 1=1"
        params: list = []
        if roster_month_id:
            where += " AND roster_month_id=%s"
            params.append(int(roster_month_id))

        if is_self_read_only_roster_role(role_name):
            where += " AND user_id=%s"
            params.append(logged_in_user_id)

        cursor.execute(
            f"""
            SELECT * FROM roster_audit_log
            {where}
            ORDER BY performed_date DESC, audit_id DESC
            LIMIT 500
            """,
            tuple(params),
        )
        rows = cursor.fetchall() or []
        return api_response(200, "Audit log fetched", rows)
    except Exception as e:
        return api_response(500, f"Audit list failed: {str(e)}")
    finally:
        cursor.close()
        conn.close()


@roster_bp.route("/recalculate", methods=["POST"])
def roster_recalculate_preview():
    data = request.get_json(silent=True) or {}
    logged_in_user_id, err = _require_logged_in_user(data)
    if err:
        return err

    roster_month_id = data.get("roster_month_id")
    if not roster_month_id:
        return api_response(400, "roster_month_id is required")

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        roster_month = get_roster_month(cursor, int(roster_month_id))
        if not roster_month:
            return api_response(404, "Roster month not found")

        days = get_roster_days(cursor, int(roster_month_id))
        leaves = get_roster_leaves(cursor, int(roster_month_id))
        from utils.roster_metrics import (
            apply_active_leaves_to_days,
            recalculate_metrics_from_days_and_leaves,
        )

        days_for_metrics = apply_active_leaves_to_days(days, leaves)
        metrics = recalculate_metrics_from_days_and_leaves(days_for_metrics, leaves)
        return api_response(200, "Metrics preview calculated", metrics)
    except Exception as e:
        return api_response(500, f"Recalculate failed: {str(e)}")
    finally:
        cursor.close()
        conn.close()


def _parse_week_start(value) -> date | None:
    d = parse_date(value)
    if d is None and value:
        try:
            raw = str(value).strip()[:10]
            d = datetime.strptime(raw, "%Y-%m-%d").date()
        except Exception:
            d = None
    if d is None:
        return None
    return monday_of_week(d)


def _employees_name_index(employees: list[dict]) -> dict[str, list[dict]]:
    index: dict[str, list[dict]] = {}
    for emp in employees:
        name = (emp.get("user_name") or "").strip()
        if not name:
            continue
        key = "".join(ch for ch in name.lower() if ch.isalnum())
        index.setdefault(key, []).append(emp)
    return index


def _load_roster_months_for_users(
    cursor,
    user_ids: list[int],
    month_years: list[str],
) -> dict[tuple[int, str], dict]:
    if not user_ids or not month_years:
        return {}
    placeholders_u = ",".join(["%s"] * len(user_ids))
    placeholders_m = ",".join(["%s"] * len(month_years))
    cursor.execute(
        f"""
        SELECT rm.*, u.user_name, LOWER(TRIM(r.role_name)) AS employee_role_name,
               locker.user_name AS locked_by_name
        FROM roster_month rm
        JOIN tfs_user u ON u.user_id = rm.user_id
        JOIN user_role r ON r.role_id = u.role_id
        LEFT JOIN tfs_user locker ON locker.user_id = rm.locked_by
        WHERE rm.is_active=1
          AND rm.user_id IN ({placeholders_u})
          AND rm.month_year IN ({placeholders_m})
        """,
        tuple([*user_ids, *month_years]),
    )
    out: dict[tuple[int, str], dict] = {}
    for row in cursor.fetchall() or []:
        out[(int(row["user_id"]), row["month_year"])] = row
    return out


def _load_days_lookup(cursor, roster_month_ids: list[int]) -> dict[tuple[int, str], dict]:
    """(user_id, date_iso) -> day row (includes user_id via join; leave overlay applied)."""
    if not roster_month_ids:
        return {}
    placeholders = ",".join(["%s"] * len(roster_month_ids))
    cursor.execute(
        f"""
        SELECT rd.*, rm.user_id
        FROM roster_day rd
        JOIN roster_month rm ON rm.roster_month_id = rd.roster_month_id
        WHERE rd.is_active=1 AND rm.is_active=1
          AND rd.roster_month_id IN ({placeholders})
        """,
        tuple(roster_month_ids),
    )
    rows_by_month: dict[int, list[dict]] = {}
    user_by_month: dict[int, int] = {}
    for row in cursor.fetchall() or []:
        mid = int(row["roster_month_id"])
        rows_by_month.setdefault(mid, []).append(row)
        user_by_month[mid] = int(row["user_id"])

    from utils.roster_metrics import apply_active_leaves_to_days

    lookup: dict[tuple[int, str], dict] = {}
    for mid, days in rows_by_month.items():
        leaves = get_roster_leaves(cursor, mid)
        enriched = apply_active_leaves_to_days(days, leaves)
        uid = user_by_month.get(mid)
        for row in enriched:
            d = parse_date(row.get("roster_date"))
            if not d or uid is None:
                continue
            lookup[(uid, d.isoformat())] = row
    return lookup


def _build_excel_preview(
    cursor,
    *,
    logged_in_user_id: int,
    role_name: str,
    file_bytes: bytes,
    team_id=None,
) -> dict:
    parsed = parse_roster_excel(file_bytes)
    dates = parsed["dates"]
    if not dates:
        raise ValueError("No date columns found in Excel")

    month_years = month_years_for_dates(dates)
    # Eligible employees across all months touched by the week
    employees_by_id: dict[int, dict] = {}
    for my in month_years:
        year, month = parse_month_year(my)
        emps = get_excel_roster_employees(
            cursor, logged_in_user_id, role_name, year, month, team_id=team_id
        )
        for e in emps:
            employees_by_id[int(e["user_id"])] = e

    employees = list(employees_by_id.values())
    name_index = _employees_name_index(employees)
    user_ids = list(employees_by_id.keys())
    roster_by_um = _load_roster_months_for_users(cursor, user_ids, month_years)
    roster_ids = [int(r["roster_month_id"]) for r in roster_by_um.values()]
    day_lookup = _load_days_lookup(cursor, roster_ids)

    changes: list[dict] = []
    skipped: list[dict] = []
    errors: list[dict] = []
    locked_weeks_found: dict[tuple[str, int], dict] = {}

    # Cache lock lookups — otherwise every cell hits the DB (all-weeks = thousands of queries)
    month_lock_cache: dict[str, str] = {}
    week_lock_cache: dict[tuple[str, int], str] = {}
    editable_cache: dict[int, tuple[bool, str]] = {}

    def _month_editable(roster_month: dict) -> tuple[bool, str]:
        rid = int(roster_month["roster_month_id"])
        cached = editable_cache.get(rid)
        if cached is not None:
            return cached
        month_year = (roster_month.get("month_year") or "").strip()
        if month_year:
            if month_year not in month_lock_cache:
                month_lock_cache[month_year] = month_calendar_lock_message(cursor, month_year) or ""
            lock_msg = month_lock_cache[month_year]
            if lock_msg:
                editable_cache[rid] = (False, lock_msg)
                return editable_cache[rid]
        if is_month_locked(roster_month.get("status", "")):
            locker = (roster_month.get("locked_by_name") or "").strip() or "an administrator"
            editable_cache[rid] = (
                False,
                f"This roster is locked by {locker} and cannot be changed",
            )
            return editable_cache[rid]
        if not can_create_change_requests(roster_month.get("status", "")):
            editable_cache[rid] = (
                False,
                f"Changes not allowed in status {roster_month.get('status')}",
            )
            return editable_cache[rid]
        editable_cache[rid] = (True, "")
        return editable_cache[rid]

    def _week_lock_for_date(month_year: str, d: date) -> str:
        """Return lock message if this date's week is locked; caches by week number."""
        month_year = (month_year or "").strip()
        if not month_year or not d:
            return ""
        meta = week_meta_for_date(month_year, d)
        if not meta:
            return ""
        wn = int(meta["week_number"])
        key = (month_year, wn)
        if key in week_lock_cache:
            return week_lock_cache[key]
        msg = week_lock_message_for_dates(cursor, month_year, [d]) or ""
        week_lock_cache[key] = msg
        if msg:
            locked_weeks_found[key] = {
                "month_year": month_year,
                "week_number": wn,
                "week_start": meta.get("week_start"),
                "week_end": meta.get("week_end"),
                "label": meta.get("label") or f"Week {wn}",
                "short_label": meta.get("short_label") or f"Week {wn}",
                "message": (
                    f"{meta.get('short_label') or f'Week {wn}'} is locked. "
                    "You cannot edit this week until an administrator unlocks it."
                ),
            }
        return msg

    for row in parsed["rows"]:
        emp, err = match_employee_name(row["name"], name_index)
        if err or not emp:
            # Out-of-scope / other-team / unknown names must not block apply
            # for the employees that did match.
            skipped.append(
                {
                    "row": row["row"],
                    "sheet": row.get("sheet"),
                    "name": row["name"],
                    "reason": f"{err or 'Unmatched'} — skipped",
                }
            )
            continue

        if not emp.get("joining_date"):
            skipped.append(
                {
                    "row": row["row"],
                    "sheet": row.get("sheet"),
                    "name": row["name"],
                    "user_id": emp.get("user_id"),
                    "reason": "joining_date is not set — skipped (set DOJ before roster/Excel)",
                }
            )
            continue

        uid = int(emp["user_id"])
        role = emp.get("role_name")

        for date_iso, label in row["cells"].items():
            d = parse_date(date_iso)
            if not d:
                errors.append(
                    {
                        "row": row["row"],
                        "sheet": row.get("sheet"),
                        "name": row["name"],
                        "date": date_iso,
                        "reason": "Invalid date",
                    }
                )
                continue

            try:
                change = excel_label_to_change(label, d, role_name=role)
            except ValueError as ve:
                errors.append(
                    {
                        "row": row["row"],
                        "sheet": row.get("sheet"),
                        "name": row["name"],
                        "date": date_iso,
                        "value": label,
                        "reason": str(ve),
                    }
                )
                continue

            if not change:
                continue

            roster_month = resolve_roster_month_for_date(roster_by_um, uid, d)
            if not roster_month:
                skipped.append(
                    {
                        "row": row["row"],
                        "sheet": row.get("sheet"),
                        "user_id": uid,
                        "user_name": emp.get("user_name"),
                        "date": date_iso,
                        "label": change.get("label"),
                        "reason": f"No roster generated for {month_year_label(d.year, d.month)} — skipped",
                    }
                )
                continue

            month_year = (roster_month.get("month_year") or "").strip()
            week_msg = _week_lock_for_date(month_year, d)
            if week_msg:
                meta = week_meta_for_date(month_year, d)
                short = (meta or {}).get("short_label") or "This week"
                errors.append(
                    {
                        "row": row["row"],
                        "sheet": row.get("sheet"),
                        "name": emp.get("user_name") or row["name"],
                        "user_id": uid,
                        "date": date_iso,
                        "value": label,
                        "reason": (
                            f"{short} is locked. You cannot edit this week "
                            "until an administrator unlocks it."
                        ),
                        "locked_week": True,
                        "week_number": (meta or {}).get("week_number"),
                    }
                )
                continue

            ok, msg = _month_editable(roster_month)
            if not ok:
                # Locked / not editable — skip these cells; don't block the rest
                skipped.append(
                    {
                        "row": row["row"],
                        "sheet": row.get("sheet"),
                        "user_id": uid,
                        "user_name": emp.get("user_name"),
                        "date": date_iso,
                        "label": change.get("label"),
                        "reason": f"{msg} — skipped",
                    }
                )
                continue

            if roster_month.get("status") == "Pending Approval":
                skipped.append(
                    {
                        "row": row["row"],
                        "sheet": row.get("sheet"),
                        "user_id": uid,
                        "user_name": emp.get("user_name"),
                        "date": date_iso,
                        "label": change.get("label"),
                        "reason": "Roster is Pending Approval — skipped (withdraw first)",
                    }
                )
                continue

            current_day = day_lookup.get((uid, date_iso))
            if not current_day:
                skipped.append(
                    {
                        "row": row["row"],
                        "sheet": row.get("sheet"),
                        "user_id": uid,
                        "user_name": emp.get("user_name"),
                        "date": date_iso,
                        "label": change.get("label"),
                        "reason": "Roster day not found — skipped",
                    }
                )
                continue

            try:
                change = adjust_excel_change_for_holiday(current_day, change)
            except ValueError as ve:
                errors.append(
                    {
                        "row": row["row"],
                        "sheet": row.get("sheet"),
                        "name": emp.get("user_name") or row["name"],
                        "user_id": uid,
                        "date": date_iso,
                        "value": label,
                        "reason": str(ve),
                    }
                )
                continue

            if is_noop_change(current_day, change):
                skipped.append(
                    {
                        "row": row["row"],
                        "user_id": uid,
                        "user_name": emp.get("user_name"),
                        "date": date_iso,
                        "label": change.get("label"),
                        "reason": "No change from current roster",
                    }
                )
                continue

            current_label = day_to_excel_label(current_day, role)
            changes.append(
                {
                    "row": row["row"],
                    "user_id": uid,
                    "user_name": emp.get("user_name"),
                    "roster_month_id": int(roster_month["roster_month_id"]),
                    "month_year": roster_month.get("month_year"),
                    "date": date_iso,
                    "current_label": current_label,
                    "new_label": change.get("label"),
                    "change_type": change["change_type"],
                    "change_payload": change["change_payload"],
                }
            )

    locked_weeks_list = list(locked_weeks_found.values())
    locked_weeks_list.sort(
        key=lambda x: (x.get("month_year") or "", int(x.get("week_number") or 0))
    )

    return {
        "week_start": dates[0].isoformat() if dates else None,
        "week_end": dates[-1].isoformat() if dates else None,
        "dates": [d.isoformat() for d in dates],
        "sheets": parsed.get("sheets") or [],
        "summary": {
            "rows": len(parsed["rows"]),
            "changes": len(changes),
            "skipped": len(skipped),
            "errors": len(errors),
            "sheets": len(parsed.get("sheets") or []),
            "locked_weeks": len(locked_weeks_list),
        },
        "changes": changes,
        "skipped": skipped,
        "errors": errors,
        "locked_weeks": locked_weeks_list,
    }


@roster_bp.route("/excel/weeks", methods=["POST"])
def roster_excel_weeks():
    """List weeks that touch a month (for template week picker)."""
    data = request.get_json(silent=True) or {}
    logged_in_user_id, err = _require_logged_in_user(data)
    if err:
        return err

    month_year = (data.get("month_year") or "").strip()
    if not month_year:
        return api_response(400, "month_year is required")

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        ctx = get_role_context(cursor, logged_in_user_id)
        role_name = ctx.get("user_role_name", "")
        if not can_manage_roster_employees(role_name):
            return api_response(403, "You do not have permission to manage rosters")
        year, month = parse_month_year(month_year)
        weeks = annotate_weeks_with_locks(
            cursor, month_year, weeks_in_month(year, month)
        )
        return api_response(
            200,
            "Weeks fetched",
            {"weeks": weeks, "week_locks": list_week_locks(cursor, month_year)},
        )
    except ValueError as e:
        return api_response(400, str(e))
    except Exception as e:
        return api_response(500, f"Failed to list weeks: {str(e)}")
    finally:
        cursor.close()
        conn.close()


@roster_bp.route("/excel/template", methods=["POST"])
def roster_excel_template():
    """
    Download roster Excel template with agent names, dates, dropdowns.

    Options:
      - week_start (YYYY-MM-DD): single week
      - month_year + week_number: single week by Week 1 / Week 2 …
      - month_year + all_weeks=true: full month (one sheet per week)
    Optional: team_id, prefill (bool, default true)
    """
    data = request.get_json(silent=True) or {}
    logged_in_user_id, err = _require_logged_in_user(data)
    if err:
        return err

    team_id = data.get("team_id")
    prefill = bool(data.get("prefill", True))
    all_weeks = bool(data.get("all_weeks", False))
    month_year = (data.get("month_year") or "").strip()
    week_number = data.get("week_number")

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        ctx = get_role_context(cursor, logged_in_user_id)
        role_name = ctx.get("user_role_name", "")
        if not can_manage_roster_employees(role_name):
            return api_response(403, "You do not have permission to manage rosters")

        weeks_meta: list[dict] = []
        week_start = None

        if all_weeks or week_number is not None:
            if not month_year:
                return api_response(400, "month_year is required for week_number / all_weeks")
            year, month = parse_month_year(month_year)
            weeks_meta = weeks_in_month(year, month)
            if not weeks_meta:
                return api_response(400, "No weeks found for this month")

            if all_weeks:
                pass
            else:
                try:
                    wn = int(week_number)
                except (TypeError, ValueError):
                    return api_response(400, "week_number must be an integer (1, 2, 3, …)")
                match = next((w for w in weeks_meta if int(w["week_number"]) == wn), None)
                if not match:
                    return api_response(400, f"Week {wn} not found for {month_year}")
                week_start = _parse_week_start(match["week_start"])
                weeks_meta = [match]
        else:
            week_start = _parse_week_start(data.get("week_start") or data.get("week_start_date"))
            if not week_start:
                return api_response(
                    400,
                    "Provide week_start, or month_year + week_number, or month_year + all_weeks",
                )
            # Resolve week number label from the month that owns most days
            primary = week_start if week_start.day <= 28 else week_start
            # Prefer month_year from request if given, else from week start
            if month_year:
                year, month = parse_month_year(month_year)
            else:
                year, month = primary.year, primary.month
            weeks_meta = weeks_in_month(year, month)
            match = next(
                (w for w in weeks_meta if w["week_start"] == week_start.isoformat()),
                None,
            )
            if match:
                weeks_meta = [match]
            else:
                weeks_meta = [
                    {
                        "week_number": 1,
                        "week_start": week_start.isoformat(),
                        "week_end": week_dates(week_start)[-1].isoformat(),
                        "label": f"Week ({week_start.isoformat()})",
                    }
                ]

        # Collect dates across selected weeks for employee/roster lookup
        all_days = []
        for w in weeks_meta:
            ws = _parse_week_start(w["week_start"])
            if ws:
                all_days.extend(week_dates(ws))
        if not all_days and week_start:
            all_days = week_dates(week_start)

        month_years = month_years_for_dates(all_days)
        if month_year and month_year not in month_years:
            month_years.insert(0, month_year)

        employees_by_id: dict[int, dict] = {}
        for my in month_years:
            year, month = parse_month_year(my)
            emps = get_excel_roster_employees(
                cursor, logged_in_user_id, role_name, year, month, team_id=team_id
            )
            for e in emps:
                employees_by_id[int(e["user_id"])] = e

        employees = sorted(
            employees_by_id.values(),
            key=lambda e: (e.get("user_name") or "").lower(),
        )
        if not employees:
            return api_response(400, "No eligible employees found for this week/team")

        day_lookup: dict[tuple[int, str], dict] = {}
        if prefill:
            roster_by_um = _load_roster_months_for_users(
                cursor, list(employees_by_id.keys()), month_years
            )
            day_lookup = _load_days_lookup(
                cursor, [int(r["roster_month_id"]) for r in roster_by_um.values()]
            )

        if all_weeks:
            file_bytes = build_month_workbook(
                weeks=weeks_meta,
                employees=employees,
                day_lookup=day_lookup,
                month_label=month_year,
            )
            filename = f"Roster_{month_year}_All_Weeks.xlsx"
        else:
            wn = int(weeks_meta[0].get("week_number") or 0) or None
            ws = week_start or _parse_week_start(weeks_meta[0]["week_start"])
            file_bytes = build_template_workbook(
                week_start=ws,
                employees=employees,
                day_lookup=day_lookup,
                week_number=wn,
            )
            week_tag = f"Week{wn}" if wn else ws.isoformat()
            filename = f"Roster_{month_year or ''}_{week_tag}.xlsx".replace("__", "_")

        return send_file(
            io.BytesIO(file_bytes),
            as_attachment=True,
            download_name=filename,
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
    except ValueError as e:
        return api_response(400, str(e))
    except Exception as e:
        return api_response(500, f"Template download failed: {str(e)}")
    finally:
        cursor.close()
        conn.close()


@roster_bp.route("/excel/preview", methods=["POST"])
def roster_excel_preview():
    """Upload Excel and return change preview (no writes)."""
    logged_in_user_id = request.form.get("logged_in_user_id")
    if not logged_in_user_id:
        return api_response(400, "logged_in_user_id is required")

    uploaded = request.files.get("file") or request.files.get("roster_file")
    if not uploaded or not uploaded.filename:
        return api_response(400, "Excel file is required")

    team_id = request.form.get("team_id")

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        ctx = get_role_context(cursor, int(logged_in_user_id))
        role_name = ctx.get("user_role_name", "")
        if not can_manage_roster_employees(role_name):
            return api_response(403, "You do not have permission to manage rosters")

        file_bytes = uploaded.read()
        preview = _build_excel_preview(
            cursor,
            logged_in_user_id=int(logged_in_user_id),
            role_name=role_name,
            file_bytes=file_bytes,
            team_id=team_id,
        )
        return api_response(200, "Roster Excel preview ready", preview)
    except ValueError as e:
        return api_response(400, str(e))
    except Exception as e:
        return api_response(500, f"Excel preview failed: {str(e)}")
    finally:
        cursor.close()
        conn.close()


@roster_bp.route("/excel/apply", methods=["POST"])
def roster_excel_apply():
    """
    Apply confirmed Excel changes as pending change requests.
    Body JSON: { changes: [ { roster_month_id, change_type, change_payload }, ... ] }
    Or multipart with file (re-parse) + apply_all=1.
    """
    data = request.get_json(silent=True)
    using_file = False
    if data is None:
        using_file = True
        logged_in_user_id = request.form.get("logged_in_user_id")
        if not logged_in_user_id:
            return api_response(400, "logged_in_user_id is required")
        logged_in_user_id = int(logged_in_user_id)
        team_id = request.form.get("team_id")
        uploaded = request.files.get("file") or request.files.get("roster_file")
        if not uploaded or not uploaded.filename:
            return api_response(400, "Excel file or changes payload is required")
        file_bytes = uploaded.read()
        data = {"logged_in_user_id": logged_in_user_id, "team_id": team_id}
    else:
        logged_in_user_id, err = _require_logged_in_user(data)
        if err:
            return err

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        ctx = get_role_context(cursor, int(logged_in_user_id))
        role_name = ctx.get("user_role_name", "")
        if not can_manage_roster_employees(role_name):
            return api_response(403, "You do not have permission to manage rosters")

        if using_file:
            preview = _build_excel_preview(
                cursor,
                logged_in_user_id=int(logged_in_user_id),
                role_name=role_name,
                file_bytes=file_bytes,
                team_id=data.get("team_id"),
            )
            if preview["errors"]:
                return api_response(
                    400,
                    "Fix Excel errors before applying",
                    {
                        "summary": preview["summary"],
                        "errors": preview["errors"],
                    },
                )
            change_rows = preview["changes"]
        else:
            change_rows = data.get("changes") or []

        if not change_rows:
            return api_response(400, "No changes to apply")

        created = 0
        updated = 0
        failed: list[dict] = []
        request_ids: list[int] = []

        for item in change_rows:
            try:
                roster_month_id = int(item["roster_month_id"])
                change_type = (item.get("change_type") or "").strip()
                change_payload = item.get("change_payload") or {}

                roster_month = get_roster_month(cursor, roster_month_id)
                if not roster_month:
                    failed.append({"date": item.get("date"), "reason": "Roster month not found"})
                    continue

                scope_err = assert_manager_scope(
                    cursor, int(logged_in_user_id), role_name, roster_month
                )
                if scope_err:
                    failed.append(
                        {
                            "user_name": item.get("user_name"),
                            "date": item.get("date"),
                            "reason": scope_err,
                        }
                    )
                    continue

                ok, msg = _validate_change_dates_editable(
                    cursor, roster_month, change_type, change_payload
                )
                if not ok:
                    failed.append(
                        {
                            "user_name": item.get("user_name"),
                            "date": item.get("date"),
                            "reason": msg,
                        }
                    )
                    continue

                if roster_month.get("status") == "Pending Approval":
                    failed.append(
                        {
                            "user_name": item.get("user_name"),
                            "date": item.get("date"),
                            "reason": "Pending Approval — withdraw first",
                        }
                    )
                    continue

                if change_type in ("LEAVE_ADD", "LEAVE_UPDATE"):
                    request_id, was_created = create_or_update_leave_request(
                        cursor,
                        roster_month_id=roster_month_id,
                        user_id=int(roster_month["user_id"]),
                        change_type=change_type,
                        change_payload=change_payload,
                        submitted_by=int(logged_in_user_id),
                    )
                    if was_created:
                        created += 1
                    else:
                        updated += 1
                    request_ids.append(int(request_id))
                else:
                    request_id = create_change_request(
                        cursor,
                        roster_month_id=roster_month_id,
                        user_id=int(roster_month["user_id"]),
                        change_type=change_type,
                        change_payload=change_payload,
                        submitted_by=int(logged_in_user_id),
                        batch_id=None,
                    )
                    created += 1
                    request_ids.append(int(request_id))
            except Exception as row_err:
                failed.append(
                    {
                        "user_name": item.get("user_name"),
                        "date": item.get("date"),
                        "reason": str(row_err),
                    }
                )

        conn.commit()
        if created + updated == 0 and failed:
            lock_fail = next(
                (
                    f
                    for f in failed
                    if "is locked" in str(f.get("reason") or "").lower()
                    or "cannot edit this week" in str(f.get("reason") or "").lower()
                ),
                None,
            )
            if lock_fail and all(
                "locked" in str(f.get("reason") or "").lower()
                or "cannot edit this week" in str(f.get("reason") or "").lower()
                for f in failed
            ):
                return api_response(
                    400,
                    lock_fail.get("reason")
                    or "This week is locked. You cannot edit it until an administrator unlocks it.",
                    {
                        "created": 0,
                        "updated": 0,
                        "failed": failed,
                        "request_ids": [],
                    },
                )

        return api_response(
            200,
            "Roster Excel changes applied as pending requests",
            {
                "created": created,
                "updated": updated,
                "failed": failed,
                "request_ids": request_ids,
            },
        )
    except ValueError as e:
        conn.rollback()
        return api_response(400, str(e))
    except Exception as e:
        conn.rollback()
        return api_response(500, f"Excel apply failed: {str(e)}")
    finally:
        cursor.close()
        conn.close()
