# routes/dashboard.py

from flask import Blueprint, request
from config import get_db_connection
from utils.response import api_response

dashboard_bp = Blueprint("dashboard", __name__, url_prefix="/dashboard")

# task_work_tracker.date_time is TEXT in your DB
# (works if stored like: "YYYY-MM-DD HH:MM:SS")
TRACKER_DT = "STR_TO_DATE(twt.date_time, '%Y-%m-%d %H:%i:%s')"


# -----------------------------
# Helpers
# -----------------------------
def get_user_role(cursor, user_id: int) -> str | None:
    cursor.execute("""
        SELECT r.role_name
        FROM tfs_user u
        JOIN user_role r ON r.role_id = u.role_id
        WHERE u.user_id=%s AND u.is_active=1 AND u.is_delete=1
    """, (user_id,))
    row = cursor.fetchone()
    if not row:
        return None
    return (row.get("role_name") or "").strip().lower()


def scope_for_logged_in_user(role: str, logged_in_user_id: int, params: list) -> str:
    """
    Visibility scope of LOGGED-IN user.
    Applied on alias `u` (tfs_user).
    """
    role = (role or "").lower()

    # Admin sees all (scope empty)
    if role in ["admin", "super admin"]:
        return ""

    # Agent sees only self
    if role == "agent":
        params.append(logged_in_user_id)
        return " AND u.user_id = %s"

    # QA sees users under them (qa_id stored as TEXT)
    if role == "qa":
        params.append(str(logged_in_user_id))
        return " AND u.qa_id = %s"

    # Manager sees users under them (project_manager_id stored as TEXT)
    if role == "manager":
        params.append(str(logged_in_user_id))
        return " AND u.project_manager_id = %s"

    # Assistant Manager: asst_manager_id is TEXT and can be JSON array / CSV / single
    # To avoid MariaDB JSON function issues, use a safe LIKE + FIND_IN_SET approach.
    if role == "assistant manager":
        params.append(str(logged_in_user_id))  # direct equality
        params.append(str(logged_in_user_id))  # find_in_set
        params.append(str(logged_in_user_id))
        return """
            AND (
                u.asst_manager_id = %s
                OR FIND_IN_SET(%s, u.asst_manager_id) > 0
                OR u.asst_manager_id LIKE CONCAT('%\"', %s, '\"%')
            )
        """

    # fallback: self only
    params.append(logged_in_user_id)
    return " AND u.user_id = %s"


def scope_for_subject_user(cursor, subject_user_id: int, params: list) -> str:
    """
    Subject scope (dashboard "for user_id" if passed, otherwise logged-in user).
    Expands depending on SUBJECT role:
      - manager => users under manager
      - assistant manager => users under that asst manager
      - qa => users under qa
      - agent/others => only that user
    Applied on alias `u`.
    """
    role = get_user_role(cursor, subject_user_id) or ""

    if role == "manager":
        params.append(str(subject_user_id))
        return " AND u.project_manager_id = %s"

    if role == "assistant manager":
        params.append(str(subject_user_id))
        params.append(str(subject_user_id))
        params.append(str(subject_user_id))
        return """
            AND (
                u.asst_manager_id = %s
                OR FIND_IN_SET(%s, u.asst_manager_id) > 0
                OR u.asst_manager_id LIKE CONCAT('%\"', %s, '\"%')
            )
        """

    if role == "qa":
        params.append(str(subject_user_id))
        return " AND u.qa_id = %s"

    # agent/others => only that user
    params.append(subject_user_id)
    return " AND u.user_id = %s"


def apply_tracker_filters(data: dict, where_sql: str, params: list) -> tuple[str, list]:
    """
    Apply filters on tracker alias `twt`.
    Supports:
      project_id, task_id, date (YYYY-MM-DD), date_from/date_to (YYYY-MM-DD HH:MM:SS)
    """
    if data.get("project_id"):
        where_sql += " AND twt.project_id = %s"
        params.append(data["project_id"])

    if data.get("task_id"):
        where_sql += " AND twt.task_id = %s"
        params.append(data["task_id"])

    # exact date (YYYY-MM-DD)
    if data.get("date"):
        where_sql += f" AND DATE({TRACKER_DT}) = %s"
        params.append(data["date"])

    if data.get("date_from"):
        where_sql += f" AND {TRACKER_DT} >= STR_TO_DATE(%s, '%Y-%m-%d %H:%i:%s')"
        params.append(data["date_from"])

    if data.get("date_to"):
        where_sql += f" AND {TRACKER_DT} <= STR_TO_DATE(%s, '%Y-%m-%d %H:%i:%s')"
        params.append(data["date_to"])

    return where_sql, params


# -----------------------------
# Dashboard Filter API
# -----------------------------
@dashboard_bp.route("/filter", methods=["POST"])
def dashboard_filter():
    data = request.get_json() or {}

    logged_in_user_id = data.get("logged_in_user_id")
    device_id = data.get("device_id")
    device_type = data.get("device_type")

    if not logged_in_user_id:
        return api_response(400, "logged_in_user_id is required")
    if not device_id:
        return api_response(400, "device_id is required")
    if not device_type:
        return api_response(400, "device_type is required")

    # If user_id is not passed, subject defaults to logged-in user
    subject_user_id = data.get("user_id") or logged_in_user_id

    # detect if any filter is present (including user_id)
    has_any_filter = any([
        data.get("user_id"),
        data.get("project_id"),
        data.get("task_id"),
        data.get("date"),
        data.get("date_from"),
        data.get("date_to"),
    ])

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    try:
        logged_role = get_user_role(cursor, logged_in_user_id)
        if not logged_role:
            return api_response(404, "Logged in user not found")

        is_admin = logged_role in ["admin", "super admin"]

        # ----------------------------------------------------------
        # CASE 1: Admin + No filters => return ALL info from masters
        # ----------------------------------------------------------
        if is_admin and not has_any_filter:
            cursor.execute("""
                SELECT
                    u.user_id,
                    u.user_name,
                    u.user_email,
                    u.user_number,
                    u.user_address,
                    u.user_tenure,
                    r.role_name AS role,
                    d.designation,
                    tm.team_name
                FROM tfs_user u
                LEFT JOIN user_role r ON r.role_id = u.role_id
                LEFT JOIN user_designation d ON d.designation_id = u.designation_id
                LEFT JOIN team tm ON tm.team_id = u.team_id
                WHERE u.is_active=1 AND u.is_delete=1
                ORDER BY u.user_id DESC
            """)
            users = cursor.fetchall()

            cursor.execute("""
                SELECT
                    project_id,
                    project_name,
                    project_code,
                    project_description,
                    project_manager_id,
                    asst_project_manager_id,
                    project_qa_id,
                    project_team_id
                FROM project
                WHERE is_active=1
                ORDER BY project_id DESC
            """)
            projects = cursor.fetchall()

            cursor.execute("""
                SELECT
                    task_id,
                    project_id,
                    task_team_id,
                    task_name,
                    task_description,
                    task_target
                FROM task
                WHERE is_active=1
                ORDER BY task_id DESC
            """)
            tasks = cursor.fetchall()

            # ---- Billable hours (project-wise) for ALL projects ----
            # If billable_hours is TEXT, use:
            # COALESCE(SUM(CAST(twt.billable_hours AS DECIMAL(10,2))), 0)
            cursor.execute("""
                SELECT
                    p.project_id,
                    COALESCE(SUM(twt.billable_hours), 0) AS total_billable_hours
                FROM task_work_tracker twt
                JOIN project p ON p.project_id = twt.project_id
                WHERE twt.is_active=1 AND p.is_active=1
                GROUP BY p.project_id
            """)
            project_billable_hours = cursor.fetchall()
            billable_map = {
                row["project_id"]: row["total_billable_hours"]
                for row in project_billable_hours
            }

            # Inject into projects list
            for p in projects:
                p["total_billable_hours"] = billable_map.get(p["project_id"], 0)

            return api_response(200, "Dashboard data fetched successfully", {
                "logged_in_role": logged_role,
                "filters_applied": {},
                "summary": {
                    "user_count": len(users),
                    "project_count": len(projects),
                    "task_count": len(tasks)
                },
                "users": users,
                "projects": projects,
                "tasks": tasks
            })

        # ----------------------------------------------------------
        # CASE 2: Tracker-driven (everything else)
        # ----------------------------------------------------------
        base_from = """
            FROM task_work_tracker twt
            JOIN tfs_user u ON u.user_id = twt.user_id
        """

        where_sql = """
            WHERE u.is_active=1 AND u.is_delete=1
              AND twt.is_active=1
        """

        params = []

        # 1) Logged-in visibility scope
        where_sql += scope_for_logged_in_user(logged_role, logged_in_user_id, params)

        # 2) Subject scope (dashboard for user_id OR logged-in user when user_id not passed)
        where_sql += scope_for_subject_user(cursor, subject_user_id, params)

        # 3) tracker filters (project/task/date)
        where_sql, params = apply_tracker_filters(data, where_sql, params)

        # --------------------
        # USERS (distinct in tracker scope)
        # --------------------
        users_query = f"""
            SELECT DISTINCT
                u.user_id,
                u.user_name,
                u.user_email,
                u.user_number,
                u.user_address,
                u.user_tenure,
                r.role_name AS role,
                d.designation,
                tm.team_name
            {base_from}
            LEFT JOIN user_role r ON r.role_id = u.role_id
            LEFT JOIN user_designation d ON d.designation_id = u.designation_id
            LEFT JOIN team tm ON tm.team_id = u.team_id
            {where_sql}
            ORDER BY u.user_id DESC
        """
        cursor.execute(users_query, tuple(params))
        users = cursor.fetchall()

        # --------------------
        # PROJECTS (distinct from tracker scope)
        # --------------------
        projects_query = f"""
            SELECT DISTINCT
                p.project_id,
                p.project_name,
                p.project_code,
                p.project_description,
                p.project_manager_id,
                p.asst_project_manager_id,
                p.project_qa_id,
                p.project_team_id
            FROM (
                SELECT DISTINCT twt.project_id
                {base_from}
                {where_sql}
            ) x
            JOIN project p ON p.project_id = x.project_id
            WHERE p.is_active=1
            ORDER BY p.project_id DESC
        """
        cursor.execute(projects_query, tuple(params))
        projects = cursor.fetchall()

        # --------------------
        # TASKS (distinct from tracker scope)
        # --------------------
        tasks_query = f"""
            SELECT DISTINCT
                tk.task_id,
                tk.project_id,
                tk.task_team_id,
                tk.task_name,
                tk.task_description,
                tk.task_target
            FROM (
                SELECT DISTINCT twt.task_id
                {base_from}
                {where_sql}
            ) x
            JOIN task tk ON tk.task_id = x.task_id
            WHERE tk.is_active=1
            ORDER BY tk.task_id DESC
        """
        cursor.execute(tasks_query, tuple(params))
        tasks = cursor.fetchall()

        # --------------------
        # BILLABLE HOURS (project-wise) within SAME scope/filters
        # --------------------
        # If billable_hours is TEXT, use:
        # COALESCE(SUM(CAST(twt.billable_hours AS DECIMAL(10,2))), 0)
        billable_query = f"""
            SELECT
                p.project_id,
                COALESCE(SUM(twt.billable_hours), 0) AS total_billable_hours
            {base_from}
            JOIN project p ON p.project_id = twt.project_id
            {where_sql}
              AND p.is_active=1
            GROUP BY p.project_id
        """
        cursor.execute(billable_query, tuple(params))
        project_billable_hours = cursor.fetchall()

        billable_map = {
            row["project_id"]: row["total_billable_hours"]
            for row in project_billable_hours
        }

        # Inject into projects list
        for p in projects:
            p["total_billable_hours"] = billable_map.get(p["project_id"], 0)

        # --------------------
        # SUMMARY
        # --------------------
        summary_query = f"""
            SELECT
                COUNT(DISTINCT u.user_id) AS user_count,
                COUNT(DISTINCT twt.project_id) AS project_count,
                COUNT(DISTINCT twt.task_id) AS task_count,
                COUNT(*) AS tracker_rows,
                COALESCE(SUM(twt.production), 0) AS total_production
            {base_from}
            {where_sql}
        """
        cursor.execute(summary_query, tuple(params))
        summary = cursor.fetchone() or {}

        return api_response(200, "Dashboard data fetched successfully", {
            "logged_in_role": logged_role,
            "filters_applied": {
                "user_id": data.get("user_id"),
                "project_id": data.get("project_id"),
                "task_id": data.get("task_id"),
                "date": data.get("date"),
                "date_from": data.get("date_from"),
                "date_to": data.get("date_to"),
            },
            "summary": summary,
            "users": users,
            "projects": projects,
            "tasks": tasks
        })

    except Exception as e:
        return api_response(500, f"Dashboard filter failed: {str(e)}")

    finally:
        cursor.close()
        conn.close()
