# routes/project.py

from flask import Blueprint, request
from utils.response import api_response
from config import get_db_connection, UPLOAD_SUBDIRS
from utils.file_utils import save_base64_file
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
    cursor.execute(
        f"SELECT user_id, user_name FROM tfs_user WHERE user_id IN ({format_strings})",
        tuple(ids),
    )
    users = cursor.fetchall()
    cursor.close()
    return users


def safe_filename_part(value: str) -> str:
    """
    Make a safe filename component:
    - strip spaces
    - replace spaces with underscore
    - remove characters that can break paths
    """
    if value is None:
        return "NA"
    s = str(value).strip().replace(" ", "_")
    # remove path separators and other risky chars
    for ch in ['/', '\\', ':', '*', '?', '"', '<', '>', '|']:
        s = s.replace(ch, "")
    # avoid empty
    return s or "NA"


def build_project_pprt_filename(project_name: str, project_code: str) -> str:
    """
    Required format:
      <project_name>_<code>_<YYYYMMDD>.<ext derived from base64>
    NOTE: ext will be appended inside save_base64_file based on base64 header.
    """
    today = datetime.now().strftime("%Y%m%d")
    name_part = safe_filename_part(project_name)
    code_part = safe_filename_part(project_code)
    return f"{name_part}_{code_part}_{today}"


# ---------------- CREATE PROJECT ---------------- #
@project_bp.route("/create", methods=["POST"])
def create_project():
    data = request.get_json(silent=True) or {}
    if not data:
        return api_response(400, "Request body is required")
    if data["project_description"] is "null" :
        data["project_description"] = None

    required_fields = ["project_name", "project_code", "project_manager_id"]
    for field in required_fields:
        if field not in data:
            return api_response(400, f"{field} is required")

    # (kept as in your script, not used)
    device_id = data.get("device_id")
    device_type = data.get("device_type")

    # File handling (CHANGED: custom filename format)
    project_pprt_base64 = None
    project_pprt = None
    if data.get("file"):
        try:
            project_pprt_base64 = data["file"]

            custom_filename = build_project_pprt_filename(
                data.get("project_name", ""),
                data.get("project_code", "")
            )
            print(custom_filename)

            # IMPORTANT: save_base64_file must accept filename=...
            project_pprt = save_base64_file(
                data["file"],
                UPLOAD_SUBDIRS["PROJECT_PPRT"],
                filename=custom_filename
            )
            print("project_pprt",project_pprt)
        except Exception as e:
            return api_response(400, f"File handling failed: {str(e)}")

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    now_str = datetime.now().strftime("%d/%m/%Y %H:%M:%S")

    try:
        conn.start_transaction()
        cursor.execute(
            """
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
            """,
            (
                data["project_name"].strip(),
                data["project_code"].strip(),
                # data.get("project_description", "").strip(),
                data.get("project_description"),
                data["project_manager_id"],
                json.dumps(data.get("asst_project_manager_id", [])),
                json.dumps(data.get("project_team_id", [])),
                json.dumps(data.get("project_qa_id", [])),
                project_pprt_base64,
                project_pprt,
                now_str,
                now_str,
            ),
        )
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
    data = request.get_json(silent=True) or {}
    if not data or "project_id" not in data:
        return api_response(400, "project_id is required")

    project_id = data["project_id"]
    # (kept as in your script, not used)
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

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    updated_str = datetime.now().strftime("%d/%m/%Y %H:%M:%S")

    try:
        conn.start_transaction()
        cursor.execute("SELECT * FROM project WHERE project_id=%s AND is_active=1", (project_id,))
        existing = cursor.fetchone()
        if not existing:
            conn.rollback()
            return api_response(404, "Project not found")

        if "project_name" in update_values:
            cursor.execute(
                "SELECT project_id FROM project WHERE project_name=%s AND is_active=1 AND project_id!=%s",
                (update_values["project_name"], project_id),
            )
            if cursor.fetchone():
                conn.rollback()
                return api_response(409, "Project name already exists")

        # File replacement (CHANGED: custom filename format)
        if data.get("file"):
            try:
                # Use incoming values if provided, else use existing values from DB
                use_project_name = (
                    (data.get("project_name") or existing.get("project_name") or "PROJECT")
                )
                use_project_code = (
                    (data.get("project_code") or existing.get("project_code") or "CODE")
                )
                custom_filename = build_project_pprt_filename(use_project_name, use_project_code)

                file_name = save_base64_file(
                    data["file"],
                    UPLOAD_SUBDIRS["PROJECT_PPRT"],
                    filename=custom_filename
                )
                update_values["project_pprt"] = file_name
                update_values["project_pprt_base64"] = data["file"]
            except Exception as e:
                conn.rollback()
                return api_response(400, f"File handling failed: {str(e)}")

        if not update_values:
            conn.rollback()
            return api_response(400, "No valid fields provided for update")

        set_clause = ", ".join(f"{k}=%s" for k in update_values)
        cursor.execute(
            f"UPDATE project SET {set_clause}, updated_date=%s WHERE project_id=%s",
            (*update_values.values(), updated_str, project_id),
        )
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
    data = request.get_json(silent=True) or {}
    if not data or "project_id" not in data:
        return api_response(400, "project_id is required")

    project_id = data["project_id"]
    # (kept as in your script, not used)
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

        cursor.execute(
            "UPDATE project SET is_active=0, updated_date=%s WHERE project_id=%s",
            (updated_str, project_id),
        )
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
        cursor.execute(
            """
            SELECT project_id, project_name, project_code, project_description, project_team_id,
                   project_manager_id, asst_project_manager_id, project_qa_id,
                   project_pprt, created_date, updated_date
            FROM project
            WHERE is_active=1
            ORDER BY project_id DESC
            """
        )
        projects = cursor.fetchall()
        result = []

        for proj in projects:
            project_team = get_users_by_ids(conn, json.loads(proj["project_team_id"] or "[]"))
            asst_managers = get_users_by_ids(conn, json.loads(proj["asst_project_manager_id"] or "[]"))
            qas = get_users_by_ids(conn, json.loads(proj["project_qa_id"] or "[]"))

            project_file_url = None
            if proj.get("project_pprt"):
                project_file_url = os.path.join("/uploads", UPLOAD_SUBDIRS["PROJECT_PPRT"], proj["project_pprt"])

            result.append(
                {
                    "project_id": proj["project_id"],
                    "project_name": proj["project_name"],
                    "project_code": proj["project_code"],
                    "project_description": proj["project_description"],
                    "project_manager_id": proj["project_manager_id"],
                    "asst_project_managers": asst_managers,
                    "project_team": project_team,
                    "qa_users": qas,
                    "project_file": project_file_url,
                    "created_date": proj["created_date"],  # dd/mm/yyyy hh:mm:ss
                    "updated_date": proj["updated_date"],  # dd/mm/yyyy hh:mm:ss
                }
            )

        return api_response(200, "Project list fetched successfully", result)
    except Exception as e:
        return api_response(500, f"Failed to fetch projects: {str(e)}")
    finally:
        cursor.close()
        conn.close()
