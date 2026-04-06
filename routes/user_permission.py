from flask import Blueprint, request
from utils.response import api_response
from config import get_db_connection

permission_bp = Blueprint("permission", __name__, url_prefix="/permission")


@permission_bp.route("/user_list", methods=["POST"])
def user_list_with_permissions():
    data = request.get_json() or {}
    logged_in_user_id = data.get("logged_in_user_id")
    filter_role = data.get("role")  # Optional role filter

    if not logged_in_user_id:
        return api_response(400, "logged_in_user_id is required")

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    try:
        # 1) Get role of logged-in user
        cursor.execute("""
            SELECT r.role_name
            FROM tfs_user u
            JOIN user_role r ON r.role_id = u.role_id
            WHERE u.user_id = %s AND u.is_active = 1 AND u.is_delete = 1
        """, (logged_in_user_id,))
        role_row = cursor.fetchone()

        if not role_row:
            return api_response(404, "User not found")

        role = (role_row["role_name"] or "").lower()

        # 2) Block QA and Agent
        if role in ["qa", "agent"]:
            return api_response(403, "You are not allowed to view user permissions", [])

        # 3) Base query: user details + role + designation + permissions
        query = """
            SELECT
                u.user_id,
                u.user_name,
                u.user_email,
                u.user_number,
                u.user_address,

                r.role_name AS role,
                d.designation_id,
                d.designation,

                COALESCE(up.project_creation_permission, 0) AS project_creation_permission,
                COALESCE(up.user_creation_permission, 0) AS user_creation_permission

            FROM tfs_user u
            LEFT JOIN user_role r ON r.role_id = u.role_id
            LEFT JOIN user_designation d ON d.designation_id = u.designation_id
            LEFT JOIN user_permission up ON up.user_id = u.user_id

            WHERE u.is_active = 1 AND u.is_delete = 1
        """
        params = []

        # 4) Role-based filtering (align with user.py)
        if role == "qa":
            query += " AND u.qa_id = %s"
            params.append(logged_in_user_id)
        elif role == "assistant manager":
            query += " AND u.asst_manager_id = %s"
            params.append(logged_in_user_id)
        elif role == "manager" or role == "project manager":
            query += " AND u.project_manager_id = %s"
            params.append(logged_in_user_id)
        # admin / super admin -> no extra filter

        # 5) Additional filter: if filter_role is provided, filter by that role
        if filter_role:
            query += " AND LOWER(r.role_name) = %s"
            params.append(filter_role.strip().lower())

        query += " ORDER BY u.user_id DESC"

        cursor.execute(query, params)
        users = cursor.fetchall()

        # Debug info for troubleshooting
        debug_info = {
            "detected_role": role,
            "sql": query,
            "params": params
        }

        # return api_response(200, "Users with permissions fetched successfully", {"users": users, "debug": debug_info})
        return api_response(200, "Users with permissions fetched successfully", {"users": users})

    except Exception as e:
        return api_response(500, f"Failed to fetch user permissions list: {str(e)}")

    finally:
        cursor.close()
        conn.close()


@permission_bp.route("/update", methods=["POST"])
def update_user_permission():
    data = request.get_json() or {}

    user_id = data.get("user_id")               # logged-in user
    target_user_id = data.get("target_user_id") # user to update

    if not user_id:
        return api_response(400, "user_id is required")
    if not target_user_id:
        return api_response(400, "target_user_id is required")

    project_perm = data.get("project_creation_permission")
    user_perm = data.get("user_creation_permission")

    if project_perm is None and user_perm is None:
        return api_response(400, "No permission fields provided")

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    try:
        # --------------------------------------------------
        # 1) Get role of logged-in user
        # --------------------------------------------------
        cursor.execute("""
            SELECT r.role_name
            FROM tfs_user u
            JOIN user_role r ON r.role_id = u.role_id
            WHERE u.user_id = %s AND u.is_active = 1 AND u.is_delete = 1
        """, (user_id,))
        role_row = cursor.fetchone()

        if not role_row:
            return api_response(404, "User not found")

        role = role_row["role_name"].lower()

        # --------------------------------------------------
        # 2) Block QA & Agent
        # --------------------------------------------------
        if role in ["qa", "agent"]:
            return api_response(403, "You are not allowed to update permissions")

        # --------------------------------------------------
        # 3) Check target user exists
        # --------------------------------------------------
        cursor.execute("""
            SELECT user_id, project_manager_id, asst_manager_id, role_id
            FROM tfs_user
            WHERE user_id = %s AND is_active = 1 AND is_delete = 1
        """, (target_user_id,))
        target_user = cursor.fetchone()

        if not target_user:
            return api_response(404, "Target user not found")

        # --------------------------------------------------
        # 4) Hierarchy check
        # --------------------------------------------------
        # if role == "assistant manager":
        #     if target_user["asst_manager_id"] != user_id:
        #         return api_response(403, "You can update permissions only for your users")

        # elif role == "manager":
        #     if target_user["project_manager_id"] != user_id:
        #         return api_response(403, "You can update permissions only for your users")

        # admin / super admin → no restriction

        # --------------------------------------------------
        # 5) UPSERT permission
        # --------------------------------------------------
        cursor.execute("SELECT user_id FROM user_permission WHERE user_id=%s", (target_user_id,))
        exists = cursor.fetchone() is not None

        fields = []
        values = []

        if project_perm is not None:
            fields.append("project_creation_permission=%s")
            values.append(project_perm)

        if user_perm is not None:
            fields.append("user_creation_permission=%s")
            values.append(user_perm)

        values.append(target_user_id)

        if exists:
            query = f"""
                UPDATE user_permission
                SET {', '.join(fields)}
                WHERE user_id=%s
            """
            cursor.execute(query, values)
        else:
            cursor.execute("""
                INSERT INTO user_permission
                (user_id, role_id, project_creation_permission, user_creation_permission)
                VALUES (%s, %s, %s, %s)
            """, (
                target_user_id,
                target_user["role_id"],
                project_perm or 0,
                user_perm or 0
            ))

        conn.commit()
        return api_response(200, "User permissions updated successfully")

    except Exception as e:
        conn.rollback()
        return api_response(500, f"Failed to update permissions: {str(e)}")

    finally:
        cursor.close()
        conn.close()
