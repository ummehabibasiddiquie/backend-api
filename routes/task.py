# routes/task.py

from flask import Blueprint, request
from utils.response import api_response
from config import get_db_connection, UPLOAD_FOLDER, UPLOAD_SUBDIRS, BASE_UPLOAD_URL
from utils.file_utils import save_uploaded_file  # ✅ multipart save
from datetime import datetime
import json
import os
import re

task_bp = Blueprint("task", __name__)

DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

# ----------------------------
# HELPERS
# ----------------------------

def safe_filename_part(value: str) -> str:
    if value is None:
        return "NA"
    s = str(value).strip()
    s = re.sub(r"\s+", "_", s)
    # keep alnum + underscore + dash only
    s = re.sub(r"[^A-Za-z0-9_\-]", "", s)
    return s or "NA"


def build_task_filename(project_id: str, task_name: str, original_filename: str) -> str:
    """
    You can change this format anytime. Keeping it stable & readable:
      TASK_<projectId>_<taskName>_<DD-MON-YYYY>_<HHAM/PM>.<ext>
    """
    if "." not in (original_filename or ""):
        raise ValueError("Uploaded file has no extension")

    ext = original_filename.rsplit(".", 1)[1].lower().strip()
    now = datetime.now()
    date_part = now.strftime("%d-%b-%Y")
    time_part = now.strftime("%I%p")  # 10AM

    return f"TASK_{safe_filename_part(project_id)}_{safe_filename_part(task_name)}_{date_part}_{time_part}.{ext}"


def get_task_file_dir() -> str:
    return os.path.abspath(os.path.join(UPLOAD_FOLDER, UPLOAD_SUBDIRS["TASK_FILES"]))


def safe_remove_task_file(filename: str) -> bool:
    """
    Deletes file from uploads/<TASK_FILES>/ if exists.
    Works even if DB stored URL/path (we basename it).
    """
    if not filename:
        return False

    filename = os.path.basename(str(filename))
    base_dir = get_task_file_dir()
    abs_path = os.path.abspath(os.path.join(base_dir, filename))

    # safety: ensure inside TASK_FILES folder
    if not abs_path.startswith(base_dir + os.sep):
        raise ValueError(f"Invalid file path: {abs_path}")

    if os.path.exists(abs_path):
        os.remove(abs_path)
        return True
    return False


def _get_form_json_list(form, key: str):
    """
    Accept JSON string for arrays in form-data: "[1,2,3]"
    """
    raw = form.get(key)
    if raw is None:
        return None  # means not provided
    raw = raw.strip()
    if raw == "":
        return []  # blank means empty list
    try:
        val = json.loads(raw)
        return val if isinstance(val, list) else []
    except Exception:
        return []


def get_public_upload_base():
    """Absolute base: https://tfshrms.cloud + /python/uploads"""
    return request.host_url.rstrip("/") + BASE_UPLOAD_URL


def task_file_url(filename: str):
    if not filename:
        return None
    base = get_public_upload_base().rstrip("/")
    sub = UPLOAD_SUBDIRS["TASK_FILES"].strip("/")
    return f"{base}/{sub}/{os.path.basename(filename)}"


def _truthy(val: str) -> bool:
    return str(val or "").strip().lower() in ("1", "true", "yes", "y")


# ---------------- CREATE TASK (multipart/form-data) ---------------- #
@task_bp.route("/add", methods=["POST"])
def add_task():
    # ✅ multipart/form-data
    form = request.form

    required_fields = ["project_id", "task_name"]
    for field in required_fields:
        if not form.get(field):
            return api_response(400, f"{field} is required")

    # task_team_id should be JSON list in form-data
    task_team_id = _get_form_json_list(form, "task_team_id")
    if task_team_id is None:
        return api_response(400, "task_team_id is required")
    if not isinstance(task_team_id, list):
        return api_response(400, "task_team_id must be a list")

    project_id = form.get("project_id")
    task_name = (form.get("task_name") or "").strip()

    task_description = form.get("task_description")
    task_description = (task_description or "").strip()

    task_target = form.get("task_target")  # keep as string; DB can cast if numeric
    important_columns = _get_form_json_list(form, "important_columns")
    # if not provided, allow NULL or []
    if important_columns is None:
        important_columns = []

    is_active = form.get("is_active")
    is_active = int(is_active) if str(is_active).strip().isdigit() else 1

    # ✅ file upload (key: task_file)
    uploaded = request.files.get("task_file")
    saved_filename = None

    if uploaded and uploaded.filename:
        try:
            custom_name = build_task_filename(project_id, task_name, uploaded.filename)
            saved_filename = save_uploaded_file(uploaded, UPLOAD_SUBDIRS["TASK_FILES"], custom_name)
        except Exception as e:
            return api_response(400, f"File handling failed: {str(e)}")

    now_str = datetime.now().strftime(DATE_FORMAT)

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    try:
        conn.start_transaction()
        cursor.execute(
            """
            INSERT INTO task (
                project_id,
                task_team_id,
                task_name,
                task_description,
                task_target,
                task_file,
                important_columns,
                is_active,
                created_date,
                updated_date
            )
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """,
            (
                project_id,
                json.dumps(task_team_id),
                task_name,
                task_description,
                task_target,
                saved_filename,
                json.dumps(important_columns),
                is_active,
                now_str,
                now_str,
            ),
        )
        conn.commit()
        return api_response(201, "Task added successfully", {
            "task_file": task_file_url(saved_filename)
        })

    except Exception as e:
        conn.rollback()
        # cleanup saved file if DB insert fails
        try:
            if saved_filename:
                safe_remove_task_file(saved_filename)
        except Exception:
            pass
        return api_response(500, f"Task creation failed: {str(e)}")

    finally:
        cursor.close()
        conn.close()


# ---------------- UPDATE TASK (multipart/form-data) ---------------- #
@task_bp.route("/update", methods=["POST","PUT"])
def update_task():
    form = request.form
    task_id = form.get("task_id")
    if not task_id:
        return api_response(400, "task_id is required")

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    new_file_saved = None
    old_file_to_delete = None

    try:
        conn.start_transaction()

        cursor.execute("SELECT task_id, task_file, project_id, task_name FROM task WHERE task_id=%s", (task_id,))
        existing = cursor.fetchone()
        if not existing:
            conn.rollback()
            return api_response(404, "Task not found")

        update_values = {}

        # Updatable fields from form-data
        if form.get("project_id") is not None:
            update_values["project_id"] = form.get("project_id")

        # task_team_id JSON list
        if form.get("task_team_id") is not None:
            team_list = _get_form_json_list(form, "task_team_id")
            if not isinstance(team_list, list):
                conn.rollback()
                return api_response(400, "task_team_id must be a list")
            update_values["task_team_id"] = json.dumps(team_list)

        if form.get("task_name") is not None:
            update_values["task_name"] = (form.get("task_name") or "").strip()

        if form.get("task_description") is not None:
            update_values["task_description"] = (form.get("task_description") or "").strip()

        if form.get("task_target") is not None:
            update_values["task_target"] = form.get("task_target")

        # important_columns JSON list
        if form.get("important_columns") is not None:
            imp = _get_form_json_list(form, "important_columns")
            if not isinstance(imp, list):
                imp = []
            update_values["important_columns"] = json.dumps(imp)

        if form.get("is_active") is not None:
            v = form.get("is_active")
            update_values["is_active"] = int(v) if str(v).strip().isdigit() else v

        # --- FILE LOGIC ---
        # Case 1: new file uploaded -> replace
        uploaded = request.files.get("task_file")
        if uploaded and uploaded.filename:
            old_file_to_delete = existing.get("task_file")

            use_project_id = update_values.get("project_id") or existing.get("project_id") or "PROJECT"
            use_task_name = update_values.get("task_name") or existing.get("task_name") or "TASK"

            custom_name = build_task_filename(use_project_id, use_task_name, uploaded.filename)
            new_name = save_uploaded_file(uploaded, UPLOAD_SUBDIRS["TASK_FILES"], custom_name)

            new_file_saved = new_name
            update_values["task_file"] = new_name

        # Case 2: explicit remove (even if no file chosen)
        # Send remove_task_file=1 from frontend when user clears files
        if _truthy(form.get("remove_task_file")):
            # clear DB field and delete old file
            old_file_to_delete = existing.get("task_file")
            update_values["task_file"] = None

        if not update_values:
            conn.rollback()
            return api_response(400, "No valid fields provided for update")

        updated_str = datetime.now().strftime(DATE_FORMAT)

        set_clause = ", ".join(f"{k}=%s" for k in update_values.keys())
        params = list(update_values.values()) + [updated_str, task_id]

        cursor.execute(
            f"UPDATE task SET {set_clause}, updated_date=%s WHERE task_id=%s",
            tuple(params),
        )

        conn.commit()

        # ✅ delete old file only after commit
        try:
            if old_file_to_delete and (update_values.get("task_file") is None or update_values.get("task_file") != old_file_to_delete):
                safe_remove_task_file(old_file_to_delete)
        except Exception as e:
            print("DELETE FAILED (task update):", e, "old_file=", old_file_to_delete)

        new_file_saved = None  # so we don't delete it in except

        return api_response(200, "Task updated successfully", {
            "task_file": task_file_url(update_values.get("task_file")) if "task_file" in update_values else None
        })

    except Exception as e:
        conn.rollback()
        # rollback cleanup: if new file saved but DB failed
        try:
            if new_file_saved:
                safe_remove_task_file(new_file_saved)
        except Exception:
            pass
        return api_response(500, f"Task update failed: {str(e)}")

    finally:
        cursor.close()
        conn.close()


# ---------------- DELETE TASK (soft delete + remove file) ---------------- #
@task_bp.route("/delete", methods=["PUT"])
def delete_task():
    data = request.get_json(silent=True) or {}
    task_id = data.get("task_id")
    if not task_id:
        return api_response(400, "task_id is required")

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    updated_str = datetime.now().strftime(DATE_FORMAT)

    try:
        conn.start_transaction()

        cursor.execute("SELECT task_file FROM task WHERE task_id=%s AND is_active=1", (task_id,))
        row = cursor.fetchone()
        if not row:
            conn.rollback()
            return api_response(404, "Task not found or already deleted")

        old_file = row.get("task_file")

        cursor.execute(
            "UPDATE task SET is_active=0, updated_date=%s WHERE task_id=%s",
            (updated_str, task_id),
        )
        conn.commit()

        # delete file after commit
        try:
            if old_file:
                safe_remove_task_file(old_file)
        except Exception as e:
            print("DELETE FAILED (task delete):", e, "file=", old_file)

        return api_response(200, "Task deleted successfully")

    except Exception as e:
        conn.rollback()
        return api_response(500, f"Task deletion failed: {str(e)}")

    finally:
        cursor.close()
        conn.close()


# ---------------- LIST TASKS (include absolute file URL) ---------------- #
@task_bp.route("/list", methods=["POST"])
def list_tasks():
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    try:
        cursor.execute(
            """
            SELECT task_id, project_id, task_team_id,
                   task_name, task_description, task_target,
                   task_file, important_columns,
                   is_active, created_date, updated_date
            FROM task
            WHERE is_active=1
            ORDER BY task_id DESC
            """
        )
        tasks = cursor.fetchall()

        result = []
        for t in tasks:
            task_team = json.loads(t.get("task_team_id") or "[]")
            important_cols = json.loads(t.get("important_columns") or "[]")

            result.append(
                {
                    "task_id": t["task_id"],
                    "project_id": t["project_id"],
                    "task_team": task_team,
                    "task_name": t["task_name"],
                    "task_description": t["task_description"],
                    "task_target": t["task_target"],
                    "important_columns": important_cols,
                    "task_file": task_file_url(t.get("task_file")),  # ✅ absolute
                    "created_date": t["created_date"],
                    "updated_date": t["updated_date"],
                }
            )

        return api_response(200, "Task list fetched successfully", result)

    except Exception as e:
        return api_response(500, f"Failed to fetch tasks: {str(e)}")

    finally:
        cursor.close()
        conn.close()
