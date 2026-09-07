"""Send weekly roster HTML email after admin approval."""

from __future__ import annotations

from datetime import date
from html import escape

from utils.email_utils import send_email
from utils.roster_week_lock import month_has_pending_submitted_requests
from utils.roster_excel import (
    LABEL_HALF_DAY,
    LABEL_HOLIDAY,
    LABEL_WEEK_OFF,
    day_to_excel_label,
    format_day_header,
    month_years_for_dates,
    week_dates,
)
from utils.roster_helpers import get_excel_roster_employees, parse_date, parse_month_year
from utils.roster_metrics import apply_active_leaves_to_days
from utils.roster_workflow import get_roster_leaves

TEAM_COLORS = ["#F8CBAD", "#D5A6E6", "#F4CCCC", "#D9EAD3"]
HEADER_BG = "#E69138"
WEEK_OFF_BG = "#9FC5E8"
LEAVE_BG = "#FFE599"
BORDER = "#000000"


APPROVER_ROLES = ("admin", "super admin")
WEEKLY_ROSTER_ROLES = (
    "qa",
    "assistant manager",
    "project manager",
    "admin",
    "super admin",
)


def _active_emails_for_roles(cursor, role_names: tuple[str, ...]) -> list[str]:
    """Emails for active, not-deleted users in the given roles."""
    if not role_names:
        return []
    placeholders = ",".join(["%s"] * len(role_names))
    cursor.execute(
        f"""
        SELECT DISTINCT u.user_email
        FROM tfs_user u
        JOIN user_role r ON r.role_id = u.role_id
        WHERE u.is_active=1 AND u.is_delete=1
          AND LOWER(TRIM(r.role_name)) IN ({placeholders})
          AND u.user_email IS NOT NULL AND TRIM(u.user_email) != ''
        """,
        tuple(role_names),
    )
    emails = []
    seen = set()
    for row in cursor.fetchall() or []:
        email = (row.get("user_email") or "").strip()
        key = email.lower()
        if email and key not in seen:
            seen.add(key)
            emails.append(email)
    return emails


def get_admin_super_admin_emails(cursor) -> list[str]:
    """Active Admin and Super Admin emails (approvers)."""
    return _active_emails_for_roles(cursor, APPROVER_ROLES)


def roster_weekly_recipients(cursor) -> tuple[list[str], list[str]]:
    """Active QA, AM, PM, Admin, Super Admin — weekly roster To list."""
    return _active_emails_for_roles(cursor, WEEKLY_ROSTER_ROLES), []


def send_roster_approval_needed_email(
    cursor,
    *,
    month_year: str,
    submitted_by: int,
    request_count: int,
    week_label: str | None = None,
) -> dict:
    """
    Short notice to Admin / Super Admin that the approval queue has new work.
    Does not include leave/day details.
    """
    to_list = get_admin_super_admin_emails(cursor)
    if not to_list:
        print("[roster approval email] no Admin/Super Admin emails; skip", flush=True)
        return {"sent": False, "skipped": True, "reason": "No Admin or Super Admin email found"}

    cursor.execute(
        "SELECT user_name FROM tfs_user WHERE user_id=%s LIMIT 1",
        (int(submitted_by),),
    )
    row = cursor.fetchone() or {}
    raw_name = (row.get("user_name") or "A manager").strip() or "A manager"
    raw_month = (month_year or "").strip() or "this month"
    submitter = escape(raw_name)
    month = escape(raw_month)
    if week_label:
        subject = f"Roster pending approval — {raw_month} {week_label}"
        html = f"""
    <div style="font-family:Arial,Helvetica,sans-serif;font-size:14px;color:#111;">
      <p>Hello,</p>
      <p>Roster change requests are waiting for approval.</p>
      <p>Please open HRMS → Roster Management → Approval Queue and approve or reject the pending requests.</p>
      <p style="color:#555;font-size:12px;">Month: {month}<br/>Week: {escape(week_label)}<br/>Pending requests: {int(request_count or 0)}<br/>Sent by: {submitter}</p>
    </div>
    """
    else:
        # Same automated mail used when a manager clicks Submit.
        subject = f"Roster pending approval — {raw_month}"
        html = f"""
    <div style="font-family:Arial,Helvetica,sans-serif;font-size:14px;color:#111;">
      <p>Hello,</p>
      <p>A roster has been submitted and is waiting for approval.</p>
      <p>Please open HRMS → Roster Management → Approval Queue and approve or reject the pending requests.</p>
      <p style="color:#555;font-size:12px;">Month: {month}<br/>Submitted by: {submitter}</p>
    </div>
    """
    try:
        send_email(to_list, subject, html)
        print(f"[roster approval email] sent {subject} to {to_list}", flush=True)
        return {"sent": True, "to": to_list}
    except Exception as err:
        print(f"[roster approval email] failed: {err}", flush=True)
        return {"sent": False, "reason": str(err)}


def _cell_bg(label: str) -> str:
    key = (label or "").strip().lower()
    if key == LABEL_WEEK_OFF.lower():
        return WEEK_OFF_BG
    if key == LABEL_HOLIDAY.lower() or key == "holiday":
        return "#D5A6E6"
    if "leave" in key or "half day" in key or key == LABEL_HALF_DAY.lower():
        return LEAVE_BG
    return "#FFFFFF"


def _format_range(week_start: date, week_end: date) -> str:
    return f"{week_start.strftime('%d-%m-%Y')} to {week_end.strftime('%d-%m-%Y')}"


def _load_days_lookup(cursor, roster_month_ids: list[int]) -> dict[tuple[int, str], dict]:
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


def _is_team_agent(emp: dict) -> bool:
    """Team agent row is the person whose name matches the team name (same as billable report)."""
    name = (emp.get("user_name") or "").strip().lower()
    team = (emp.get("team_name") or "").strip().lower()
    return bool(name and team and name == team)


def _team_sort_key(team_name: str | None) -> tuple:
    """Team A, Team B, … then other names; people with no team last."""
    raw = (team_name or "").strip()
    if not raw:
        return (9, "", "")
    key = raw.lower()
    letter = None
    if key.startswith("team ") and len(raw) > 5:
        letter = raw[5:].strip()[:1].lower()
    elif len(raw) == 1 and raw.isalpha():
        letter = raw.lower()
    if letter and letter.isalpha():
        return (0, letter, key)
    return (1, key, key)


def _employees_team_order(employees: list[dict]) -> list[dict]:
    """Team A then Team B; team agent first inside each team; rest A–Z."""
    return sorted(
        employees,
        key=lambda e: (
            _team_sort_key(e.get("team_name")),
            0 if _is_team_agent(e) else 1,
            (e.get("user_name") or "").lower(),
        ),
    )


def build_weekly_roster_html(
    *,
    week_start: date,
    employees: list[dict],
    day_lookup: dict[tuple[int, str], dict],
) -> str:
    days = week_dates(week_start)
    week_end = days[-1]
    headers = ["Team Member"] + [format_day_header(d) for d in days]

    sorted_emps = _employees_team_order(employees)

    team_color: dict[str, str] = {}
    color_i = 0
    for emp in sorted_emps:
        team_key = (emp.get("team_name") or "").strip() or "No Team"
        if team_key not in team_color:
            team_color[team_key] = TEAM_COLORS[color_i % len(TEAM_COLORS)]
            color_i += 1

    header_cells = "".join(
        f'<th style="border:1px solid {BORDER};background:{HEADER_BG};color:#000;padding:6px 8px;'
        f'text-align:center;font-weight:bold;">{escape(h)}</th>'
        for h in headers
    )

    body_rows = []
    for emp in sorted_emps:
        team_key = (emp.get("team_name") or "").strip() or "No Team"
        name_bg = team_color[team_key]
        name = emp.get("user_name") or f"User {emp.get('user_id')}"
        role = emp.get("role_name")
        uid = int(emp["user_id"])
        cells = [
            f'<td style="border:1px solid {BORDER};background:{name_bg};padding:6px 8px;'
            f'text-align:center;white-space:nowrap;font-weight:{"bold" if _is_team_agent(emp) else "normal"};">'
            f'{escape(name)}</td>'
        ]
        for d in days:
            day = day_lookup.get((uid, d.isoformat()))
            label = day_to_excel_label(day, role)
            if not label:
                if d.weekday() >= 5:
                    label = LABEL_WEEK_OFF
                elif (role or "").strip().lower() == "qa":
                    label = "10:00AM to 7:30PM"
                else:
                    label = "9:00AM to 6:30PM"
            bg = _cell_bg(label)
            cells.append(
                f'<td style="border:1px solid {BORDER};background:{bg};padding:6px 8px;'
                f'text-align:center;">{escape(label)}</td>'
            )
        body_rows.append("<tr>" + "".join(cells) + "</tr>")

    range_label = _format_range(week_start, week_end)
    return f"""
    <div style="font-family:Arial,Helvetica,sans-serif;font-size:13px;color:#000;">
      <p>Dear Team,</p>
      <p>Please find the roster details below for the week {escape(range_label)}.</p>
      <table cellpadding="0" cellspacing="0" style="border-collapse:collapse;border:1px solid {BORDER};">
        <thead><tr>{header_cells}</tr></thead>
        <tbody>
          {"".join(body_rows)}
        </tbody>
      </table>
    </div>
    """


def send_weekly_roster_after_approval(
    cursor,
    *,
    weeks: list[dict],
    logged_in_user_id: int,
    role_name: str,
) -> list[dict]:
    """
    Email the updated week grid for each approved week.
    Failures are returned, they do not raise.
    """
    to_list, cc_list = roster_weekly_recipients(cursor)
    if not to_list:
        print("[roster weekly email] no active QA/AM/PM/Admin emails; skip send", flush=True)
        return [{"skipped": True, "reason": "No active QA, Assistant Manager, Project Manager, Admin, or Super Admin emails found"}]

    pending_months: list[str] = []
    for week in weeks or []:
        extra = (week.get("month_year") or "").strip()
        if extra:
            pending_months.append(extra)
        week_start = parse_date(week.get("week_start"))
        if week_start:
            pending_months.extend(month_years_for_dates(week_dates(week_start)))
    if month_has_pending_submitted_requests(cursor, pending_months):
        print("[roster weekly email] skip — pending approve/reject still on queue", flush=True)
        return [
            {
                "skipped": True,
                "sent": False,
                "deferred": True,
                "reason": "Weekly roster email waits until all pending requests are approved or rejected",
            }
        ]

    results: list[dict] = []
    for week in weeks or []:
        week_start = parse_date(week.get("week_start"))
        if not week_start:
            continue
        days = week_dates(week_start)
        month_years = month_years_for_dates(days)
        extra_my = (week.get("month_year") or "").strip()
        if extra_my and extra_my not in month_years:
            month_years.insert(0, extra_my)

        employees_by_id: dict[int, dict] = {}
        for my in month_years:
            try:
                year, month = parse_month_year(my)
            except ValueError:
                continue
            for emp in get_excel_roster_employees(
                cursor, logged_in_user_id, role_name, year, month
            ):
                employees_by_id[int(emp["user_id"])] = emp

        employees = list(employees_by_id.values())
        if not employees:
            print(f"[roster weekly email] skip week {week_start}: no employees", flush=True)
            results.append({"week": week, "sent": False, "reason": "No employees"})
            continue

        cursor.execute(
            f"""
            SELECT roster_month_id FROM roster_month
            WHERE is_active=1 AND user_id IN ({",".join(["%s"] * len(employees))})
              AND month_year IN ({",".join(["%s"] * len(month_years))})
            """,
            tuple([int(e["user_id"]) for e in employees] + month_years),
        )
        roster_ids = [int(r["roster_month_id"]) for r in (cursor.fetchall() or [])]
        day_lookup = _load_days_lookup(cursor, roster_ids)
        range_label = _format_range(week_start, days[-1])
        subject = f"Weekly Roster {range_label}"
        try:
            html = build_weekly_roster_html(
                week_start=week_start,
                employees=employees,
                day_lookup=day_lookup,
            )
            send_email(to_list, subject, html, cc=cc_list)
            results.append({"week": week, "sent": True, "to": to_list, "cc": cc_list})
            print(f"[roster weekly email] sent {subject} to {to_list}", flush=True)
        except Exception as err:
            print(f"[roster weekly email] failed {subject}: {err}", flush=True)
            results.append({"week": week, "sent": False, "reason": str(err)})
    if not results:
        print("[roster weekly email] no weeks to email", flush=True)
        return [{"skipped": True, "sent": False, "reason": "No weeks found on the approved requests"}]
    return results
