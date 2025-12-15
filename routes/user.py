from flask import Blueprint, request
from utils.response import api_response
from config import get_db_connection

user_bp = Blueprint("user", __name__)


@user_bp.route("/list", methods=["POST"])
def list_users():
    """
    List users with manager name, optional filters by role, designation, manager.
    """
    data = request.get_json() or {}
    role_filter = data.get("role")
    designation_filter = data.get("designation")
    manager_filter = data.get("reporting_manager")  # can be manager's user_id or name

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    try:
        # Base query with self-join to get manager name
        query = """
            SELECT 
                u.user_id,
                u.user_name,
                u.user_email,
                u.user_role,
                u.user_designation,
                m.user_name AS reporting_manager_name
            FROM tfs_user u
            LEFT JOIN tfs_user m ON u.reporting_manager = m.user_id
            WHERE 1=1
        """
        params = []

        # Optional filters
        if role_filter:
            query += " AND u.user_role = %s"
            params.append(role_filter)

        if designation_filter:
            query += " AND u.user_designation = %s"
            params.append(designation_filter)

        if manager_filter:
            query += " AND m.user_name = %s"
            params.append(manager_filter)

        query += " ORDER BY u.user_id DESC"

        cursor.execute(query, tuple(params))
        results = cursor.fetchall()

        users_list = []
        for row in results:
            users_list.append({
                "user_id": row["user_id"],
                "user_name": row["user_name"],
                "user_email": row["user_email"],
                "user_role": row["user_role"],
                "designation": row.get("user_designation"),
                "reporting_to": row.get("reporting_manager_name")  # display name
            })

        return api_response(200, "Users fetched successfully", users_list)

    except Exception as e:
        return api_response(500, f"Failed to fetch users: {str(e)}")

    finally:
        cursor.close()
        conn.close()
        
        
@user_bp.route("/update_user", methods=["PUT"])
def update_user():
    data = request.get_json()

    if not data:
        return api_response(400, "Invalid JSON or no body received")

    # Required field
    user_id = data.get("user_id")
    if not user_id:
        return api_response(400, "user_id is required")

    conn = get_db_connection()
    cursor = conn.cursor()

    try:
        # -------------------------------------------------------
        # 1. Dynamic UPDATE for tfs_user
        # -------------------------------------------------------
        user_fields = {
            "user_name": data.get("user_name"),
            "user_number": data.get("user_number"),
            "user_address": data.get("user_address"),
            "user_role": data.get("user_role"),
            "designation": data.get("designation"),
            "reporting_manager": data.get("reporting_manager"),
            "device_type": data.get("device_type"),
            "device_id": data.get("device_id")
        }

        user_update_cols = []
        user_update_vals = []

        for col, val in user_fields.items():
            if val is not None:   # update only provided fields
                user_update_cols.append(f"{col} = %s")
                user_update_vals.append(val)

        if user_update_cols:
            update_user_query = f"""
                UPDATE tfs_user
                SET {', '.join(user_update_cols)}
                WHERE user_id = %s
            """
            user_update_vals.append(user_id)
            print(update_user_query, user_update_vals)
            cursor.execute(update_user_query, user_update_vals)

        # -------------------------------------------------------
        # 2. Dynamic UPDATE for user_role
        # -------------------------------------------------------
        role_fields = {
            "role_name": data.get("role_name"),
            "project_creation_permission": data.get("project_creation_permission"),
            "user_creation_permission": data.get("user_creation_permission")
        }

        role_update_cols = []
        role_update_vals = []

        for col, val in role_fields.items():
            if val is not None:
                role_update_cols.append(f"{col} = %s")
                role_update_vals.append(val)

        if role_update_cols:
            update_role_query = f"""
                UPDATE user_role
                SET {', '.join(role_update_cols)}
                WHERE user_id = %s
            """
            role_update_vals.append(user_id)
            cursor.execute(update_role_query, role_update_vals)

        conn.commit()
        return api_response(200, "User updated successfully")

    except Exception as e:
        conn.rollback()
        return api_response(500, f"Failed to update user: {str(e)}")

    finally:
        cursor.close()
        conn.close()


@user_bp.route("/delete_user", methods=["PUT"])
def delete_user():
    data = request.get_json()

    if not data:
        return api_response(400, "Invalid JSON or no body received")

    user_id = data.get("user_id")
    if not user_id:
        return api_response(400, "user_id is required")

    conn = get_db_connection()
    cursor = conn.cursor()

    try:
        query = """
            UPDATE tfs_user
            SET is_active = 0
            WHERE user_id = %s
        """
        cursor.execute(query, (user_id,))
        conn.commit()

        return api_response(200, "User deactivated successfully")

    except Exception as e:
        conn.rollback()
        return api_response(500, f"Failed to deactivate user: {str(e)}")

    finally:
        cursor.close()
        conn.close()
