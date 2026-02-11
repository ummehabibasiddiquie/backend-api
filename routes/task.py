from flask import Blueprint, request
from utils.response import api_response
from config import get_db_connection, UPLOAD_FOLDER, UPLOAD_SUBDIRS
import json
from utils.file_utils import save_base64_file
from datetime import datetime

task_bp = Blueprint("task", __name__)

DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

# ---------------- CREATE TASK ---------------- #
@task_bp.route("/add", methods=["POST"])
def add_task():
    data = request.get_json()
    if not data:
        return api_response(400, "Request body is required")

    required_fields = ["project_id", "task_team_id", "task_name"]
    for field in required_fields:
        if field not in data:
            return api_response(400, f"{field} is required")

    if not isinstance(data["task_team_id"], list):
        return api_response(400, "task_team_id must be a list")

    device_id = data.get("device_id")
    device_type = data.get("device_type")
    
    task_file_base64 = data.get("task_file")
    task_file = None
    is_active=1
    important_columns = ["Email"] #static for testing purpose
    
    if task_file_base64 :
        task_file = save_base64_file(task_file_base64, UPLOAD_SUBDIRS['TASK_FILES'])

    now_str = datetime.now().strftime(DATE_FORMAT)

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    try:
        conn.start_transaction()
        cursor.execute("""
            INSERT INTO task (
                project_id,
                task_team_id,
                task_name,
                task_description,
                task_target,
                task_file,
                task_file_base64,
                important_columns,
                is_active,
                created_date,
                updated_date
            )
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """, (
            data["project_id"],
            json.dumps(data["task_team_id"]),
            data["task_name"].strip(),
            data.get("task_description", "").strip(),
            data.get("task_target"),
            task_file,
            task_file_base64,
            # json.dumps(data["important_columns"]),
            json.dumps(important_columns),
            is_active,
            now_str,
            now_str
        ))
        conn.commit()
        return api_response(201, "Task added successfully")
    except Exception as e:
        conn.rollback()
        return api_response(500, f"Task creation failed: {str(e)}")
    finally:
        cursor.close()
        conn.close()


# ---------------- UPDATE TASK ---------------- #
@task_bp.route("/update", methods=["PUT"])
def update_task():
    data = request.get_json()
    if not data or "task_id" not in data:
        return api_response(400, "task_id is required")

    task_id = data["task_id"]
    device_id = data.get("device_id")
    device_type = data.get("device_type")

    update_values = {}

    # ✅ Add new DB column in updatable list
    updatable_fields = [
        "project_id",
        "task_team_id",
        "task_name",
        "task_description",
        "task_target",
        "important_columns",
        "is_active",
        # NOTE: we will NOT directly accept task_file here as normal field
        # because task_file in request is base64
    ]

    for key in updatable_fields:
        if key in data:
            if key == "task_team_id":
                if not isinstance(data[key], list):
                    return api_response(400, "task_team_id must be a list")
                update_values[key] = json.dumps(data[key])

            elif key == "important_columns":
                # ✅ allow list, store as JSON string if list
                if isinstance(data[key], list):
                    update_values[key] = json.dumps(data[key])
                else:
                    update_values[key] = data[key]

            else:
                update_values[key] = data[key]

    # ✅ Handle task file base64 -> save file -> store both columns
    if data.get("task_file"):
        try:
            base64_str = data["task_file"]

            # save_base64_file() is your reusable function
            # it returns the filename like "uuid.pdf"
            filename = save_base64_file(base64_str, "TASK_FILES")

            update_values["task_file"] = filename
            update_values["task_file_base64"] = base64_str

        except Exception as e:
            return api_response(400, f"Invalid task_file: {str(e)}")

    if not update_values:
        return api_response(400, "No valid fields provided for update")

    updated_str = datetime.now().strftime(DATE_FORMAT)
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    try:
        conn.start_transaction()

        cursor.execute("SELECT task_id FROM task WHERE task_id=%s AND is_active=1", (task_id,))
        if not cursor.fetchone():
            conn.rollback()
            return api_response(404, "Task not found")

        set_clause = ", ".join(f"{k}=%s" for k in update_values)

        cursor.execute(f"""
            UPDATE task
            SET {set_clause}, updated_date=%s
            WHERE task_id=%s
        """, (*update_values.values(), updated_str, task_id))

        conn.commit()
        return api_response(200, "Task updated successfully")

    except Exception as e:
        conn.rollback()
        return api_response(500, f"Task update failed: {str(e)}")

    finally:
        cursor.close()
        conn.close()



# ---------------- SOFT DELETE TASK ---------------- #
@task_bp.route("/delete", methods=["PUT"])
def delete_task():
    data = request.get_json()
    if not data or "task_id" not in data:
        return api_response(400, "task_id is required")

    task_id = data["task_id"]
    device_id = data.get("device_id")
    device_type = data.get("device_type")

    updated_str = datetime.now().strftime(DATE_FORMAT)
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    try:
        conn.start_transaction()
        cursor.execute("SELECT task_id FROM task WHERE task_id=%s AND is_active=1", (task_id,))
        if not cursor.fetchone():
            conn.rollback()
            return api_response(404, "Task not found or already deleted")

        cursor.execute("UPDATE task SET is_active=0, updated_date=%s WHERE task_id=%s", (updated_str, task_id))
        conn.commit()
        return api_response(200, "Task deleted successfully")
    except Exception as e:
        conn.rollback()
        return api_response(500, f"Task deletion failed: {str(e)}")
    finally:
        cursor.close()
        conn.close()


# ---------------- LIST TASKS ---------------- #
@task_bp.route("/list", methods=["POST"])
def list_tasks():
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute("""
            SELECT task_id, project_id, task_team_id,
                   task_name, task_description, task_target,
                   is_active, created_date, updated_date
            FROM task
            WHERE is_active=1
            ORDER BY task_id DESC
        """)
        tasks = cursor.fetchall()
        result = []
        for t in tasks:
            task_team = json.loads(t["task_team_id"] or "[]")
            result.append({
                "task_id": t["task_id"],
                "project_id": t["project_id"],
                "task_team": task_team,
                "task_name": t["task_name"],
                "task_description": t["task_description"],
                "task_target": t["task_target"],
                "created_date": t["created_date"],
                "updated_date": t["updated_date"]
            })
        return api_response(200, "Task list fetched successfully", result)
    except Exception as e:
        return api_response(500, f"Failed to fetch tasks: {str(e)}")
    finally:
        cursor.close()
        conn.close()
