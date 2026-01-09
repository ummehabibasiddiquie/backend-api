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
# ---------------- GET DROPDOWN DATA ---------------- #
@dropdown_bp.route("/get", methods=["POST"])
def get():
    data = request.get_json()
    if not data or "dropdown_type" not in data:
        return api_response(400, "dropdown_type is required")

    dropdown_type = data["dropdown_type"]
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    try:
        if dropdown_type == "designations":
            query = """
                SELECT designation_id, designation AS label
                FROM user_designation
                WHERE is_active = 1
                ORDER BY designation
            """
            params = []

        elif dropdown_type == "user roles":
            query = """
                SELECT role_id, role_name AS label
                FROM user_role
                WHERE is_active = 1
                ORDER BY role_name
            """
            params = []
            
        elif dropdown_type == "teams":
            query = """
                SELECT team_id,team_name AS label
                FROM team
                WHERE is_active = 1
                ORDER BY team_name
            """
            params = []

        elif dropdown_type in ROLE_BASED_USER_DROPDOWNS:
            query = """
                SELECT 
                    u.user_id,
                    u.user_name AS label
                FROM tfs_user u
                JOIN user_role r ON r.role_id = u.role_id
                WHERE u.is_active = 1
                    AND u.is_delete = 1
                    AND r.is_active = 1
                    AND r.role_name = %s
                ORDER BY u.user_name
            """
            params = (dropdown_type,)
        
        elif dropdown_type == "projects with tasks":
            query = """
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
                WHERE p.is_active = 1
                ORDER BY p.project_name, t.task_name
            """
            cursor.execute(query)
            rows = cursor.fetchall()

            projects_map = {}

            for row in rows:
                project_id = row["project_id"]

                if project_id not in projects_map:
                    projects_map[project_id] = {
                        "project_id": project_id,
                        "project_name": row["project_name"],
                        "tasks": []
                    }

                if row["task_id"]:
                    projects_map[project_id]["tasks"].append({
                        "task_id": row["task_id"],
                        "label": row["task_name"],
                        "task_target": row["task_target"]
                    })

            result = list(projects_map.values())

            return api_response(
                200,
                "Dropdown data fetched successfully",
                result
            )

        

        else:
            return api_response(400, "Invalid dropdown_type")

        cursor.execute(query, params)
        result = cursor.fetchall()
        for item in result:
            if "label" in item and item["label"]:
                item["label"] = item["label"].title()
        return api_response(200, "Dropdown data fetched successfully", result)

    except Exception as e:
        return api_response(500, f"Failed to fetch dropdown data: {str(e)}")

    finally:
        cursor.close()
        conn.close()
