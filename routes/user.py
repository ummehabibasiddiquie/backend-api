from flask import Blueprint, request
from utils.response import api_response
from config import get_db_connection
from config import UPLOAD_SUBDIRS, BASE_UPLOAD_URL
import os
from utils.validators import validate_request
from utils.json_utils import to_db_json
from datetime import datetime

user_bp = Blueprint("user", __name__)


@user_bp.route("/list", methods=["POST"])
def list_users():
    
    data, err = validate_request(required=["user_id"])
    if err:
        return err
        
    user_id = data.get("user_id")

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    
    UPLOAD_URL_PREFIX = "/uploads" 
    
    try:
        # --------------------------------------------------
        # 1. Get role of requesting user
        # --------------------------------------------------
        cursor.execute("""
            SELECT r.role_name
            FROM tfs_user u
            JOIN user_role r ON r.role_id = u.role_id
            WHERE u.user_id = %s AND u.is_active = 1 and is_delete = 1
        """, (user_id,))
        role_row = cursor.fetchone()

        if not role_row:
            return api_response(404, "User not found")

        role = role_row["role_name"].lower()

        # --------------------------------------------------
        # 2. Agent → no users
        # --------------------------------------------------
        if role == "agent":
            return api_response(200, "No users available", [])

        # --------------------------------------------------
        # 3. Base query
        # --------------------------------------------------
        query = """
            SELECT
                u.user_id,
                u.user_name,
                u.user_email,
                u.user_number,
                u.user_address,
                u.user_password,
                u.user_tenure,
                u.profile_picture,
                u.is_active,

                r.role_name AS role,
                t.team_name,
                d.designation_id,
                d.designation,

                pm.user_name AS project_manager,
                am.user_name AS asst_manager,
                qa.user_name AS qa

            FROM tfs_user u
            LEFT JOIN user_role r ON r.role_id = u.role_id
            LEFT JOIN user_designation d ON d.designation_id = u.designation_id
            left join team t on u.team_id = t.team_id

            LEFT JOIN tfs_user pm ON pm.user_id = u.project_manager_id
            LEFT JOIN tfs_user am ON am.user_id = u.asst_manager_id
            LEFT JOIN tfs_user qa ON qa.user_id = u.qa_id

            WHERE u.is_delete = 1
        """

        params = []

        # --------------------------------------------------
        # 4. Role-based filtering
        # --------------------------------------------------
        if role == "qa":
            query += " AND u.qa_id = %s"
            params.append(user_id)

        elif role == "assistant manager":
            query += " AND u.asst_manager_id = %s"
            params.append(user_id)

        elif role == "manager":
            query += " AND u.project_manager_id = %s"
            params.append(user_id)

        # admin / super admin → no extra filter

        query += " ORDER BY u.user_id DESC"
        # print(query)

        cursor.execute(query, params)
        users = cursor.fetchall()
        
        for u in users:
            filename = u.get("profile_picture")  # DB column
            if filename:
                # u["profile_picture"] = f"{UPLOAD_URL_PREFIX}/{UPLOAD_SUBDIRS['PROFILE_PIC']}/{filename}"
                u["profile_picture"] = f"{BASE_UPLOAD_URL}/{UPLOAD_SUBDIRS['PROFILE_PIC']}/{filename}"
            else:
                u["profile_picture"] = None

        return api_response(200, "Users fetched successfully", users)

    except Exception as e:
        return api_response(500, f"Failed to fetch users: {str(e)}")

    finally:
        cursor.close()
        conn.close()

        
@user_bp.route("/update_user", methods=["PUT"])
def update_user():
    data = request.get_json()
    # print(data)

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
            "role_id": data.get("role_id"),
            "designation_id": data.get("designation_id"),
            "reporting_manager": data.get("reporting_manager"),
            "is_active": data.get("is_active"),
            "user_tenure": data.get("user_tenure"),
            "team_id": data.get("team_id"),
            
            # ✅ JSON columns (store as JSON)
            "project_manager_id": data.get("project_manager_id"),
            "asst_manager_id": to_db_json(data.get("asst_manager_id"), allow_single=True),
            "qa_id": to_db_json(data.get("qa_id"), allow_single=True)
        }
        
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        user_update_cols = []
        user_update_vals = []

        for col, val in user_fields.items():
            if val is not None:   # update only provided fields
                user_update_cols.append(f"{col} = %s")
                user_update_vals.append(val)
                user_update_cols.append("updated_date=%s")
                user_update_vals.append(now_str)

        if user_update_cols:
            update_user_query = f"""
                UPDATE tfs_user
                SET {', '.join(user_update_cols)}
                WHERE user_id = %s
            """
            # print(update_user_query)
            user_update_vals.append(user_id)
            # print(update_user_query, user_update_vals)
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
            SET is_delete = 0, is_active = 0
            WHERE user_id = %s
        """
        cursor.execute(query, (user_id,))
        conn.commit()

        return api_response(200, "User Deleted successfully")

    except Exception as e:
        conn.rollback()
        return api_response(500, f"Failed to Delete user: {str(e)}")

    finally:
        cursor.close()
        conn.close()
