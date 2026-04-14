from apscheduler.schedulers.background import BackgroundScheduler
from datetime import datetime
from config import get_db_connection
from calendar import monthrange

def get_roster_day(cursor, user_id, date):
    cursor.execute("""
        SELECT * FROM roster_days
        WHERE user_id=%s AND date=%s
    """, (user_id, date))
    return cursor.fetchone()


def upsert_draft(cursor, day, field, value, edited_by):
    old_value = str(day.get(field))

    # Check existing draft
    cursor.execute("""
        SELECT * FROM roster_day_drafts
        WHERE user_id=%s AND date=%s AND field_name=%s AND is_submitted=0
    """, (day["user_id"], day["date"], field))

    existing = cursor.fetchone()

    if existing:
        cursor.execute("""
            UPDATE roster_day_drafts
            SET new_value=%s, edited_by=%s
            WHERE draft_id=%s
        """, (value, edited_by, existing["draft_id"]))
    else:
        cursor.execute("""
            INSERT INTO roster_day_drafts
            (roster_day_id, roster_id, user_id, date,
             field_name, old_value, new_value, edited_by)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
        """, (
            day["roster_day_id"],
            day["roster_id"],
            day["user_id"],
            day["date"],
            field,
            old_value,
            value,
            edited_by
        ))


def get_user_drafts(cursor, edited_by):
    cursor.execute("""
        SELECT * FROM roster_day_drafts
        WHERE edited_by=%s AND is_submitted=0
    """, (edited_by,))
    return cursor.fetchall()


def move_drafts_to_changes(cursor, drafts, edited_by):
    for d in drafts:
        cursor.execute("""
            INSERT INTO roster_day_changes
            (roster_day_id, roster_id, user_id, date,
             field_name, old_value, new_value,
             requested_by)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
        """, (
            d["roster_day_id"],
            d["roster_id"],
            d["user_id"],
            d["date"],
            d["field_name"],
            d["old_value"],
            d["new_value"],
            edited_by
        ))


def mark_drafts_submitted(cursor, edited_by):
    cursor.execute("""
        UPDATE roster_day_drafts
        SET is_submitted=1
        WHERE edited_by=%s AND is_submitted=0
    """, (edited_by,))


def apply_change(cursor, change):
    field = change["field_name"]

    query = f"""
        UPDATE roster_days
        SET {field}=%s
        WHERE roster_day_id=%s
    """
    cursor.execute(query, (change["new_value"], change["roster_day_id"]))


def auto_create_rosters(month_year=None, created_by=None):
    from datetime import datetime
    import calendar
    from config import get_db_connection

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    try:
        # ==========================
        # 1. Decide Month Format (APR2026)
        # ==========================
        if month_year:
            # input: "2026-04"
            year, month = map(int, month_year.split("-"))
        else:
            today = datetime.today()
            if today.month == 12:
                year = today.year + 1
                month = 1
            else:
                year = today.year
                month = today.month + 1

        month_name = datetime(year, month, 1).strftime("%b").upper()  # APR
        month_year_str = f"{month_name}{year}"  # APR2026

        # ==========================
        # 2. Get users (your table is tfs_user)
        # ==========================
        cursor.execute("SELECT user_id FROM tfs_user")
        users = cursor.fetchall()

        # ==========================
        # 3. Get holidays for month
        # ==========================
        cursor.execute("""
            SELECT holiday_date FROM holidays
            WHERE MONTH(holiday_date)=%s AND YEAR(holiday_date)=%s
        """, (month, year))
        holidays = {h["holiday_date"].strftime("%Y-%m-%d") for h in cursor.fetchall()}

        total_days = calendar.monthrange(year, month)[1]

        for user in users:

            # ==========================
            # 4. Skip if already exists
            # ==========================
            cursor.execute("""
                SELECT roster_id FROM rosters
                WHERE user_id=%s AND month_year=%s
            """, (user["user_id"], month_year_str))

            if cursor.fetchone():
                continue

            # ==========================
            # 5. Calculate counts
            # ==========================
            weekoff_days = 0
            holiday_days = 0
            working_days = 0

            for d in range(1, total_days + 1):
                date_obj = datetime(year, month, d)
                date_str = date_obj.strftime("%Y-%m-%d")

                if date_obj.weekday() in [5, 6]:  # Saturday or Sunday
                    weekoff_days += 1
                elif date_str in holidays:
                    holiday_days += 1
                else:
                    working_days += 1

            base_target = working_days * 9
            final_target = base_target
            # ==========================
            # 6. Create roster
            # ==========================
            cursor.execute("""
                INSERT INTO rosters
                (user_id, month_year, total_days, working_days, weekoff_days, holiday_days, base_target, final_target, created_by)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """, (
                user["user_id"],
                month_year_str,
                total_days,
                working_days,
                weekoff_days,
                holiday_days,
                base_target,
                final_target,
                created_by
            ))

            roster_id = cursor.lastrowid

            # ==========================
            # 7. Create roster_days
            # ==========================
            for d in range(1, total_days + 1):
                date_obj = datetime(year, month, d)
                date_str = date_obj.strftime("%Y-%m-%d")

                if date_obj.weekday() in [5, 6]:
                    day_type = "weekoff"
                elif date_str in holidays:
                    day_type = "holiday"
                else:
                    day_type = "working"

                cursor.execute("""
                    INSERT INTO roster_days
                    (roster_id, user_id, date, day_type)
                    VALUES (%s,%s,%s,%s)
                """, (
                    roster_id,
                    user["user_id"],
                    date_str,
                    day_type
                ))

        conn.commit()

        return {
            "status": 200,
            "message": f"Roster created for {month_year_str}"
        }

    except Exception as e:
        conn.rollback()
        return {
            "status": 500,
            "message": str(e)
        }

    finally:
        cursor.close()
        conn.close()