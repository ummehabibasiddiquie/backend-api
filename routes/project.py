# routes/project.py

from flask import Blueprint, request
from utils.response import api_response
from config import get_db_connection, UPLOAD_SUBDIRS, BASE_UPLOAD_URL, UPLOAD_FOLDER
from utils.file_utils import save_uploaded_file
import json
import os
from datetime import datetime

project_bp = Blueprint("project", __name__)

# ---------------- HELPERS ---------------- #

def safe_filename_part(value: str) -> str:
    if value is None:
        return "NA"
    s = str(value).strip().replace(" ", "_")
    for ch in ['/', '\\', ':', '*', '?', '"', '<', '>', '|']:
        s = s.replace(ch, "")
    return s or "NA"


def build_project_filename(project_name: str, project_code: str, original_filename: str, index: int, total: int) -> str:
    """
    Format: <Project>_<Code>_<DD-MON-YYYY>_1.ext (if multiple)
    """
    if "." not in (original_filename or ""):
        raise ValueError("Uploaded file has no extension")

    ext = original_filename.rsplit(".", 1)[1].lower().strip()
    date_part = datetime.now().strftime("%d-%b-%Y")  # DD-MON-YYYY

    name_part = safe_filename_part(project_name)
    code_part = safe_filename_part(project_code)

    suffix = f"_{index}" if total > 1 else ""
    return f"{name_part}_{code_part}_{date_part}{suffix}.{ext}"


def _get_form_json_list(form, key: str):
    raw = form.get(key)
    if not raw:
        return []
    try:
        val = json.loads(raw)
        return val if isinstance(val, list) else []
    except Exception:
        return []


def _get_uploaded_files():
    """
    Accept both:
      - files (recommended)
      - file  (common in Postman)
    IMPORTANT: in Postman you must use same key multiple times for multiple files.
    """
    files = request.files.getlist("files")
    if not files:
        files = request.files.getlist("file")
    return [f for f in files if f and f.filename]


def safe_remove_project_file(filename: str) -> bool:
    if not filename:
        return False

    filename = os.path.basename(str(filename))
    proj_dir = os.path.abspath(os.path.join(UPLOAD_FOLDER, UPLOAD_SUBDIRS["PROJECT_PPRT"]))
    abs_path = os.path.abspath(os.path.join(proj_dir, filename))

    if os.path.commonpath([proj_dir, abs_path]) != proj_dir:
        raise ValueError(f"Invalid file path: {abs_path}")

    if os.path.exists(abs_path):
        os.remove(abs_path)
        return True
    return False


def safe_remove_project_files(file_list):
    deleted = 0
    for f in file_list or []:
        try:
            if safe_remove_project_file(f):
                deleted += 1
        except Exception as e:
            print("DELETE FAILED:", e, "file=", f)
    return deleted


def parse_db_files(val):
    """
    DB can contain:
      - None
      - "[]"
      - '["a.pdf","b.xlsx"]'
      - "a.pdf" (legacy)
    """
    if not val:
        return []

    if isinstance(val, list):
        return [os.path.basename(str(x)) for x in val if x]

    if isinstance(val, str):
        s = val.strip()
        if not s:
            return []

        # legacy single filename
        if not (s.startswith("[") and s.endswith("]")):
            return [os.path.basename(s)]

        try:
            arr = json.loads(s)
            if isinstance(arr, list):
                return [os.path.basename(str(x)) for x in arr if x]
        except Exception:
            return []

    return []


def get_public_upload_base():
    # Absolute base: https://tfshrms.cloud + /python/uploads
    return request.host_url.rstrip("/") + BASE_UPLOAD_URL


def files_to_urls(files):
    base = get_public_upload_base().rstrip("/")
    sub = UPLOAD_SUBDIRS["PROJECT_PPRT"].strip("/")
    return [f"{base}/{sub}/{fname}" for fname in (files or [])]


# ---------------- CREATE PROJECT (multipart, multiple files) ---------------- #

@project_bp.route("/create", methods=["POST"])
def create_project():
    form = request.form

    required_fields = ["project_name", "project_code", "project_manager_id"]
    for f in required_fields:
        if not form.get(f):
            return api_response(400, f"{f} is required")

    project_name = form.get("project_name", "").strip()
    project_code = form.get("project_code", "").strip()
    project_description = form.get("project_description")
    if project_description == "null":
        project_description = None

    project_manager_id = form.get("project_manager_id")
    asst_project_manager_id = _get_form_json_list(form, "asst_project_manager_id")
    project_team_id = _get_form_json_list(form, "project_team_id")
    project_qa_id = _get_form_json_list(form, "project_qa_id")
    project_category_id = form.get("project_category_id")

    uploaded_files = _get_uploaded_files()

    saved_files = []
    try:
        total = len(uploaded_files)
        for idx, fs in enumerate(uploaded_files, start=1):
            custom_name = build_project_filename(project_name, project_code, fs.filename, idx, total)
            saved_name = save_uploaded_file(fs, UPLOAD_SUBDIRS["PROJECT_PPRT"], custom_name)
            saved_files.append(saved_name)
    except Exception as e:
        safe_remove_project_files(saved_files)
        return api_response(400, f"File handling failed: {str(e)}")

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

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
                project_pprt,
                project_category_id,
                created_date,
                updated_date,
                is_active
            )
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,1)
            """,
            (
                project_name,
                project_code,
                project_description,
                project_manager_id,
                json.dumps(asst_project_manager_id),
                json.dumps(project_team_id),
                json.dumps(project_qa_id),
                json.dumps(saved_files),   # ✅ store array*
                project_category_id,
                now_str,
                now_str,
            ),
        )
        conn.commit()

        # ✅ return absolute URLs
        return api_response(201, "Project created successfully", {
            "files": files_to_urls(saved_files)
        })

    except Exception as e:
        conn.rollback()
        safe_remove_project_files(saved_files)
        return api_response(500, f"Project creation failed: {str(e)}")
    finally:
        cursor.close()
        conn.close()


# ---------------- UPDATE PROJECT (multipart, multiple files) ---------------- #

@project_bp.route("/update", methods=["POST"])
def update_project():
    form = request.form
    project_id = form.get("project_id")
    if not project_id:
        return api_response(400, "project_id is required")

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    updated_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    new_saved_files = []
    old_files_to_delete = []

    try:
        conn.start_transaction()

        cursor.execute("SELECT * FROM project WHERE project_id=%s AND is_active=1", (project_id,))
        existing = cursor.fetchone()
        if not existing:
            conn.rollback()
            return api_response(404, "Project not found")

        update_values = {}

        # normal fields
        for key in ["project_name", "project_code", "project_description", "project_manager_id", "project_category_id"]:
            if form.get(key) is not None:
                v = form.get(key)
                if key in ["project_name", "project_code"] and v is not None:
                    v = v.strip()
                if key == "project_description" and v == "null":
                    v = None
                update_values[key] = v

        # json fields
        for key in ["asst_project_manager_id", "project_team_id", "project_qa_id"]:
            if form.get(key) is not None:
                update_values[key] = json.dumps(_get_form_json_list(form, key))

        # --- FILE LOGIC ---
        uploaded_files = _get_uploaded_files()

        # ✅ explicit clear signal from frontend/postman
        clear_files = (form.get("clear_files") or "").strip().lower() in ("1", "true", "yes")

        if clear_files:
            # delete old files + set DB to empty array
            old_files_to_delete = parse_db_files(existing.get("project_pprt"))
            update_values["project_pprt"] = json.dumps([])

        elif uploaded_files:
            # replace with new uploaded files
            old_files_to_delete = parse_db_files(existing.get("project_pprt"))

            use_project_name = update_values.get("project_name") or existing.get("project_name") or "PROJECT"
            use_project_code = update_values.get("project_code") or existing.get("project_code") or "CODE"

            total = len(uploaded_files)
            for idx, fs in enumerate(uploaded_files, start=1):
                custom_name = build_project_filename(use_project_name, use_project_code, fs.filename, idx, total)
                saved = save_uploaded_file(fs, UPLOAD_SUBDIRS["PROJECT_PPRT"], custom_name)
                new_saved_files.append(saved)

            update_values["project_pprt"] = json.dumps(new_saved_files)

        # if nothing to update
        if not update_values:
            conn.rollback()
            return api_response(400, "No valid fields provided for update")

        set_clause = ", ".join(f"{k}=%s" for k in update_values.keys())
        params = list(update_values.values()) + [updated_str, project_id]

        cursor.execute(
            f"UPDATE project SET {set_clause}, updated_date=%s WHERE project_id=%s",
            tuple(params),
        )

        conn.commit()

        # ✅ delete old files only AFTER commit
        if old_files_to_delete:
            safe_remove_project_files(old_files_to_delete)

        # response
        if "project_pprt" in update_values:
            final_files = parse_db_files(update_values["project_pprt"])
            return api_response(200, "Project updated successfully", {"files": files_to_urls(final_files)})

        return api_response(200, "Project updated successfully")

    except Exception as e:
        conn.rollback()
        # cleanup new saved files if update failed
        safe_remove_project_files(new_saved_files)
        return api_response(500, f"Project update failed: {str(e)}")

    finally:
        cursor.close()
        conn.close()


# ---------------- DELETE PROJECT (soft delete + remove all files) ---------------- #

@project_bp.route("/delete", methods=["PUT"])
def delete_project():
    data = request.get_json(silent=True) or {}
    project_id = data.get("project_id")
    if not project_id:
        return api_response(400, "project_id is required")

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    updated_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    try:
        conn.start_transaction()

        cursor.execute(
            "SELECT project_pprt FROM project WHERE project_id=%s AND is_active=1",
            (project_id,),
        )
        row = cursor.fetchone()
        if not row:
            conn.rollback()
            return api_response(404, "Project not found or already deleted")

        old_files = parse_db_files(row.get("project_pprt"))

        cursor.execute(
            "UPDATE project SET is_active=0, updated_date=%s WHERE project_id=%s",
            (updated_str, project_id),
        )
        conn.commit()

        # delete files after commit
        safe_remove_project_files(old_files)

        return api_response(200, "Project deleted successfully")

    except Exception as e:
        conn.rollback()
        return api_response(500, f"Project deletion failed: {str(e)}")
    finally:
        cursor.close()
        conn.close()


# ---------------- LIST PROJECTS (return multiple absolute URLs) ---------------- #

@project_bp.route("/list", methods=["POST"])
def list_projects():
    data = request.get_json(silent=True) or {}
    logged_in_user_id = data.get("logged_in_user_id")

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    try:
        role_name = None
        if logged_in_user_id:
            cursor.execute("""
                SELECT r.role_name FROM tfs_user u
                JOIN user_role r ON r.role_id = u.role_id
                WHERE u.user_id=%s AND u.is_active=1 AND u.is_delete=1
            """, (logged_in_user_id,))
            row = cursor.fetchone()
            if row:
                role_name = (row["role_name"] or "").strip().lower()

        # ✅ Role-based project filtering
        base_query = """
            SELECT project_id, project_name, project_code, project_description,
                   project_team_id, project_manager_id, asst_project_manager_id, project_qa_id,
                   project_pprt, created_date, updated_date
            FROM project
            WHERE is_active=1
        """
        params = []

        if role_name in ["admin", "super admin"] or not logged_in_user_id:
            # Admin sees all projects
            pass
        elif role_name in ["manager", "project manager"]:
            # Project manager sees projects they manage
            base_query += """
                AND (
                    TRIM(COALESCE(project_manager_id, '')) = %s
                    OR FIND_IN_SET(
                        %s,
                        REPLACE(REPLACE(REPLACE(REPLACE(COALESCE(project_manager_id,''), '[',''), ']',''), '"',''), ' ', '')
                    ) > 0
                )
            """
            params.extend([str(logged_in_user_id), str(logged_in_user_id)])
        elif role_name == "assistant manager":
            # Assistant manager sees projects where they are asst_project_manager
            base_query += """
                AND (
                    TRIM(COALESCE(asst_project_manager_id, '')) = %s
                    OR FIND_IN_SET(
                        %s,
                        REPLACE(REPLACE(REPLACE(REPLACE(COALESCE(asst_project_manager_id,''), '[',''), ']',''), '"',''), ' ', '')
                    ) > 0
                )
            """
            params.extend([str(logged_in_user_id), str(logged_in_user_id)])
        elif role_name == "qa":
            # QA sees projects where they are project_qa
            base_query += """
                AND (
                    TRIM(COALESCE(project_qa_id, '')) = %s
                    OR FIND_IN_SET(
                        %s,
                        REPLACE(REPLACE(REPLACE(REPLACE(COALESCE(project_qa_id,''), '[',''), ']',''), '"',''), ' ', '')
                    ) > 0
                )
            """
            params.extend([str(logged_in_user_id), str(logged_in_user_id)])
        elif role_name == "agent":
            # Agent sees projects where they are in project_team
            base_query += """
                AND (
                    TRIM(COALESCE(project_team_id, '')) = %s
                    OR FIND_IN_SET(
                        %s,
                        REPLACE(REPLACE(REPLACE(REPLACE(COALESCE(project_team_id,''), '[',''), ']',''), '"',''), ' ', '')
                    ) > 0
                )
            """
            params.extend([str(logged_in_user_id), str(logged_in_user_id)])

        base_query += " ORDER BY project_id DESC"

        cursor.execute(base_query, tuple(params))
        projects = cursor.fetchall()

        result = []
        for proj in projects:

            files = parse_db_files(proj.get("project_pprt"))
            project_files = files_to_urls(files)  # ✅ absolute array links

            result.append({
                "project_id": proj["project_id"],
                "project_name": proj["project_name"],
                "project_code": proj["project_code"],
                "project_description": proj["project_description"],
                "project_manager_id": proj["project_manager_id"],
                "project_team_id": json.loads(proj.get("project_team_id") or "[]"),
                "asst_project_manager_id": json.loads(proj.get("asst_project_manager_id") or "[]"),
                "project_qa_id": json.loads(proj.get("project_qa_id") or "[]"),
                "project_category": proj["project_category"],
                "project_files": project_files,
                "created_date": proj["created_date"],
                "updated_date": proj["updated_date"],
            })

        return api_response(200, "Project list fetched successfully", result)

    except Exception as e:
        return api_response(500, f"Failed to fetch projects: {str(e)}")
    finally:
        cursor.close()
        conn.close()