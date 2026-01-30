# routes/dashboard.py

from flask import Blueprint, request
from config import get_db_connection, UPLOAD_FOLDER, UPLOAD_SUBDIRS, BASE_UPLOAD_URL
from utils.response import api_response

dashboard_bp = Blueprint("dashboard", __name__, url_prefix="/dashboard")

TRACKER_DT = "CAST(twt.date_time AS DATETIME)"


# -----------------------------
# Helpers
# -----------------------------
def get_user_role(cursor, user_id: int) -> str | None:
    cursor.execute(
        """
        SELECT r.role_name
        FROM tfs_user u
        JOIN user_role r ON r.role_id = u.role_id
        WHERE u.user_id=%s AND u.is_active=1 AND u.is_delete=1
        """,
        (user_id,),
    )
    row = cursor.fetchone()
    if not row:
        return None
    return (row.get("role_name") or "").strip().lower()


def multi_id_match_sql(col: str) -> str:
    cleaned = f"REPLACE(REPLACE(REPLACE(REPLACE({col}, '[', ''), ']', ''), CHAR(34), ''), ' ', '')"
    return f"({col} = %s OR FIND_IN_SET(%s, {cleaned}) > 0)"


def build_in_clause_int(ids: list[int], params: list) -> str:
    if not ids:
        return "IN (NULL)"
    placeholders = ",".join(["%s"] * len(ids))
    params.extend(ids)
    return f"IN ({placeholders})"


def apply_tracker_filters(data: dict, where_sql: str, params: list) -> tuple[str, list]:
    if data.get("user_id"):
        where_sql += " AND twt.user_id = %s"
        params.append(int(data["user_id"]))

    if data.get("project_id"):
        where_sql += " AND twt.project_id = %s"
        params.append(data["project_id"])

    if data.get("task_id"):
        where_sql += " AND twt.task_id = %s"
        params.append(data["task_id"])

    if data.get("date"):
        where_sql += f" AND DATE({TRACKER_DT}) = %s"
        params.append(data["date"])

    if data.get("date_from"):
        date_from = data["date_from"]
        if len(date_from) == 10:
            date_from += " 00:00:00"
        where_sql += f" AND {TRACKER_DT} >= %s"
        params.append(date_from)

    if data.get("date_to"):
        date_to = data["date_to"]
        if len(date_to) == 10:
            date_to += " 23:59:59"
        where_sql += f" AND {TRACKER_DT} <= %s"
        params.append(date_to)

    return where_sql, params


# -----------------------------
# USER → TRACKER SCOPING (IMPORTANT PART)
# -----------------------------
def detect_existing_column(cursor, table: str, candidates: list[str]) -> str | None:
    cursor.execute(
        """
        SELECT COLUMN_NAME
        FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_SCHEMA = DATABASE()
          AND TABLE_NAME = %s
        """,
        (table,),
    )
    cols = {r["COLUMN_NAME"].lower() for r in (cursor.fetchall() or [])}
    for c in candidates:
        if c.lower() in cols:
            return c
    return None


def get_subordinate_user_ids(cursor, role: str, logged_in_user_id: int) -> list[int] | None:
    """
    Returns:
      - None for admin (means ALL)
      - list[int] for other roles (users under them, including self)
    """
    role = (role or "").strip().lower()
    v = str(logged_in_user_id)

    if role in ["admin", "super admin"]:
        return None

    if role == "agent":
        return [logged_in_user_id]

    # ✅ QA: agents mapped via tfs_user.qa_id containing QA user_id
    if role == "qa":
        cursor.execute(
            f"""
            SELECT DISTINCT tu.user_id
            FROM tfs_user tu
            WHERE tu.is_active=1 AND tu.is_delete=1
              AND {multi_id_match_sql("tu.qa_id")}
            """,
            (v, v),
        )
        rows = cursor.fetchall() or []
        ids = [int(r["user_id"]) for r in rows if r.get("user_id") is not None]
        if logged_in_user_id not in ids:
            ids.append(logged_in_user_id)
        return ids

    # ✅ Assistant Manager: MUST come from tfs_user mapping (NOT project table)
    if role == "assistant manager":
        # Put your real column first (example: assistant_manager_id / asst_manager_id / reporting_manager_id)
        col = detect_existing_column(
            cursor,
            "tfs_user",
            [
                "assistant_manager_id",
                "asst_manager_id",
                "asst_reporting_manager_id",
                "reporting_manager_id",  # if your org uses same column for all managers
            ],
        )
        if not col:
            # no mapping column => only self
            return [logged_in_user_id]

        cursor.execute(
            f"""
            SELECT tu.user_id
            FROM tfs_user tu
            WHERE tu.is_active=1 AND tu.is_delete=1
              AND {multi_id_match_sql(f"tu.{col}")}
            """,
            (v, v),
        )
        rows = cursor.fetchall() or []
        ids = [int(r["user_id"]) for r in rows if r.get("user_id") is not None]
        if logged_in_user_id not in ids:
            ids.append(logged_in_user_id)
        return ids

    # ✅ Project Manager: also from tfs_user mapping (not project table)
    if role in ["manager", "project manager", "product manager"]:
        col = detect_existing_column(
            cursor,
            "tfs_user",
            [
                "reporting_manager_id",
                "manager_id",
                "project_manager_id",
                "reporting_to",
            ],
        )
        if not col:
            return [logged_in_user_id]

        cursor.execute(
            f"""
            SELECT tu.user_id
            FROM tfs_user tu
            WHERE tu.is_active=1 AND tu.is_delete=1
              AND {multi_id_match_sql(f"tu.{col}")}
            """,
            (v, v),
        )
        rows = cursor.fetchall() or []
        ids = [int(r["user_id"]) for r in rows if r.get("user_id") is not None]
        if logged_in_user_id not in ids:
            ids.append(logged_in_user_id)
        return ids

    return [logged_in_user_id]


# -----------------------------
# PROJECT/TASK VISIBILITY (INDIVIDUAL ROLE LOGIC)
# -----------------------------
def get_projects_for_role(cursor, role: str, logged_in_user_id: int) -> list[dict]:
    role = (role or "").strip().lower()
    v = str(logged_in_user_id)

    if role in ["admin", "super admin"]:
        cursor.execute(
            """
            SELECT project_id, project_name, project_code, project_description,
                   project_manager_id, asst_project_manager_id, project_qa_id, project_team_id
            FROM project
            WHERE is_active=1
            ORDER BY project_id DESC
            """
        )
        return cursor.fetchall() or []

    if role in ["manager", "project manager", "product manager"]:
        cursor.execute(
            f"""
            SELECT project_id, project_name, project_code, project_description,
                   project_manager_id, asst_project_manager_id, project_qa_id, project_team_id
            FROM project
            WHERE is_active=1 AND {multi_id_match_sql("project_manager_id")}
            ORDER BY project_id DESC
            """,
            (v, v),
        )
        return cursor.fetchall() or []

    if role == "assistant manager":
        cursor.execute(
            f"""
            SELECT project_id, project_name, project_code, project_description,
                   project_manager_id, asst_project_manager_id, project_qa_id, project_team_id
            FROM project
            WHERE is_active=1 AND {multi_id_match_sql("asst_project_manager_id")}
            ORDER BY project_id DESC
            """,
            (v, v),
        )
        return cursor.fetchall() or []

    if role == "qa":
        cursor.execute(
            f"""
            SELECT project_id, project_name, project_code, project_description,
                   project_manager_id, asst_project_manager_id, project_qa_id, project_team_id
            FROM project
            WHERE is_active=1 AND {multi_id_match_sql("project_qa_id")}
            ORDER BY project_id DESC
            """,
            (v, v),
        )
        return cursor.fetchall() or []

    # Agent: show projects from their trackers
    cursor.execute(
        """
        SELECT DISTINCT p.project_id, p.project_name, p.project_code, p.project_description,
                        p.project_manager_id, p.asst_project_manager_id, p.project_qa_id, p.project_team_id
        FROM task_work_tracker twt
        JOIN project p ON p.project_id = twt.project_id
        WHERE twt.is_active=1 AND p.is_active=1 AND twt.user_id=%s
        ORDER BY p.project_id DESC
        """,
        (logged_in_user_id,),
    )
    return cursor.fetchall() or []


def get_tasks_for_role(cursor, role: str, logged_in_user_id: int, project_ids: list[int]) -> list[dict]:
    if not project_ids:
        return []

    params: list = []
    in_sql = build_in_clause_int(project_ids, params)

    cursor.execute(
        f"""
        SELECT task_id, project_id, task_team_id, task_name, task_description, task_target
        FROM task
        WHERE is_active=1 AND project_id {in_sql}
        ORDER BY task_id DESC
        """,
        tuple(params),
    )
    return cursor.fetchall() or []


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

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    try:
        logged_role = get_user_role(cursor, int(logged_in_user_id))
        if not logged_role:
            return api_response(404, "Logged in user not found")

        # ✅ USERS UNDER LOGGED-IN (HIERARCHY) FIRST
        visible_user_ids = get_subordinate_user_ids(cursor, logged_role, int(logged_in_user_id))

        # --------------------
        # TRACKERS (ONLY THOSE USERS)
        # --------------------
        base_from = """
            FROM task_work_tracker twt
            JOIN tfs_user u ON u.user_id = twt.user_id
            JOIN project p ON p.project_id = twt.project_id
        """
        where_sql = """
            WHERE u.is_active=1 AND u.is_delete=1
              AND twt.is_active=1
              AND p.is_active=1
        """
        params: list = []

        if visible_user_ids is not None:
            where_sql += f" AND twt.user_id {build_in_clause_int(visible_user_ids, params)}"

        # Ensure requested user_id cannot leak outside visible set
        if data.get("user_id") and visible_user_ids is not None:
            req_uid = int(data["user_id"])
            if req_uid not in set(visible_user_ids):
                return api_response(
                    200,
                    "Dashboard data fetched successfully",
                    {
                        "logged_in_role": logged_role,
                        "filters_applied": {
                            "user_id": data.get("user_id"),
                            "project_id": data.get("project_id"),
                            "task_id": data.get("task_id"),
                            "date": data.get("date"),
                            "date_from": data.get("date_from"),
                            "date_to": data.get("date_to"),
                        },
                        "summary": {
                            "user_count": 0,
                            "project_count": 0,
                            "task_count": 0,
                            "tracker_rows": 0,
                            "total_production": 0,
                            "total_billable_hours": 0,
                        },
                        "users": [],
                        "projects": [],
                        "tasks": [],
                        "tracker": [],
                    },
                )

        # Apply all existing tracker filters
        where_sql, params = apply_tracker_filters(data, where_sql, params)

        # USERS list (from trackers scope)
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

        # TRACKER rows
        tracker_query = f"""
            SELECT
                twt.tracker_id,
                twt.user_id,
                twt.actual_target,
                twt.tenure_target,
                u.user_name,
                twt.project_id,
                p.project_name,
                twt.task_id,
                twt.production,
                twt.billable_hours,
                twt.date_time,
                twt.tracker_file
            {base_from}
            {where_sql}
            ORDER BY {TRACKER_DT} DESC
            LIMIT 500
        """
        cursor.execute(tracker_query, tuple(params))
        tracker_rows = cursor.fetchall()

        # tracker_files_url = f"{UPLOAD_FOLDER}/{UPLOAD_SUBDIRS['TRACKER_FILES']}/"
        tracker_files_url = f"{BASE_UPLOAD_URL}/{UPLOAD_SUBDIRS['TRACKER_FILES']}/"
        
        for t in tracker_rows:
            tracker_file_temp = t.get("tracker_file")
            t["tracker_file"] = tracker_files_url + tracker_file_temp if tracker_file_temp else None

        # SUMMARY
        summary_query = f"""
            SELECT
                COUNT(DISTINCT twt.user_id) AS user_count,
                COUNT(DISTINCT twt.project_id) AS project_count,
                COUNT(DISTINCT twt.task_id) AS task_count,
                COUNT(*) AS tracker_rows,
                COALESCE(SUM(twt.production), 0) AS total_production,
                COALESCE(SUM(twt.billable_hours), 0) AS total_billable_hours
            {base_from}
            {where_sql}
        """
        cursor.execute(summary_query, tuple(params))
        summary = cursor.fetchone() or {}

        # --------------------
        # PROJECTS / TASKS (INDIVIDUAL ROLE LOGIC)
        # --------------------
        projects = get_projects_for_role(cursor, logged_role, int(logged_in_user_id))
        project_ids = [p["project_id"] for p in projects]
        tasks = get_tasks_for_role(cursor, logged_role, int(logged_in_user_id), project_ids)

        # Billable hours for only returned projects but from SAME tracker scope
        billable_query = f"""
            SELECT
                p.project_id,
                COALESCE(SUM(twt.billable_hours), 0) AS total_billable_hours
            {base_from}
            {where_sql}
            GROUP BY p.project_id
        """
        cursor.execute(billable_query, tuple(params))
        bill_rows = cursor.fetchall() or []
        billable_map = {r["project_id"]: r["total_billable_hours"] for r in bill_rows}
        for pr in projects:
            pr["total_billable_hours"] = billable_map.get(pr["project_id"], 0)

        return api_response(
            200,
            "Dashboard data fetched successfully",
            {
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
                "tasks": tasks,
                "tracker": tracker_rows,
            },
        )

    except Exception:
        import logging

        logging.exception("Dashboard filter failed")
        return api_response(500, "Dashboard filter failed due to an internal error.")

    finally:
        try:
            cursor.close()
        except Exception:
            pass
        try:
            conn.close()
        except Exception:
            pass
