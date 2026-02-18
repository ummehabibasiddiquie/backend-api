# routes/user_monthly_tracker.py

from flask import Blueprint, request
from config import get_db_connection
from utils.response import api_response
from datetime import datetime

user_monthly_tracker_bp = Blueprint("user_monthly_tracker", __name__)

# task_work_tracker.date_time is TEXT like "YYYY-MM-DD HH:MM:SS"
TRACKER_DT = "CAST(twt.date_time AS DATETIME)"
TRACKER_YEAR_MONTH = f"(YEAR({TRACKER_DT})*100 + MONTH({TRACKER_DT}))"


def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def month_year_to_yyyymm_sql(month_year_col: str) -> str:
    """
    Your DB stores month_year like 'JAN2026', 'DEC2025' (MONYYYY).
    Convert MONYYYY -> integer YYYYMM inside SQL.
    """
    return f"""
    CAST(
      DATE_FORMAT(
        STR_TO_DATE(CONCAT('01-', {month_year_col}), '%d-%b%Y'),
        '%Y%m'
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
@user_monthly_tracker_bp.route("/add", methods=["POST"])
def add_user_monthly_target():
    data = request.get_json(silent=True) or {}

    if not data.get("user_id"):
        return api_response(400, "user_id is required")
    if not data.get("month_year"):
        return api_response(400, "month_year is required (MONYYYY e.g. JAN2026)")
    if data.get("monthly_target") in [None, ""]:
        return api_response(400, "monthly_target is required")
    if data.get("working_days") in [None, ""]:
        return api_response(400, "working_days is required")

    user_id = int(data["user_id"])
    month_year = str(data["month_year"]).strip()  # keep as-is (MONYYYY)
    monthly_target = str(data["monthly_target"]).strip()  # TEXT in DB
    extra_assigned_hours = int(data.get("extra_assigned_hours") or 0)
    working_days = str(data["working_days"]).strip()  # TEXT in DB
    created_date = str(data.get("created_date") or now_str())

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    try:
        # Validate user exists
        cursor.execute(
            """
            SELECT user_id
            FROM tfs_user
            WHERE user_id=%s AND is_active=1 AND is_delete=1
            """,
            (user_id,),
        )
        if not cursor.fetchone():
            return api_response(404, "User not found or inactive")

        # Prevent duplicate active (user + month)
        cursor.execute(
            """
            SELECT user_monthly_tracker_id
            FROM user_monthly_tracker
            WHERE user_id=%s AND month_year=%s AND is_active=1
            """,
            (user_id, month_year),
        )
        if cursor.fetchone():
            return api_response(409, "Monthly target already exists for this user and month")

        cursor.execute(
            """
            INSERT INTO user_monthly_tracker
                (user_id, month_year, monthly_target, extra_assigned_hours, working_days, is_active, created_date)
            VALUES (%s, %s, %s, %s, %s, 1, %s)
            """,
            (user_id, month_year, monthly_target, extra_assigned_hours, working_days, created_date),
        )
        conn.commit()

        return api_response(
            201,
            "User monthly target added successfully",
            {"user_monthly_tracker_id": cursor.lastrowid},
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

    updates = []
    params = []

    if "user_id" in data and data["user_id"] not in [None, ""]:
        updates.append("user_id=%s")
        params.append(int(data["user_id"]))

    if "month_year" in data and data["month_year"] not in [None, ""]:
        updates.append("month_year=%s")
        params.append(str(data["month_year"]).strip())  # keep as-is (MONYYYY)

    if "monthly_target" in data and data["monthly_target"] not in [None, ""]:
        updates.append("monthly_target=%s")
        params.append(str(data["monthly_target"]).strip())

    if "extra_assigned_hours" in data and data["extra_assigned_hours"] not in [None, ""]:
        updates.append("extra_assigned_hours=%s")
        params.append(int(data["extra_assigned_hours"]))

    if "working_days" in data and data["working_days"] not in [None, ""]:
        updates.append("working_days=%s")
        params.append(str(data["working_days"]).strip())

    if not updates:
        return api_response(400, "Nothing to update")

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    try:
        # Current row
        cursor.execute(
            """
            SELECT user_id, month_year
            FROM user_monthly_tracker
            WHERE user_monthly_tracker_id=%s AND is_active=1
            """,
            (umt_id,),
        )
        current = cursor.fetchone()
        if not current:
            return api_response(404, "Active record not found")

        # Validate user if updating it
        if "user_id" in data and data["user_id"] not in [None, ""]:
            new_user_id = int(data["user_id"])
            cursor.execute(
                """
                SELECT user_id
                FROM tfs_user
                WHERE user_id=%s AND is_active=1
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
                WHERE user_id=%s AND month_year=%s AND is_active=1
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
            UPDATE user_monthly_tracker
            SET is_active=0
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

        # ---------------- Base WHERE: only agent rows ----------------
        user_where = """
            WHERE u.is_active=1
              AND u.is_delete=1
              AND u.role_id=%s
        """
        user_params = [agent_role_id]

        if filter_user_id:
            user_where += " AND u.user_id=%s"
            user_params.append(int(filter_user_id))

        if filter_team_id:
            user_where += " AND u.team_id=%s"
            user_params.append(int(filter_team_id))

        if my_role_name in ("admin", "super admin"):
            pass
        elif my_role_name == "agent":
            user_where += " AND u.user_id=%s"
            user_params.append(int(logged_in_user_id))
        else:
            mid = str(logged_in_user_id)
            user_where += """
                AND (
                    u.project_manager_id = %s
                    OR u.asst_manager_id = %s
                    OR u.qa_id = %s
                    OR FIND_IN_SET(%s, REPLACE(u.project_manager_id, ' ', '')) > 0
                    OR FIND_IN_SET(%s, REPLACE(u.asst_manager_id, ' ', '')) > 0
                    OR FIND_IN_SET(%s, REPLACE(u.qa_id, ' ', '')) > 0
                )
            """
            user_params.extend([mid, mid, mid, mid, mid, mid])

        # ---------------- Joins: month_year optional ----------------
        # temp_qc.date is TEXT 'YYYY-MM-DD'
        QC_YEAR_MONTH = "DATE_FORMAT(STR_TO_DATE(tq.date, '%Y-%m-%d'), '%Y%m')"

        if month_year:
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
                 AND {TRACKER_YEAR_MONTH} = {month_year_to_yyyymm_sql('%s')}
            """
            # ✅ avg_qc_score = SUM(qc_score) / COUNT(days having qc_score)
            qc_join = f"""
                LEFT JOIN (
                    SELECT
                        tq.user_id,
                        ROUND(SUM(tq.qc_score) / NULLIF(COUNT(DISTINCT tq.date), 0), 2) AS avg_qc_score,
                        COUNT(DISTINCT tq.date) AS qc_days_count
                    FROM temp_qc tq
                    WHERE tq.qc_score IS NOT NULL
                      AND {QC_YEAR_MONTH} = {month_year_to_yyyymm_sql('%s')}
                    GROUP BY tq.user_id
                ) qc ON qc.user_id = u.user_id
            """
        else:
            umt_join = """
                LEFT JOIN user_monthly_tracker umt
                  ON umt.user_id = u.user_id
                 AND umt.is_active=1
            """
            twt_join = """
                LEFT JOIN task_work_tracker twt
                  ON twt.user_id = u.user_id
                 AND twt.is_active=1
            """
            qc_join = """
                LEFT JOIN (
                    SELECT
                        tq.user_id,
                        ROUND(SUM(tq.qc_score) / NULLIF(COUNT(DISTINCT tq.date), 0), 2) AS avg_qc_score,
                        COUNT(DISTINCT tq.date) AS qc_days_count
                    FROM temp_qc tq
                    WHERE tq.qc_score IS NOT NULL
                    GROUP BY tq.user_id
                ) qc ON qc.user_id = u.user_id
            """

        # ---------------- Main query ----------------
        query = f"""
            SELECT
                u.user_id,
                u.user_name,
                t.team_name,
                umt.user_monthly_tracker_id,
                umt.month_year,
                COALESCE(CAST(umt.monthly_target AS DECIMAL(10,2)), 0) AS monthly_target,
                COALESCE(umt.extra_assigned_hours, 0) AS extra_assigned_hours,
                (
                    COALESCE(CAST(umt.monthly_target AS DECIMAL(10,2)), 0)
                    + COALESCE(umt.extra_assigned_hours, 0)
                ) AS monthly_total_target,

                COALESCE(SUM(twt.billable_hours), 0) AS total_billable_hours,
                COALESCE(SUM(twt.production), 0) AS total_production,
                COUNT(twt.tracker_id) AS tracker_rows,

                -- ✅ QC monthly avg and qc-days count
                qc.avg_qc_score AS avg_qc_score,
                COALESCE(qc.qc_days_count, 0) AS qc_days_count,

                GREATEST(
                    (
                        COALESCE(CAST(umt.monthly_target AS DECIMAL(10,2)), 0)
                        + COALESCE(umt.extra_assigned_hours, 0)
                    ) - COALESCE(SUM(twt.billable_hours), 0),
                    0
                ) AS pending_target
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
                monthly_target,
                extra_assigned_hours,
                qc.avg_qc_score,
                qc.qc_days_count
            ORDER BY u.user_name ASC
        """

        # Params order:
        # if month_year: umt_join(%s), twt_join(%s), qc_join(%s), then user_where params
        if month_year:
            final_params = [month_year, month_year, month_year]
        else:
            final_params = []
        final_params.extend(user_params)

        cursor.execute(query, tuple(final_params))
        rows = cursor.fetchall()
        return api_response(200, "User monthly targets fetched successfully", rows)

    except Exception as e:
        return api_response(500, f"List failed: {str(e)}", None)

    finally:
        cursor.close()
        conn.close()