from flask import Blueprint, request
from utils.response import api_response
from config import get_db_connection

dropdown_bp = Blueprint("dropdown", __name__)

ROLE_BASED_USER_DROPDOWNS = (
    "super admin",
    "admin",
    "project manager",
    "assistant manager",
    "qa",
    "agent"
)

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


def multi_id_match_sql(col: str) -> str:
    # supports: 78 / 78,81 / [78] / [78,81] / ["78","81"] / spaces
    cleaned = f"REPLACE(REPLACE(REPLACE(REPLACE({col},'[',''),']',''),'\"',''),' ','')"
    return f"({col} = %s OR FIND_IN_SET(%s, {cleaned}) > 0)"


# ---------------- GET DROPDOWN DATA ---------------- #
@dropdown_bp.route("/get", methods=["POST"])
def get():
    data = request.get_json()
    if not data or "dropdown_type" not in data:
        return api_response(400, "dropdown_type is required")

    dropdown_type = (data["dropdown_type"] or "").strip().lower()

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    try:
        # -------------------- DESIGNATIONS -------------------- #
        if dropdown_type == "designations":
            query = """
                SELECT designation_id, designation AS label
                FROM user_designation
                WHERE is_active = 1
                ORDER BY designation
            """
            params = []

            cursor.execute(query, params)
            result = cursor.fetchall()

            for item in result:
                if item.get("label"):
                    item["label"] = item["label"].title()

            return api_response(200, "Dropdown data fetched successfully", result)

        # -------------------- USER ROLES -------------------- #
        if dropdown_type == "user roles":
            query = """
                SELECT role_id, role_name AS label
                FROM user_role
                WHERE is_active = 1
                ORDER BY role_name
            """
            params = []

            cursor.execute(query, params)
            result = cursor.fetchall()

            for item in result:
                if item.get("label"):
                    item["label"] = item["label"].title()

            return api_response(200, "Dropdown data fetched successfully", result)

        # -------------------- TEAMS -------------------- #
        if dropdown_type == "teams":
            query = """
                SELECT team_id, team_name AS label
                FROM team
                WHERE is_active = 1
                ORDER BY team_name
            """
            params = []

            cursor.execute(query, params)
            result = cursor.fetchall()

            for item in result:
                if item.get("label"):
                    item["label"] = item["label"].title()

            return api_response(200, "Dropdown data fetched successfully", result)

        # -------------------- ROLE-BASED USER LIST -------------------- #
        if dropdown_type in ROLE_BASED_USER_DROPDOWNS:
            query = """
                SELECT
                    u.user_id,
                    u.user_name AS label
                FROM tfs_user u
                JOIN user_role r ON r.role_id = u.role_id
                WHERE u.is_active = 1
                  AND u.is_delete = 1
                  AND r.is_active = 1
                  AND LOWER(r.role_name) = %s
                ORDER BY u.user_name
            """
            params = (dropdown_type,)

            cursor.execute(query, params)
            result = cursor.fetchall()

            for item in result:
                if item.get("label"):
                    item["label"] = item["label"].title()

            return api_response(200, "Dropdown data fetched successfully", result)

        # -------------------- PROJECTS WITH TASKS -------------------- #
        if dropdown_type == "projects with tasks":
            logged_in_user_id = data.get("logged_in_user_id")
            if not logged_in_user_id:
                return api_response(400, "logged_in_user_id is required for projects with tasks")

            logged_in_user_id = int(logged_in_user_id)
            logged_role = get_user_role(cursor, logged_in_user_id)
            if not logged_role:
                return api_response(404, "Logged in user not found")

            # Role scope
            params: list = []
            where_sql = "WHERE p.is_active = 1"

            if logged_role in ["admin", "super admin"]:
                pass

            elif logged_role == "qa":
                v = str(logged_in_user_id)
                where_sql += " AND " + multi_id_match_sql("p.project_qa_id")
                params.extend([v, v])

            elif logged_role in ["project manager", "manager"]:
                v = str(logged_in_user_id)
                where_sql += " AND " + multi_id_match_sql("p.project_manager_id")
                params.extend([v, v])

            elif logged_role == "assistant manager":
                v = str(logged_in_user_id)
                where_sql += " AND " + multi_id_match_sql("p.asst_project_manager_id")
                params.extend([v, v])

            elif logged_role == "agent":
                # ✅ NO tracker dependency: check assignment inside project_team_id
                v = str(logged_in_user_id)
                where_sql += " AND " + multi_id_match_sql("p.project_team_id")
                params.extend([v, v])

            else:
                # safest fallback: same as agent assignment rule
                v = str(logged_in_user_id)
                where_sql += " AND " + multi_id_match_sql("p.project_team_id")
                params.extend([v, v])

            # ✅ Optional: filter tasks by task_team_id for agent
            task_join_extra = ""
            task_params: list = []
            if logged_role == "agent":
                v = str(logged_in_user_id)
                task_join_extra = " AND " + multi_id_match_sql("t.task_team_id")
                task_params.extend([v, v])

            query = f"""
                SELECT
                    p.project_id,
                    p.project_name,
                    t.task_id,
                    t.task_name,
                    t.task_target
                FROM project p
                LEFT JOIN task t
                    ON t.project_id = p.project_id
                    AND t.is_active = 1
                    {task_join_extra}
                {where_sql}
                ORDER BY p.project_name, t.task_name
            """

            cursor.execute(query, tuple(params + task_params))
            rows = cursor.fetchall()

            projects_map = {}
            for row in rows:
                pid = row["project_id"]
                if pid not in projects_map:
                    projects_map[pid] = {
                        "project_id": pid,
                        "project_name": row["project_name"],
                        "tasks": []
                    }

                if row.get("task_id"):
                    projects_map[pid]["tasks"].append({
                        "task_id": row["task_id"],
                        "label": row["task_name"],
                        "task_target": row["task_target"]
                    })

            return api_response(200, "Dropdown data fetched successfully", list(projects_map.values()))

        # -------------------- INVALID -------------------- #
        return api_response(400, "Invalid dropdown_type")

    except Exception as e:
        return api_response(500, f"Failed to fetch dropdown data: {str(e)}")

    finally:
        try:
            cursor.close()
        except Exception:
            pass
        try:
            conn.close()
        except Exception:
            pass
