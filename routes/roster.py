# routes/roster.py

from __future__ import annotations

from datetime import date

from flask import Blueprint, request

from config import get_db_connection
from utils.response import api_response
from utils.roster_helpers import (
    clear_change_requests_for_month,
    can_manage_roster_employees,
    deactivate_active_rosters_for_employee,
    deactivate_active_rosters_for_month,
    deactivate_roster_month,
    employee_already_has_roster,
    enrich_roster_day_for_response,
    get_eligible_employees,
    get_role_context,
    insert_roster_for_employee,
    is_admin_or_super_admin,
    is_last_week_of_month,
    is_self_read_only_roster_role,
    can_reset_regenerate_roster,
    load_active_holidays,
    load_tracker_baselines_map,
    fill_missing_umt_for_roster_month,
    FULL_DAY_HOURS,
    format_ist_display,
    month_calendar_has_lock,
    month_date_range,
    month_year_label,
    next_calendar_month,
    parse_month_year,
    require_logged_in_user,
    write_audit_log,
)
from utils.roster_week_lock import list_week_locks
from utils.roster_metrics import (
    apply_active_leaves_to_days,
    recalculate_metrics_from_days_and_leaves,
)
from utils.roster_workflow import get_roster_leaves, reconcile_month_goals_to_universal

roster_bp = Blueprint("roster", __name__)


def _require_logged_in_user(data: dict) -> tuple[int | None, dict | None]:
    """Wrapper around shared utility for consistency."""
    user_id, err = require_logged_in_user(data)
    if err:
        return None, api_response(err.get("status", 400), err.get("error", "Authentication required"))
    return user_id, None


def _resolve_target_month(
    data: dict, *, required: bool = False
) -> tuple[int, int, str, tuple | None]:
    month_year = (data.get("month_year") or "").strip()
    if not month_year:
        if required:
            return (
                0,
                0,
                "",
                api_response(
                    400,
                    "month_year is required (e.g. JUL2026). Select the month in the roster screen first.",
                ),
            )
        year, month = next_calendar_month()
        return year, month, month_year_label(year, month), None

    try:
        year, month = parse_month_year(month_year)
        return year, month, month_year_label(year, month), None
    except ValueError as e:
        return 0, 0, "", api_response(400, str(e))


@roster_bp.route("/can_generate", methods=["POST"])
def can_generate_roster():
    """
    Check whether roster generation is available.

    - No month_year: next calendar month (Generate Next Month), enabled last week only.
    - With month_year: selected month (Generate Missing), always available.
    """
    data = request.get_json(silent=True) or {}
    logged_in_user_id, err = _require_logged_in_user(data)
    if err:
        return err

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        ctx = get_role_context(cursor, logged_in_user_id)
        if not ctx.get("user_role_name"):
            return api_response(404, "User not found")

        explicit_month = bool((data.get("month_year") or "").strip())
        target_year, target_month, target_month_year, month_err = _resolve_target_month(
            data, required=explicit_month
        )
        if month_err:
            return month_err

        last_week = is_last_week_of_month()
        if explicit_month:
            return api_response(
                200,
                "Generate availability checked",
                {
                    "can_generate": True,
                    "target_month_year": target_month_year,
                    "generate_mode": "selected_month",
                    "is_last_week_of_month": last_week,
                },
            )

        return api_response(
            200,
            "Generate availability checked",
            {
                "can_generate": last_week,
                "target_month_year": target_month_year,
                "generate_mode": "next_month",
                "is_last_week_of_month": last_week,
            },
        )
    finally:
        cursor.close()
        conn.close()


@roster_bp.route("/generate", methods=["POST"])
def generate_roster():
    """
    Default generation: only employees without an existing roster for the target month.
    Super Admin/Admin: all eligible employees in scope.
    PM/AM: only employees in their existing HRMS assignment scope.
    """
    data = request.get_json(silent=True) or {}
    logged_in_user_id, err = _require_logged_in_user(data)
    if err:
        return err

    explicit_month = bool((data.get("month_year") or "").strip())
    if not explicit_month and not is_last_week_of_month():
        return api_response(
            400,
            "Roster generation is only allowed during the last week of the current month",
        )

    target_year, target_month, target_month_year, month_err = _resolve_target_month(
        data, required=explicit_month
    )
    if month_err:
        return month_err

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        ctx = get_role_context(cursor, logged_in_user_id)
        role_name = ctx.get("user_role_name", "")
        if not role_name:
            return api_response(404, "User not found")
        if not can_manage_roster_employees(role_name):
            return api_response(403, "You do not have permission to generate rosters")

        employees = get_eligible_employees(
            cursor, logged_in_user_id, role_name, target_year, target_month
        )
        holidays = load_active_holidays(cursor, target_year)
        month_start, month_end = month_date_range(target_year, target_month)

        created = []
        skipped = []

        for employee in employees:
            if employee_already_has_roster(cursor, employee["user_id"], target_month_year):
                skipped.append(
                    {
                        "user_id": employee["user_id"],
                        "user_name": employee.get("user_name"),
                        "reason": "roster already exists for this month",
                    }
                )
                continue

            result = insert_roster_for_employee(
                cursor,
                employee,
                target_month_year,
                month_start,
                month_end,
                holidays,
                logged_in_user_id,
            )
            if result.get("status") == "created":
                created.append(result)
            else:
                skipped.append(result)

        goals_created = fill_missing_umt_for_roster_month(
            cursor, target_month_year, logged_in_user_id
        )
        cap_result = reconcile_month_goals_to_universal(
            cursor, target_month_year, logged_in_user_id
        )

        conn.commit()
        return api_response(
            200,
            "Roster generation completed",
            {
                "month_year": target_month_year,
                "created_count": len(created),
                "skipped_count": len(skipped),
                "monthly_goals_created": goals_created,
                "universal_working_days": cap_result.get("universal_working_days"),
                "goals_capped": cap_result,
                "created": created,
                "skipped": skipped,
            },
        )
    except Exception as e:
        conn.rollback()
        return api_response(500, f"Roster generation failed: {str(e)}")
    finally:
        cursor.close()
        conn.close()


@roster_bp.route("/generate_employee", methods=["POST"])
def generate_roster_for_employee():
    """
    Generate roster for a single employee only.
    Used for mid-month joins or filling missing rosters.
    Does not modify any other employee rosters.
    """
    data = request.get_json(silent=True) or {}
    logged_in_user_id, err = _require_logged_in_user(data)
    if err:
        return err

    target_user_id = data.get("user_id")
    if not target_user_id:
        return api_response(400, "user_id is required")

    target_year, target_month, target_month_year, month_err = _resolve_target_month(
        data, required=True
    )
    if month_err:
        return month_err

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        ctx = get_role_context(cursor, logged_in_user_id)
        role_name = ctx.get("user_role_name", "")
        if not role_name:
            return api_response(404, "User not found")
        if not can_manage_roster_employees(role_name):
            return api_response(403, "You do not have permission to generate rosters")

        employees = get_eligible_employees(
            cursor,
            logged_in_user_id,
            role_name,
            target_year,
            target_month,
            user_id=int(target_user_id),
        )
        if not employees:
            return api_response(403, "You do not have permission to manage this employee")

        if employee_already_has_roster(cursor, int(target_user_id), target_month_year):
            return api_response(409, "Roster already exists for this employee and month")

        holidays = load_active_holidays(cursor, target_year)
        month_start, month_end = month_date_range(target_year, target_month)

        result = insert_roster_for_employee(
            cursor,
            employees[0],
            target_month_year,
            month_start,
            month_end,
            holidays,
            logged_in_user_id,
        )
        if result.get("status") != "created":
            conn.rollback()
            return api_response(400, result.get("reason", "Roster generation failed"), result)

        fill_missing_umt_for_roster_month(cursor, target_month_year, logged_in_user_id)
        reconcile_month_goals_to_universal(cursor, target_month_year, logged_in_user_id)
        conn.commit()
        return api_response(200, "Employee roster generated successfully", result)
    except Exception as e:
        conn.rollback()
        return api_response(500, f"Employee roster generation failed: {str(e)}")
    finally:
        cursor.close()
        conn.close()


@roster_bp.route("/reset_regenerate", methods=["POST"])
def reset_regenerate_roster():
    """
    Super Admin only. Replaces all roster records for the month.
    Pending requests are cancelled (not deleted). Audit history is preserved.
    """
    data = request.get_json(silent=True) or {}
    logged_in_user_id, err = _require_logged_in_user(data)
    if err:
        return err

    if not data.get("confirm_reset"):
        return api_response(400, "confirm_reset=true is required for reset and regenerate")

    target_year, target_month, target_month_year, month_err = _resolve_target_month(
        data, required=True
    )
    if month_err:
        return month_err

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        ctx = get_role_context(cursor, logged_in_user_id)
        role_name = ctx.get("user_role_name", "")
        if not can_reset_regenerate_roster(role_name):
            return api_response(403, "Only Admin or Super Admin can reset and regenerate a roster month")

        employees = get_eligible_employees(
            cursor, logged_in_user_id, role_name, target_year, target_month
        )
        holidays = load_active_holidays(cursor, target_year)
        month_start, month_end = month_date_range(target_year, target_month)

        deactivated_ids = deactivate_active_rosters_for_month(cursor, target_month_year)
        if deactivated_ids:
            write_audit_log(
                cursor,
                roster_month_id=None,
                user_id=None,
                action="ROSTER_DEACTIVATED_FOR_REGENERATION",
                entity_type="roster_month",
                entity_id=None,
                old_value={"month_year": target_month_year, "count": len(deactivated_ids)},
                new_value={"is_active": 0},
                performed_by=logged_in_user_id,
                notes="Bulk deactivated due to month reset and regenerate",
            )

        cleared_count = clear_change_requests_for_month(
            cursor, target_month_year, logged_in_user_id
        )

        tracker_map = load_tracker_baselines_map(
            cursor,
            [int(e["user_id"]) for e in employees],
            target_month_year,
        )

        default_tracker = {
            "daily_full_hours": FULL_DAY_HOURS,
            "extra_assigned_hours": 0.0,
            "from_tracker": False,
        }
        created_count = 0
        skipped_count = 0
        skip_reasons: dict[str, int] = {}
        for employee in employees:
            result = insert_roster_for_employee(
                cursor,
                employee,
                target_month_year,
                month_start,
                month_end,
                holidays,
                logged_in_user_id,
                tracker_baseline=tracker_map.get(int(employee["user_id"]), default_tracker),
                write_audit=False,
            )
            if result.get("status") == "created":
                created_count += 1
            else:
                skipped_count += 1
                reason = result.get("reason") or "skipped"
                skip_reasons[reason] = skip_reasons.get(reason, 0) + 1

        write_audit_log(
            cursor,
            roster_month_id=None,
            user_id=None,
            action="ROSTER_MONTH_RESET_REGENERATED",
            entity_type="roster_month",
            entity_id=None,
            old_value={"month_year": target_month_year},
            new_value={
                "created_count": created_count,
                "skipped_count": skipped_count,
                "cleared_change_requests": cleared_count,
                "deactivated_count": len(deactivated_ids),
                "skip_reasons": skip_reasons,
            },
            performed_by=logged_in_user_id,
            notes="Super Admin reset and regenerated roster month",
        )

        fill_missing_umt_for_roster_month(cursor, target_month_year, logged_in_user_id)
        reconcile_month_goals_to_universal(cursor, target_month_year, logged_in_user_id)

        conn.commit()
        return api_response(
            200,
            "Roster month reset and regenerated successfully",
            {
                "month_year": target_month_year,
                "cleared_change_requests": cleared_count,
                "deactivated_count": len(deactivated_ids),
                "created_count": created_count,
                "skipped_count": skipped_count,
                "skip_reasons": skip_reasons,
            },
        )
    except Exception as e:
        conn.rollback()
        return api_response(500, f"Reset and regenerate failed: {str(e)}")
    finally:
        cursor.close()
        conn.close()


@roster_bp.route("/reset_regenerate_employee", methods=["POST"])
def reset_regenerate_employee_roster():
    """
    Super Admin only. Reset & regenerate roster for one employee in a month.
    Clears that employee's change-request history for the month (pending/approved/rejected).
    """
    data = request.get_json(silent=True) or {}
    logged_in_user_id, err = _require_logged_in_user(data)
    if err:
        return err

    if not data.get("confirm_reset"):
        return api_response(400, "confirm_reset=true is required for reset and regenerate")

    target_user_id = data.get("user_id")
    if not target_user_id:
        return api_response(400, "user_id is required")

    target_year, target_month, target_month_year, month_err = _resolve_target_month(
        data, required=True
    )
    if month_err:
        return month_err

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        ctx = get_role_context(cursor, logged_in_user_id)
        role_name = ctx.get("user_role_name", "")
        if not can_reset_regenerate_roster(role_name):
            return api_response(403, "Only Admin or Super Admin can reset and regenerate a roster")

        employees = get_eligible_employees(
            cursor,
            logged_in_user_id,
            role_name,
            target_year,
            target_month,
            user_id=int(target_user_id),
        )
        if not employees:
            return api_response(404, "Employee not found or not eligible for roster")

        employee = employees[0]
        holidays = load_active_holidays(cursor, target_year)
        month_start, month_end = month_date_range(target_year, target_month)

        cleared_count = clear_change_requests_for_month(
            cursor,
            target_month_year,
            logged_in_user_id,
            user_id=int(target_user_id),
        )
        deactivated_ids = deactivate_active_rosters_for_employee(
            cursor, int(target_user_id), target_month_year
        )

        tracker_map = load_tracker_baselines_map(
            cursor, [int(target_user_id)], target_month_year
        )
        default_tracker = {
            "daily_full_hours": FULL_DAY_HOURS,
            "extra_assigned_hours": 0.0,
            "from_tracker": False,
        }
        result = insert_roster_for_employee(
            cursor,
            employee,
            target_month_year,
            month_start,
            month_end,
            holidays,
            logged_in_user_id,
            tracker_baseline=tracker_map.get(int(target_user_id), default_tracker),
            write_audit=True,
        )
        if result.get("status") != "created":
            conn.rollback()
            return api_response(
                400,
                result.get("reason", "Employee roster reset failed"),
                result,
            )

        write_audit_log(
            cursor,
            roster_month_id=result.get("roster_month_id"),
            user_id=int(target_user_id),
            action="ROSTER_EMPLOYEE_RESET_REGENERATED",
            entity_type="roster_month",
            entity_id=result.get("roster_month_id"),
            old_value={"month_year": target_month_year, "deactivated": deactivated_ids},
            new_value={
                "cleared_change_requests": cleared_count,
                "roster_month_id": result.get("roster_month_id"),
            },
            performed_by=logged_in_user_id,
            notes="Super Admin reset and regenerated single employee roster",
        )

        conn.commit()
        return api_response(
            200,
            "Employee roster reset and regenerated successfully",
            {
                "month_year": target_month_year,
                "user_id": int(target_user_id),
                "user_name": employee.get("user_name"),
                "cleared_change_requests": cleared_count,
                "deactivated_count": len(deactivated_ids),
                "roster": result,
            },
        )
    except Exception as e:
        conn.rollback()
        return api_response(500, f"Employee reset and regenerate failed: {str(e)}")
    finally:
        cursor.close()
        conn.close()


@roster_bp.route("/list_employees", methods=["POST"])
def list_roster_employees():
    """
    Eligible Agents and QA in the logged-in user's roster scope.
    Used to populate the employee dropdown on Roster Management.
    """
    data = request.get_json(silent=True) or {}
    logged_in_user_id, err = _require_logged_in_user(data)
    if err:
        return err

    month_year = (data.get("month_year") or "").strip()
    if not month_year:
        return api_response(400, "month_year is required")

    team_id = data.get("team_id")

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        ctx = get_role_context(cursor, logged_in_user_id)
        role_name = ctx.get("user_role_name", "")
        if not role_name:
            return api_response(404, "User not found")

        if is_self_read_only_roster_role(role_name):
            return api_response(403, "You cannot list employees for roster management")

        try:
            year, month = parse_month_year(month_year)
        except ValueError as e:
            return api_response(400, str(e))

        employees = get_eligible_employees(
            cursor, logged_in_user_id, role_name, year, month
        )

        if team_id not in (None, "", "all"):
            team_id_str = str(team_id)
            employees = [
                e
                for e in employees
                if str(e.get("team_id") or "") == team_id_str
            ]

        payload = [
            {
                "user_id": e["user_id"],
                "user_name": e["user_name"],
                "role_name": e.get("role_name"),
                "team_id": e.get("team_id"),
                "team_name": e.get("team_name"),
            }
            for e in employees
        ]
        return api_response(200, "Employees fetched successfully", {"employees": payload})
    finally:
        cursor.close()
        conn.close()


@roster_bp.route("/list", methods=["POST"])
def list_rosters():
    data = request.get_json(silent=True) or {}
    logged_in_user_id, err = _require_logged_in_user(data)
    if err:
        return err

    month_year = (data.get("month_year") or "").strip()
    if not month_year:
        return api_response(400, "month_year is required")

    target_user_id = data.get("user_id")
    include_days = bool(data.get("include_days", False))

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        ctx = get_role_context(cursor, logged_in_user_id)
        role_name = ctx.get("user_role_name", "")
        if not role_name:
            return api_response(404, "User not found")

        try:
            year, month = parse_month_year(month_year)
        except ValueError as e:
            return api_response(400, str(e))

        if role_name == "agent" or role_name == "qa":
            target_user_id = logged_in_user_id
        elif target_user_id:
            if is_self_read_only_roster_role(role_name):
                return api_response(403, "You can only view your own roster")
            allowed = get_eligible_employees(
                cursor, logged_in_user_id, role_name, year, month, user_id=int(target_user_id)
            )
            if not allowed and not is_admin_or_super_admin(role_name):
                return api_response(403, "You do not have permission to view this employee roster")
        else:
            if is_self_read_only_roster_role(role_name):
                return api_response(403, "You can only view your own roster")
            if not is_admin_or_super_admin(role_name) and role_name not in (
                "project manager",
                "assistant manager",
            ):
                return api_response(403, "You do not have permission to list rosters")

        where = "WHERE rm.month_year=%s AND rm.is_active=1"
        params: list = [month_year]

        if target_user_id:
            where += " AND rm.user_id=%s"
            params.append(int(target_user_id))
        elif not is_admin_or_super_admin(role_name):
            scoped_employees = get_eligible_employees(
                cursor, logged_in_user_id, role_name, year, month
            )
            scoped_ids = [int(e["user_id"]) for e in scoped_employees]
            if not scoped_ids:
                return api_response(200, "Rosters fetched successfully", [])
            placeholders = ",".join(["%s"] * len(scoped_ids))
            where += f" AND rm.user_id IN ({placeholders})"
            params.extend(scoped_ids)

        cursor.execute(
            f"""
            SELECT
                rm.roster_month_id,
                rm.user_id,
                u.user_name,
                rm.month_year,
                rm.status,
                rm.roster_start_date,
                rm.roster_end_date,
                rm.baseline_target_days,
                rm.calendar_working_days,
                rm.target_working_days,
                rm.monthly_target_hours,
                COALESCE(umt.extra_assigned_hours, rm.extra_assigned_hours) AS extra_assigned_hours,
                rm.created_by,
                rm.created_date,
                rm.approved_by,
                rm.approved_date,
                rm.locked_by,
                rm.locked_date,
                locker.user_name AS locked_by_name
            FROM roster_month rm
            JOIN tfs_user u ON u.user_id = rm.user_id
            LEFT JOIN tfs_user locker ON locker.user_id = rm.locked_by
            LEFT JOIN user_monthly_tracker umt
              ON umt.user_id = rm.user_id
             AND umt.month_year = rm.month_year
             AND umt.is_active = 1
            {where}
            ORDER BY u.user_name ASC
            """,
            tuple(params),
        )
        rows = cursor.fetchall() or []
        holiday_lookup: dict[int, dict] = {}
        if include_days:
            holidays = load_active_holidays(cursor, year)
            holiday_lookup = {
                int(h["holiday_id"]): h for h in holidays.values() if h.get("holiday_id") is not None
            }

        read_only = is_self_read_only_roster_role(role_name)

        for row in rows:
            row["access_mode"] = "read_only" if read_only else "manage"
            if row.get("locked_date"):
                row["locked_date_display"] = format_ist_display(row["locked_date"])
            for key in ("roster_start_date", "roster_end_date", "locked_date"):
                if row.get(key):
                    row[key] = row[key].isoformat()

            if include_days:
                cursor.execute(
                    """
                    SELECT
                        roster_day_id, roster_date, day_type, shift,
                        shift_start, shift_end, working_type, working_hours,
                        holiday_id, leave_id
                    FROM roster_day
                    WHERE roster_month_id=%s AND is_active=1
                    ORDER BY roster_date ASC
                    """,
                    (int(row["roster_month_id"]),),
                )
                days = cursor.fetchall() or []
                leaves = get_roster_leaves(cursor, int(row["roster_month_id"]))
                days_for_metrics = apply_active_leaves_to_days(days, leaves)
                metrics = recalculate_metrics_from_days_and_leaves(days_for_metrics, leaves)
                row["days"] = [
                    enrich_roster_day_for_response(day, holiday_lookup)
                    for day in apply_active_leaves_to_days(days, leaves)
                ]
                row["calendar_working_days"] = metrics["calendar_working_days"]
                row["target_working_days"] = metrics["target_working_days"]
                row["monthly_target_hours"] = metrics["monthly_target_hours"]

        lock_info = month_calendar_has_lock(cursor, month_year)
        month_calendar_locked = lock_info is not None
        week_locks = list_week_locks(cursor, month_year)

        return api_response(
            200,
            "Rosters fetched successfully",
            {
                "access_mode": "read_only" if read_only else "manage",
                "month_calendar_locked": month_calendar_locked,
                "month_lock_info": {
                    "locked_by_name": lock_info.get("locked_by_name") if lock_info else None,
                    "locked_date": lock_info.get("locked_date").isoformat()
                    if lock_info and lock_info.get("locked_date")
                    else None,
                    "locked_date_display": format_ist_display(lock_info.get("locked_date"))
                    if lock_info
                    else None,
                }
                if lock_info
                else None,
                "week_locks": week_locks,
                "rosters": rows,
            },
        )
    except Exception as e:
        return api_response(500, f"Failed to fetch rosters: {str(e)}")
    finally:
        cursor.close()
        conn.close()
