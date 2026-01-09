from flask import Blueprint, request
from utils.response import api_response
from config import get_db_connection, UPLOAD_FOLDER, UPLOAD_SUBDIRS
from utils.file_utils import save_base64_project_file
import json
import os
from datetime import datetime

project_bp = Blueprint("project", __name__)

# ---------------- HELPER ---------------- #
def get_users_by_ids(conn, ids):
    """Fetch user details by ID list."""
    if not ids:
        return []
    cursor = conn.cursor(dictionary=True)
    format_strings = ",".join(["%s"] * len(ids))
    cursor.execute(f"SELECT user_id, user_name FROM tfs_user WHERE user_id IN ({format_strings})", tuple(ids))
    users = cursor.fetchall()
    cursor.close()
    return users

# ---------------- CREATE PROJECT ---------------- #
@project_bp.route("/create", methods=["POST"])
def create_project():
    data = request.get_json()
    if not data:
        return api_response(400, "Request body is required")

    required_fields = ["project_name", "project_code", "project_manager_id"]
    for field in required_fields:
        if field not in data:
            return api_response(400, f"{field} is required")

    device_id = data.get("device_id")
    device_type = data.get("device_type")

    # File handling
    project_pprt_base64 = None
    project_pprt = None
    if data.get("files"):
        try:
            project_pprt_base64 = data["files"]
            project_pprt = save_base64_project_file(data["files"])
        except Exception as e:
            return api_response(400, f"File handling failed: {str(e)}")

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    now_str = datetime.now().strftime("%d/%m/%Y %H:%M:%S")

    try:
        conn.start_transaction()
        cursor.execute("""
            INSERT INTO project (
                project_name,
                project_code,
                project_description,
                project_manager_id,
                asst_project_manager_id,
                project_team_id,
                project_qa_id,
                project_pprt_base64,
                project_pprt,
                created_date,
                updated_date
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (
            data["project_name"].strip(),
            data["project_code"].strip(),
            data.get("project_description", "").strip(),
            data["project_manager_id"],
            json.dumps(data.get("asst_project_manager_id", [])),
            json.dumps(data.get("project_team_id", [])),
            json.dumps(data.get("project_qa_id", [])),
            project_pprt_base64,
            project_pprt,
            now_str,
            now_str
        ))
        conn.commit()
        return api_response(201, "Project created successfully")
    except Exception as e:
        conn.rollback()
        return api_response(500, f"Project creation failed: {str(e)}")
    finally:
        cursor.close()
        conn.close()

# ---------------- UPDATE PROJECT ---------------- #
@project_bp.route("/update", methods=["PUT"])
def update_project():
    data = request.get_json()
    if not data or "project_id" not in data:
        return api_response(400, "project_id is required")

    project_id = data["project_id"]
    device_id = data.get("device_id")
    device_type = data.get("device_type")

    update_values = {}

    # JSON fields
    for key in ["asst_project_manager_id", "project_team_id", "project_qa_id"]:
        if key in data:
            update_values[key] = json.dumps(data[key])

    # String/int fields
    for key in ["project_name", "project_code", "project_description", "project_manager_id"]:
        if key in data:
            update_values[key] = data[key].strip() if isinstance(data[key], str) else data[key]

    # File replacement
    if data.get("files"):
        try:
            file_name = save_base64_project_file(data["files"])
            update_values["project_pprt"] = file_name
            update_values["project_pprt_base64"] = data["files"]
        except Exception as e:
            return api_response(400, f"File handling failed: {str(e)}")

    if not update_values:
        return api_response(400, "No valid fields provided for update")

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    updated_str = datetime.now().strftime("%d/%m/%Y %H:%M:%S")

    try:
        conn.start_transaction()
        cursor.execute("SELECT * FROM project WHERE project_id=%s AND is_active=1", (project_id,))
        if not cursor.fetchone():
            conn.rollback()
            return api_response(404, "Project not found")

        if "project_name" in update_values:
            cursor.execute("SELECT project_id FROM project WHERE project_name=%s AND is_active=1 AND project_id!=%s",
                           (update_values["project_name"], project_id))
            if cursor.fetchone():
                conn.rollback()
                return api_response(409, "Project name already exists")

        set_clause = ", ".join(f"{k}=%s" for k in update_values)
        cursor.execute(f"UPDATE project SET {set_clause}, updated_date=%s WHERE project_id=%s",
                       (*update_values.values(), updated_str, project_id))
        conn.commit()
        return api_response(200, "Project updated successfully")
    except Exception as e:
        conn.rollback()
        return api_response(500, f"Project update failed: {str(e)}")
    finally:
        cursor.close()
        conn.close()

# ---------------- SOFT DELETE PROJECT ---------------- #
@project_bp.route("/delete", methods=["PUT"])
def delete_project():
    data = request.get_json()
    if not data or "project_id" not in data:
        return api_response(400, "project_id is required")

    project_id = data["project_id"]
    device_id = data.get("device_id")
    device_type = data.get("device_type")

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    updated_str = datetime.now().strftime("%d/%m/%Y %H:%M:%S")

    try:
        conn.start_transaction()
        cursor.execute("SELECT project_id FROM project WHERE project_id=%s AND is_active=1", (project_id,))
        if not cursor.fetchone():
            conn.rollback()
            return api_response(404, "Project not found or already deleted")

        cursor.execute("UPDATE project SET is_active=0, updated_date=%s WHERE project_id=%s", (updated_str, project_id))
        conn.commit()
        return api_response(200, "Project deleted successfully")
    except Exception as e:
        conn.rollback()
        return api_response(500, f"Project deletion failed: {str(e)}")
    finally:
        cursor.close()
        conn.close()

# ---------------- LIST PROJECTS ---------------- #
@project_bp.route("/list", methods=["POST"])
def list_projects():
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    try:
        cursor.execute("""
            SELECT project_id, project_name, project_code, project_description, project_team_id,
                   project_manager_id, asst_project_manager_id, project_qa_id,
                   project_pprt, created_date, updated_date
            FROM project
            WHERE is_active=1
            ORDER BY project_id DESC
        """)
        projects = cursor.fetchall()
        result = []

        for proj in projects:
            project_team = get_users_by_ids(conn, json.loads(proj["project_team_id"] or "[]"))
            asst_managers = get_users_by_ids(conn, json.loads(proj["asst_project_manager_id"] or "[]"))
            qas = get_users_by_ids(conn, json.loads(proj["project_qa_id"] or "[]"))

            project_file_url = None
            if proj.get("project_pprt"):
                project_file_url = os.path.join("/uploads", UPLOAD_SUBDIRS["PROJECT_PPRT"], proj["project_pprt"])

            result.append({
                "project_id": proj["project_id"],
                "project_name": proj["project_name"],
                "project_code": proj["project_code"],
                "project_description": proj["project_description"],
                "project_manager_id": proj["project_manager_id"],
                "asst_project_managers": asst_managers,
                "project_team": project_team,
                "qa_users": qas,
                "project_file": project_file_url,
                "created_date": proj["created_date"],   # already in dd/mm/yyyy hh:mm:ss
                "updated_date": proj["updated_date"]    # already in dd/mm/yyyy hh:mm:ss
            })

        return api_response(200, "Project list fetched successfully", result)
    except Exception as e:
        return api_response(500, f"Failed to fetch projects: {str(e)}")
    finally:
        cursor.close()
        conn.close()
