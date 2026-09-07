# utils/roster_week_lock.py
"""Weekly roster lock helpers — lock Mon–Sun weeks after admin approval."""

from __future__ import annotations

import json
from datetime import date, timedelta

from utils.roster_excel import month_years_for_dates, week_dates, weeks_in_month
from utils.roster_helpers import format_ist_display, parse_date, parse_month_year, now_str


_TABLE_READY = False


def ensure_week_lock_table(cursor) -> None:
    """Create roster_week_lock if missing (safe to call repeatedly)."""
    global _TABLE_READY
    if _TABLE_READY:
        return
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS roster_week_lock (
          week_lock_id INT NOT NULL AUTO_INCREMENT,
          month_year VARCHAR(16) NOT NULL,
          week_number INT NOT NULL,
          week_start DATE NOT NULL,
          week_end DATE NOT NULL,
          locked_by INT NOT NULL,
          locked_date DATETIME NOT NULL,
          PRIMARY KEY (week_lock_id),
          UNIQUE KEY uq_roster_week_lock (month_year, week_number),
          KEY idx_roster_week_lock_month (month_year)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """
    )
    _TABLE_READY = True


def week_meta_for_number(month_year: str, week_number: int) -> dict | None:
    """Return Week 1..N meta for this month, or None if the number is out of range."""
    if not month_year:
        return None
    try:
        year, month = parse_month_year(month_year)
        wn = int(week_number)
    except (TypeError, ValueError):
        return None
    if wn <= 0:
        return None
    for w in weeks_in_month(year, month):
        if int(w.get("week_number") or 0) == wn:
            return w
    return None


def week_meta_for_date(month_year: str, d: date) -> dict | None:
    """Return Week 1..N meta for a date within month_year, or None."""
    if not month_year or not d:
        return None
    try:
        year, month = parse_month_year(month_year)
    except ValueError:
        return None
    for w in weeks_in_month(year, month):
        ws = parse_date(w.get("week_start"))
        we = parse_date(w.get("week_end"))
        if ws and we and ws <= d <= we:
            return w
    return None


def dates_from_change_payload(change_type: str, payload: dict | None) -> list[date]:
    """Collect roster dates touched by a change request payload."""
    payload = payload or {}
    change_type = (change_type or "").strip().upper()
    out: list[date] = []

    def add(val) -> None:
        d = parse_date(val)
        if d:
            out.append(d)

    if change_type in ("DAY_UPDATE", "EXTRA_HOURS_UPDATE", "LEAVE_DELETE"):
        add(payload.get("roster_date"))
        apply_rest = payload.get("apply_through_month_end")
        if change_type == "DAY_UPDATE" and (
            apply_rest is True or str(apply_rest).strip() in ("1", "true", "True")
        ):
            start = parse_date(payload.get("roster_date"))
            end = parse_date(
                payload.get("through_end_date") or payload.get("roster_end_date")
            )
            if start and end and end >= start:
                cur = start
                while cur <= end:
                    out.append(cur)
                    cur += timedelta(days=1)
    elif change_type in ("LEAVE_ADD", "LEAVE_UPDATE"):
        start = parse_date(payload.get("start_date"))
        end = parse_date(payload.get("end_date")) or start
        if start and end:
            if end < start:
                start, end = end, start
            cur = start
            while cur <= end:
                out.append(cur)
                cur += timedelta(days=1)
    elif change_type == "WEEKOFF_SWAP":
        for key in ("week_off_dates", "new_week_off_dates", "proposed_week_off_dates"):
            for item in payload.get(key) or []:
                add(item)
        for c in payload.get("changes") or []:
            if isinstance(c, dict):
                add(c.get("roster_date"))

    # Deduplicate while preserving order
    seen: set[date] = set()
    unique: list[date] = []
    for d in out:
        if d not in seen:
            seen.add(d)
            unique.append(d)
    return unique


def request_touches_week(req: dict, week: dict) -> bool:
    """True if this change request has any date in the week's Mon–Sun range."""
    ws = parse_date(week.get("week_start"))
    we = parse_date(week.get("week_end"))
    if not ws or not we:
        return False
    for d in dates_from_change_request(req):
        if ws <= d <= we:
            return True
    return False


def dates_from_change_request(req: dict) -> list[date]:
    payload = req.get("change_payload")
    if isinstance(payload, (bytes, bytearray)):
        payload = payload.decode("utf-8", errors="ignore")
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except Exception:
            payload = {}
    if not isinstance(payload, dict):
        payload = {}
    return dates_from_change_payload(req.get("change_type") or "", payload)


def list_week_locks(cursor, month_year: str) -> list[dict]:
    """Active week locks for a month, enriched for API responses."""
    ensure_week_lock_table(cursor)
    month_year = (month_year or "").strip()
    if not month_year:
        return []
    cursor.execute(
        """
        SELECT
            wl.week_lock_id,
            wl.month_year,
            wl.week_number,
            wl.week_start,
            wl.week_end,
            wl.locked_by,
            wl.locked_date,
            locker.user_name AS locked_by_name
        FROM roster_week_lock wl
        LEFT JOIN tfs_user locker ON locker.user_id = wl.locked_by
        WHERE wl.month_year=%s
        ORDER BY wl.week_number ASC
        """,
        (month_year,),
    )
    rows = cursor.fetchall() or []
    out: list[dict] = []
    for row in rows:
        item = dict(row)
        for key in ("week_start", "week_end"):
            val = item.get(key)
            if val is not None and hasattr(val, "isoformat"):
                item[key] = val.isoformat()
        locked_date = item.get("locked_date")
        if locked_date is not None and hasattr(locked_date, "isoformat"):
            item["locked_date"] = locked_date.isoformat()
        item["locked_date_display"] = format_ist_display(locked_date) if locked_date else None
        wn = int(item.get("week_number") or 0)
        item["label"] = f"Week {wn}"
        item["short_label"] = f"Week {wn}"
        out.append(item)
    return out


def get_week_lock(cursor, month_year: str, week_number: int) -> dict | None:
    ensure_week_lock_table(cursor)
    cursor.execute(
        """
        SELECT
            wl.week_lock_id,
            wl.month_year,
            wl.week_number,
            wl.week_start,
            wl.week_end,
            wl.locked_by,
            wl.locked_date,
            locker.user_name AS locked_by_name
        FROM roster_week_lock wl
        LEFT JOIN tfs_user locker ON locker.user_id = wl.locked_by
        WHERE wl.month_year=%s AND wl.week_number=%s
        LIMIT 1
        """,
        ((month_year or "").strip(), int(week_number)),
    )
    return cursor.fetchone()


def is_week_locked(cursor, month_year: str, week_number: int) -> bool:
    return get_week_lock(cursor, month_year, week_number) is not None


def week_lock_message(lock_row: dict | None, week_meta: dict | None = None) -> str:
    if not lock_row:
        return ""
    wn = int(lock_row.get("week_number") or (week_meta or {}).get("week_number") or 0)
    locker = (lock_row.get("locked_by_name") or "").strip() or "an administrator"
    range_label = (week_meta or {}).get("date_range") or ""
    if not range_label:
        ws = lock_row.get("week_start")
        we = lock_row.get("week_end")
        if ws and we:
            range_label = f"{ws} – {we}"
    suffix = f" ({range_label})" if range_label else ""
    return (
        f"Week {wn}{suffix} is locked by {locker}. "
        "No roster changes can be made until an administrator unlocks this week."
    )


def week_lock_message_for_dates(cursor, month_year: str, dates: list[date]) -> str:
    """Return lock message if any date falls in a locked week for this month."""
    ensure_week_lock_table(cursor)
    month_year = (month_year or "").strip()
    if not month_year or not dates:
        return ""
    locks = {int(r["week_number"]): r for r in list_week_locks(cursor, month_year)}
    if not locks:
        return ""
    for d in dates:
        meta = week_meta_for_date(month_year, d)
        if not meta:
            continue
        wn = int(meta["week_number"])
        if wn in locks:
            return week_lock_message(locks[wn], meta)
    return ""


def week_lock_message_for_change(
    cursor, month_year: str, change_type: str, change_payload: dict | None
) -> str:
    return week_lock_message_for_dates(
        cursor, month_year, dates_from_change_payload(change_type, change_payload)
    )


def lock_week(
    cursor,
    *,
    month_year: str,
    week_number: int,
    week_start: date | str,
    week_end: date | str,
    locked_by: int,
) -> bool:
    """
    Insert week lock if not already locked.
    Returns True if a new lock row was created.
    """
    ensure_week_lock_table(cursor)
    month_year = (month_year or "").strip()
    wn = int(week_number)
    ws = parse_date(week_start)
    we = parse_date(week_end)
    if not month_year or not ws or not we:
        raise ValueError("month_year, week_start, and week_end are required to lock a week")

    if get_week_lock(cursor, month_year, wn):
        return False

    cursor.execute(
        """
        INSERT INTO roster_week_lock
          (month_year, week_number, week_start, week_end, locked_by, locked_date)
        VALUES (%s, %s, %s, %s, %s, %s)
        """,
        (month_year, wn, ws.isoformat(), we.isoformat(), int(locked_by), now_str()),
    )
    return True


def unlock_week(cursor, month_year: str, week_number: int) -> bool:
    """Delete week lock. Returns True if a row was removed."""
    ensure_week_lock_table(cursor)
    cursor.execute(
        """
        DELETE FROM roster_week_lock
        WHERE month_year=%s AND week_number=%s
        """,
        ((month_year or "").strip(), int(week_number)),
    )
    return cursor.rowcount > 0


def lock_weeks_touched_by_requests(
    cursor,
    *,
    requests: list[dict],
    months_by_id: dict[int, dict],
    locked_by: int,
) -> list[dict]:
    """
    After admin approval: lock every week touched by the approved requests.
    Other weeks in the same month remain editable.
    """
    ensure_week_lock_table(cursor)
    locked_out: list[dict] = []
    seen: set[tuple[str, int]] = set()

    for req in requests or []:
        mid = req.get("roster_month_id")
        if mid is None:
            continue
        mid = int(mid)
        roster_month = months_by_id.get(mid)
        if not roster_month:
            continue
        month_year = (roster_month.get("month_year") or "").strip()
        if not month_year:
            continue

        for d in dates_from_change_request(req):
            # Only lock weeks for dates that belong to this roster month
            try:
                my_year, my_month = parse_month_year(month_year)
            except ValueError:
                continue
            if d.year != my_year or d.month != my_month:
                continue

            meta = week_meta_for_date(month_year, d)
            if not meta:
                continue
            wn = int(meta["week_number"])
            key = (month_year, wn)
            if key in seen:
                continue
            seen.add(key)
            created = lock_week(
                cursor,
                month_year=month_year,
                week_number=wn,
                week_start=meta["week_start"],
                week_end=meta["week_end"],
                locked_by=locked_by,
            )
            if created:
                locked_out.append(
                    {
                        "month_year": month_year,
                        "week_number": wn,
                        "week_start": meta["week_start"],
                        "week_end": meta["week_end"],
                        "label": meta.get("label") or f"Week {wn}",
                    }
                )
    return locked_out


def weeks_touched_by_requests(
    requests: list[dict],
    months_by_id: dict[int, dict],
) -> list[dict]:
    """Unique weeks affected by approved change requests (for email / lock)."""
    seen: set[tuple[str, int]] = set()
    out: list[dict] = []
    for req in requests or []:
        mid = req.get("roster_month_id")
        if mid is None:
            continue
        roster_month = months_by_id.get(int(mid))
        if not roster_month:
            continue
        month_year = (roster_month.get("month_year") or "").strip()
        if not month_year:
            continue
        try:
            my_year, my_month = parse_month_year(month_year)
        except ValueError:
            continue
        for d in dates_from_change_request(req):
            if d.year != my_year or d.month != my_month:
                continue
            meta = week_meta_for_date(month_year, d)
            if not meta:
                continue
            wn = int(meta["week_number"])
            key = (month_year, wn)
            if key in seen:
                continue
            seen.add(key)
            out.append(
                {
                    "month_year": month_year,
                    "week_number": wn,
                    "week_start": meta["week_start"],
                    "week_end": meta["week_end"],
                    "label": meta.get("label") or f"Week {wn}",
                }
            )
    return out


def week_has_pending_submitted_requests(cursor, week: dict) -> bool:
    """True if any submitted pending change still touches this Mon–Sun week."""
    ws = parse_date(week.get("week_start"))
    we = parse_date(week.get("week_end"))
    if not ws or not we:
        return False
    month_years: list[str] = []
    extra = (week.get("month_year") or "").strip()
    if extra:
        month_years.append(extra)
    for my in month_years_for_dates(week_dates(ws)):
        if my not in month_years:
            month_years.append(my)
    if not month_years:
        return False
    placeholders = ",".join(["%s"] * len(month_years))
    cursor.execute(
        f"""
        SELECT rcr.change_type, rcr.change_payload
        FROM roster_change_request rcr
        JOIN roster_month rm ON rm.roster_month_id = rcr.roster_month_id
        WHERE rcr.is_active=1
          AND rcr.status='Pending'
          AND rcr.batch_id IS NOT NULL AND TRIM(rcr.batch_id) != ''
          AND rm.is_active=1
          AND rm.month_year IN ({placeholders})
        """,
        tuple(month_years),
    )
    for row in cursor.fetchall() or []:
        for d in dates_from_change_request(row):
            if ws <= d <= we:
                return True
    return False


def month_has_pending_submitted_requests(cursor, month_years: list[str] | None) -> bool:
    """True while the approval queue still has submitted pending rows for these months."""
    months = [str(m).strip() for m in (month_years or []) if str(m).strip()]
    if not months:
        return False
    placeholders = ",".join(["%s"] * len(months))
    cursor.execute(
        f"""
        SELECT COUNT(*) AS n
        FROM roster_change_request rcr
        JOIN roster_month rm ON rm.roster_month_id = rcr.roster_month_id
        WHERE rcr.is_active=1
          AND rcr.status='Pending'
          AND rcr.batch_id IS NOT NULL AND TRIM(rcr.batch_id) != ''
          AND rm.is_active=1
          AND rm.month_year IN ({placeholders})
        """,
        tuple(months),
    )
    return int((cursor.fetchone() or {}).get("n") or 0) > 0


def weeks_from_approved_requests_for_months(cursor, month_years: list[str] | None) -> list[dict]:
    """Weeks touched by approved submitted requests in these months."""
    months = [str(m).strip() for m in (month_years or []) if str(m).strip()]
    if not months:
        return []
    placeholders = ",".join(["%s"] * len(months))
    cursor.execute(
        f"""
        SELECT rcr.*, rm.month_year
        FROM roster_change_request rcr
        JOIN roster_month rm ON rm.roster_month_id = rcr.roster_month_id
        WHERE rcr.is_active=1
          AND rcr.status='Approved'
          AND rcr.batch_id IS NOT NULL AND TRIM(rcr.batch_id) != ''
          AND rm.is_active=1
          AND rm.month_year IN ({placeholders})
        """,
        tuple(months),
    )
    rows = cursor.fetchall() or []
    months_by_id: dict[int, dict] = {}
    for row in rows:
        mid = int(row["roster_month_id"])
        months_by_id[mid] = {
            "roster_month_id": mid,
            "month_year": row.get("month_year"),
        }
    return weeks_touched_by_requests(rows, months_by_id)


def annotate_weeks_with_locks(cursor, month_year: str, weeks: list[dict]) -> list[dict]:
    """Attach is_locked + lock info onto weeks_in_month() rows."""
    locks = {int(r["week_number"]): r for r in list_week_locks(cursor, month_year)}
    out: list[dict] = []
    for w in weeks or []:
        item = dict(w)
        wn = int(item.get("week_number") or 0)
        lock = locks.get(wn)
        item["is_locked"] = lock is not None
        item["lock_info"] = lock
        out.append(item)
    return out
