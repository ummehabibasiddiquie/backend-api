# routes/user_monthly_tracker.py

from flask import Blueprint, request
from config import get_db_connection
from utils.response import api_response
from utils.roster_helpers import (
    can_manage_roster_employees,
    sync_tracker_extra_hours_to_roster,
    sync_tracker_monthly_target_to_roster,
)
from utils.qc_auto_score import AUTO_QC_DAYS_WITHOUT_EXISTING_SCORE_SQL
from utils.time_ist import now_str
from datetime import datetime, timedelta

user_monthly_tracker_bp = Blueprint("user_monthly_tracker", __name__)


def _parse_extra_assigned_hours(raw) -> tuple[float | None, str | None]:
    """Extra hours may be negative (reduces monthly goal). None means not provided."""
    if raw in (None, ""):
        return None, None
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        return None, "extra_assigned_hours must be a number"
    return value, None

# task_work_tracker.date_time is TEXT like "YYYY-MM-DD HH:MM:SS"
TRACKER_DT = "CAST(twt.date_time AS DATETIME)"
TRACKER_YEAR_MONTH = f"(YEAR({TRACKER_DT})*100 + MONTH({TRACKER_DT}))"


def month_year_to_yyyymm_sql(month_year_col: str) -> str:
    return f"""
    CAST(
        CONCAT(
            RIGHT({month_year_col},4),
            LPAD(
                CASE LEFT(UPPER({month_year_col}),3)
                    WHEN 'JAN' THEN 1
                    WHEN 'FEB' THEN 2
                    WHEN 'MAR' THEN 3
                    WHEN 'APR' THEN 4
                    WHEN 'MAY' THEN 5
                    WHEN 'JUN' THEN 6
                    WHEN 'JUL' THEN 7
                    WHEN 'AUG' THEN 8
                    WHEN 'SEP' THEN 9
                    WHEN 'OCT' THEN 10
                    WHEN 'NOV' THEN 11
                    WHEN 'DEC' THEN 12
                END,
                2,
                '0'
            )
        ) AS UNSIGNED
    )
    """


# ---------------------------
# Single helper (role_name + agent_role_id)
# ---------------------------
def get_role_context(cursor, user_id: int) -> dict:
    """
    Returns:
      {
        "user_role_id": int|None,
        "user_role_name": str,
        "agent_role_id": int|None
      }
    """
    cursor.execute(
        """
        SELECT
            u.role_id AS user_role_id,
            r.role_name AS user_role_name,
            (
                SELECT ur2.role_id
                FROM user_role ur2
                WHERE LOWER(TRIM(ur2.role_name)) = 'agent'
                LIMIT 1
            ) AS agent_role_id
        FROM tfs_user u
        JOIN user_role r ON r.role_id = u.role_id
        WHERE u.user_id=%s AND u.is_active=1 AND u.is_delete=1
        """,
        (int(user_id),),
    )
    row = cursor.fetchone() or {}
    return {
        "user_role_id": row.get("user_role_id"),
        "user_role_name": (row.get("user_role_name") or "").strip().lower(),
        "agent_role_id": row.get("agent_role_id"),
    }


# ---------------------------
# ADD
# ---------------------------
# ---------------------------
# ADD (Single OR Bulk)
# ---------------------------
@user_monthly_tracker_bp.route("/add", methods=["POST"])
def add_user_monthly_target():
    """
    Accepts either:
      - Single object:
          {"user_id": 1, "month_year": "JAN2026", "monthly_target": "160", "working_days": "22", "extra_assigned_hours": 0}
      - Array of objects:
          [{"user_id": 1, ...}, {"user_id": 2, ...}]
    """
    raw = request.get_json(silent=True)

    # Normalize to list
    if isinstance(raw, list):
        records = raw
    elif isinstance(raw, dict):
        records = [raw]
    else:
        return api_response(400, "Invalid JSON payload")

    if not records:
        return api_response(400, "No records provided")

    required_fields = ["user_id", "month_year", "monthly_target", "working_days"]

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    try:
        inserted_ids = []
        skipped = []

        for idx, data in enumerate(records):
            # ---- required validation per record
            missing = None
            for f in required_fields:
                if data.get(f) in [None, ""]:
                    missing = f
                    break
            if missing:
                skipped.append({"index": idx + 1, "reason": f"{missing} is required"})
                continue

            user_id = int(data["user_id"])
            month_year = str(data["month_year"]).strip()  # MONYYYY like JAN2026
            monthly_target = str(data["monthly_target"]).strip()  # TEXT in DB
            extra_assigned_hours, extra_err = _parse_extra_assigned_hours(
                data.get("extra_assigned_hours")
            )
            if extra_err:
                skipped.append({"index": idx + 1, "user_id": user_id, "reason": extra_err})
                continue
            extra_assigned_hours = extra_assigned_hours if extra_assigned_hours is not None else 0.0
            working_days = str(data["working_days"]).strip()  # TEXT in DB
            created_date = str(data.get("created_date") or now_str())

            # ---- Validate user exists
            cursor.execute(
                """
                SELECT user_id
                FROM tfs_user
                WHERE user_id=%s AND is_delete=1
                """,
                (user_id,),
            )
            if not cursor.fetchone():
                skipped.append(
                    {"index": idx + 1, "user_id": user_id, "reason": "User not found or inactive"}
                )
                continue

            # ---- Prevent duplicate active (user + month)
            cursor.execute(
                """
                SELECT user_monthly_tracker_id
                FROM user_monthly_tracker
                WHERE user_id=%s AND month_year=%s 
                """,
                (user_id, month_year),
            )
            if cursor.fetchone():
                skipped.append(
                    {
                        "index": idx + 1,
                        "user_id": user_id,
                        "month_year": month_year,
                        "reason": "Monthly target already exists for this user and month",
                    }
                )
                continue

            cursor.execute(
                """
                INSERT INTO user_monthly_tracker
                    (user_id, month_year, monthly_target, extra_assigned_hours, working_days, is_active, created_date)
                VALUES (%s, %s, %s, %s, %s, 1, %s)
                """,
                (
                    user_id,
                    month_year,
                    monthly_target,
                    extra_assigned_hours,
                    working_days,
                    created_date,
                ),
            )
            inserted_ids.append(cursor.lastrowid)
            sync_tracker_extra_hours_to_roster(
                cursor, user_id, month_year, extra_assigned_hours
            )

        conn.commit()

        # If nothing inserted but there are skipped => conflict-ish
        if not inserted_ids and skipped:
            return api_response(409, "No records inserted", {"skipped": skipped})

        return api_response(
            201,
            f"{len(inserted_ids)} record(s) added successfully",
            {
                "inserted_count": len(inserted_ids),
                "user_monthly_tracker_ids": inserted_ids,
                "skipped_count": len(skipped),
                "skipped": skipped if skipped else None,
            },
        )

    except Exception as e:
        conn.rollback()
        return api_response(500, f"Add failed: {str(e)}")
    finally:
        cursor.close()
        conn.close()


# ---------------------------
# UPDATE
# ---------------------------
@user_monthly_tracker_bp.route("/update", methods=["POST"])
def update_user_monthly_target():
    data = request.get_json(silent=True) or {}

    if not data.get("user_monthly_tracker_id"):
        return api_response(400, "user_monthly_tracker_id is required")

    umt_id = int(data["user_monthly_tracker_id"])
    logged_in_user_id = data.get("logged_in_user_id")

    updates = []
    params = []

    if "user_id" in data and data["user_id"] not in [None, ""]:
        updates.append("user_id=%s")
        params.append(int(data["user_id"]))

    if "month_year" in data and data["month_year"] not in [None, ""]:
        updates.append("month_year=%s")
        params.append(str(data["month_year"]).strip())  # keep as-is (MONYYYY)

    parsed_monthly_target = None
    if "monthly_target" in data and data["monthly_target"] not in [None, ""]:
        # Monthly target is set by roster generate and must not be edited afterwards.
        # Reduce/increase the goal with extra_assigned_hours (negative allowed).
        parsed_monthly_target = None

    parsed_extra = None
    if "extra_assigned_hours" in data and data["extra_assigned_hours"] not in [None, ""]:
        parsed_extra, extra_err = _parse_extra_assigned_hours(data.get("extra_assigned_hours"))
        if extra_err:
            return api_response(400, extra_err)
        updates.append("extra_assigned_hours=%s")
        params.append(parsed_extra)

    if not updates:
        return api_response(400, "Nothing to update")

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    try:
        # Current row
        cursor.execute(
            """
            SELECT user_id, month_year, monthly_target
            FROM user_monthly_tracker
            WHERE user_monthly_tracker_id=%s
            """,
            (umt_id,),
        )
        current = cursor.fetchone()
        if not current:
            return api_response(404, "Active record not found")

        if parsed_extra is not None:
            try:
                current_target = float(current.get("monthly_target") or 0)
            except (TypeError, ValueError):
                current_target = 0.0
            if current_target + parsed_extra < 0:
                return api_response(
                    400,
                    "Extra assigned hours cannot reduce the monthly goal below 0",
                )

        if logged_in_user_id not in [None, ""]:
            ctx = get_role_context(cursor, int(logged_in_user_id))
            if not can_manage_roster_employees(ctx.get("user_role_name") or ""):
                return api_response(
                    403,
                    "Only Assistant Manager, Project Manager, Admin, or Super Admin can update monthly target",
                )

        # Validate user if updating it
        if "user_id" in data and data["user_id"] not in [None, ""]:
            new_user_id = int(data["user_id"])
            cursor.execute(
                """
                SELECT user_id
                FROM tfs_user
                WHERE user_id=%s AND is_delete=1
                """,
                (new_user_id,),
            )
            if not cursor.fetchone():
                return api_response(404, "User not found or inactive")

        # Prevent duplicate active (final user_id + final month_year)
        if (
            ("user_id" in data and data["user_id"] not in [None, ""])
            or ("month_year" in data and data["month_year"] not in [None, ""])
        ):
            final_user_id = (
                int(data["user_id"])
                if ("user_id" in data and data["user_id"] not in [None, ""])
                else int(current["user_id"])
            )
            final_month_year = (
                str(data["month_year"]).strip()
                if ("month_year" in data and data["month_year"] not in [None, ""])
                else str(current["month_year"])
            )

            cursor.execute(
                """
                SELECT user_monthly_tracker_id
                FROM user_monthly_tracker
                WHERE user_id=%s AND month_year=%s 
                  AND user_monthly_tracker_id<>%s
                """,
                (final_user_id, final_month_year, umt_id),
            )
            if cursor.fetchone():
                return api_response(409, "Monthly target already exists for this user and month")

        params.append(umt_id)
        query = f"""
            UPDATE user_monthly_tracker
            SET {', '.join(updates)}
            WHERE user_monthly_tracker_id=%s
        """
        cursor.execute(query, tuple(params))

        final_user_id = int(
            data["user_id"] if data.get("user_id") not in [None, ""] else current["user_id"]
        )
        final_month_year = str(
            data["month_year"] if data.get("month_year") not in [None, ""] else current["month_year"]
        ).strip()

        if parsed_extra is not None:
            sync_tracker_extra_hours_to_roster(
                cursor,
                final_user_id,
                final_month_year,
                parsed_extra,
            )

        if parsed_monthly_target is not None:
            sync_tracker_monthly_target_to_roster(
                cursor,
                final_user_id,
                final_month_year,
                parsed_monthly_target,
            )

        conn.commit()

        return api_response(200, "User monthly target updated successfully")

    except Exception as e:
        conn.rollback()
        return api_response(500, f"Update failed: {str(e)}")
    finally:
        cursor.close()
        conn.close()


# ---------------------------
# DELETE (SOFT)
# ---------------------------
@user_monthly_tracker_bp.route("/delete", methods=["POST"])
def delete_user_monthly_target():
    data = request.get_json(silent=True) or {}

    if not data.get("user_monthly_tracker_id"):
        return api_response(400, "user_monthly_tracker_id is required")

    umt_id = int(data["user_monthly_tracker_id"])

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    try:
        cursor.execute(
            """
            delete from user_monthly_tracker
            WHERE user_monthly_tracker_id=%s AND is_active=1
            """,
            (umt_id,),
        )
        conn.commit()

        if cursor.rowcount == 0:
            return api_response(404, "Active record not found")

        return api_response(200, "User monthly target deleted successfully")

    except Exception as e:
        conn.rollback()
        return api_response(500, f"Delete failed: {str(e)}")
    finally:
        cursor.close()
        conn.close()


# ---------------------------
# LIST
# Changes:
# - month_year optional: if missing -> default current month (MONYYYY) so pending_days works
# - only agent rows (managers/qa won't appear as rows)
# - monthly_total_target = monthly_target + extra_assigned_hours
# - pending_days = working_days(from UMT) - distinct worked days till today (month-wise)
# - do NOT return working_days or working_days_till_today separately
# - Show users based on monthly targets availability for the month
# - When month_year provided: only show users who have monthly targets for that month
# - Deactivated users still appear if they have monthly targets for the selected month
# - user_monthly_tracker is INNER JOIN (required) - only users with monthly targets appear
# ---------------------------
@user_monthly_tracker_bp.route("/list", methods=["POST"])
def list_user_monthly_targets():
    data = request.get_json(silent=True) or {}

    logged_in_user_id = data.get("logged_in_user_id")
    month_year = (data.get("month_year") or "").strip()  # OPTIONAL (MonYYYY)
    filter_user_id = data.get("user_id")  # OPTIONAL
    filter_team_id = data.get("team_id")  # OPTIONAL

    if not logged_in_user_id:
        return api_response(400, "logged_in_user_id is required", None)

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    try:
        ctx = get_role_context(cursor, int(logged_in_user_id))
        my_role_name = (ctx.get("user_role_name") or "").lower()
        agent_role_id = ctx.get("agent_role_id")

        if not agent_role_id:
            return api_response(500, "Agent role not found in user_role table", None)
        
        if month_year:
            dt = datetime.strptime(month_year, "%b%Y")  # Mar2026
            month_start = dt.replace(day=1)
        else:
            now = datetime.now()
            month_start = now.replace(day=1)

        month_end = (month_start.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(seconds=1)

        month_start_str = month_start.strftime("%Y-%m-%d %H:%M:%S")
        month_end_str = month_end.strftime("%Y-%m-%d %H:%M:%S")

        # ---------------- Base WHERE: only agent rows ----------------
        # Remove is_active check to show deactivated users if they have tracker data
        user_where = """
            WHERE u.is_delete=1
            AND u.role_id=%s
        """

        user_params = [agent_role_id]

        if filter_user_id:
            user_where += " AND u.user_id=%s"
            user_params.append(int(filter_user_id))

        if filter_team_id:
            user_where += " AND u.team_id=%s"
            user_params.append(int(filter_team_id))

        if my_role_name in ("admin", "super admin", "project manager"):
            pass
        elif my_role_name == "agent":
            user_where += " AND u.user_id=%s"
            user_params.append(int(logged_in_user_id))
        else:
            mid = str(logged_in_user_id)
            user_where += """
                AND (
                    JSON_CONTAINS(u.project_manager_id, %s)
                    OR JSON_CONTAINS(u.asst_manager_id, %s)
                    OR JSON_CONTAINS(u.qa_id, %s)
                )
            """

            user_params.extend([str(mid), str(mid), str(mid)])

        # ---------------- Joins: month_year optional ----------------
        # temp_qc.date is TEXT 'YYYY-MM-DD'
        if month_year:
            # Use INNER JOIN for user_monthly_tracker to show only users with monthly targets for this month
            umt_join = """
                INNER JOIN user_monthly_tracker umt
                ON umt.user_id = u.user_id
                AND umt.is_active=1
                AND umt.month_year=%s
            """

            twt_join = f"""
                LEFT JOIN task_work_tracker twt
                ON twt.user_id = u.user_id
                AND twt.is_active=1
                AND {TRACKER_YEAR_MONTH}=%s
            """

            qc_join = f"""
                LEFT JOIN (
                    SELECT
                        x.user_id,
                        ROUND(AVG(x.daily_qc_avg),2) AS avg_qc_score,
                        COUNT(*) AS qc_days_count
                    FROM
                    (
                        -- OLD SYSTEM (temp_qc already has one record/day)
                        SELECT
                            tq.user_id,
                            tq.date AS qc_date,
                            tq.qc_score AS daily_qc_avg
                        FROM temp_qc tq
                        WHERE tq.qc_score IS NOT NULL

                        UNION ALL

                        -- NEW SYSTEM (many files/day)
                        SELECT
                            qr.agent_id AS user_id,
                            DATE(qr.date_of_file_submission) AS qc_date,
                            ROUND(AVG(qr.qc_score),2) AS daily_qc_avg
                        FROM qc_records qr
                        WHERE qr.qc_score IS NOT NULL
                        GROUP BY
                            qr.agent_id,
                            DATE(qr.date_of_file_submission)

                        UNION ALL

                        {AUTO_QC_DAYS_WITHOUT_EXISTING_SCORE_SQL}

                    ) x

                    WHERE DATE_FORMAT(
                            STR_TO_DATE(x.qc_date,'%Y-%m-%d'),
                            '%Y%m'
                        )
                        = %s

                    GROUP BY x.user_id

                ) qc
                ON qc.user_id=u.user_id
            """
        else:
            umt_join = """
                LEFT JOIN user_monthly_tracker umt
                ON umt.user_id=u.user_id
                AND umt.is_active=1
            """

            twt_join = """
                LEFT JOIN task_work_tracker twt
                ON twt.user_id=u.user_id
                AND twt.is_active=1
            """

            qc_join = f"""
                LEFT JOIN (
                    SELECT
                        x.user_id,
                        ROUND(AVG(x.daily_qc_avg),2) AS avg_qc_score,
                        COUNT(*) AS qc_days_count
                    FROM
                    (
                        -- OLD QC system
                        SELECT
                            tq.user_id,
                            tq.date AS qc_date,
                            tq.qc_score AS daily_qc_avg
                        FROM temp_qc tq
                        WHERE tq.qc_score IS NOT NULL

                        UNION ALL

                        -- NEW QC system
                        SELECT
                            qr.agent_id AS user_id,
                            DATE(qr.date_of_file_submission) AS qc_date,
                            ROUND(AVG(qr.qc_score),2) AS daily_qc_avg
                        FROM qc_records qr
                        WHERE qr.qc_score IS NOT NULL
                        GROUP BY
                            qr.agent_id,
                            DATE(qr.date_of_file_submission)

                        UNION ALL

                        {AUTO_QC_DAYS_WITHOUT_EXISTING_SCORE_SQL}

                    ) x
                    GROUP BY x.user_id
                ) qc
                ON qc.user_id=u.user_id
            """

        # ---------------- Main query ----------------
        # Use COALESCE for month_year only when month_year is provided
        month_year_coalesce = f"COALESCE(umt.month_year, '{month_year}')" if month_year else "umt.month_year"
        
        query = f"""
            SELECT
                u.user_id,
                u.user_name,
                t.team_name,
                umt.user_monthly_tracker_id,
                {month_year_coalesce} AS month_year,
                umt.working_days,
                COALESCE(CAST(umt.monthly_target AS DECIMAL(10,2)), 0) AS monthly_target,
                COALESCE(umt.extra_assigned_hours, 0) AS extra_assigned_hours,
                (
                    COALESCE(CAST(umt.monthly_target AS DECIMAL(10,2)), 0)
                    + COALESCE(umt.extra_assigned_hours, 0)
                ) AS monthly_total_target,

                COALESCE(SUM(COALESCE(twt.production, 0) / NULLIF(twt.tenure_target, 0)), 0) AS total_billable_hours,
                COALESCE(SUM(twt.production), 0) AS total_production,
                COUNT(twt.tracker_id) AS tracker_rows,

                -- QC monthly avg and qc-days count
                qc.avg_qc_score AS avg_qc_score,
                COALESCE(qc.qc_days_count, 0) AS qc_days_count,

                (
                    COALESCE(CAST(umt.monthly_target AS DECIMAL(10,2)), 0)
                    + COALESCE(umt.extra_assigned_hours, 0)
                ) - COALESCE(SUM(COALESCE(twt.production, 0) / NULLIF(twt.tenure_target, 0)), 0) AS pending_target
            FROM tfs_user u
            LEFT JOIN team t ON u.team_id = t.team_id
            {umt_join}
            {twt_join}
            {qc_join}
            {user_where}
            GROUP BY
                u.user_id,
                u.user_name,
                t.team_name,
                umt.user_monthly_tracker_id,
                umt.month_year,
                umt.working_days,
                umt.monthly_target,
                umt.extra_assigned_hours,
                qc.avg_qc_score,
                qc.qc_days_count
            ORDER BY
              CASE WHEN t.team_name IS NULL OR TRIM(t.team_name) = '' THEN 1 ELSE 0 END,
              t.team_name ASC,
              CASE
                WHEN LOWER(TRIM(u.user_name)) = LOWER(TRIM(IFNULL(t.team_name, ''))) THEN 0
                ELSE 1
              END,
              u.user_name ASC
        """

        # Params order:
        # if month_year: umt_join(%s), twt_join(%s), qc_join(%s), then user_where params
        if month_year:

            month_dt = datetime.strptime(month_year, "%b%Y")
            yyyymm = month_dt.strftime("%Y%m")

            final_params = [
                month_year,  # umt
                yyyymm,      # twt
                yyyymm       # qc
            ]
        else:
            final_params=[]

        final_params.extend(user_params)
        print(f"DEBUG: Final params: {final_params}")

        cursor.execute(query, tuple(final_params))
        rows = cursor.fetchall()

        return api_response(200, "User monthly targets fetched successfully", rows)

    except Exception as e:
        return api_response(500, f"List failed: {str(e)}", None)

    finally:
        cursor.close()
        conn.close()