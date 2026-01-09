from flask import Blueprint, request
from utils.response import api_response
from config import get_db_connection
import json
from datetime import datetime

task_bp = Blueprint("task", __name__)

DATE_FORMAT = "%d/%m/%Y %H:%M:%S"

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
                is_active,
                created_date,
                updated_date
            )
            VALUES (%s,%s,%s,%s,%s,1,%s,%s)
        """, (
            data["project_id"],
            json.dumps(data["task_team_id"]),
            data["task_name"].strip(),
            data.get("task_description", "").strip(),
            data.get("task_target"),
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
    updatable_fields = ["project_id", "task_team_id", "task_name", "task_description", "task_target", "is_active"]

    for key in updatable_fields:
        if key in data:
            if key == "task_team_id":
                if not isinstance(data[key], list):
                    return api_response(400, "task_team_id must be a list")
                update_values[key] = json.dumps(data[key])
            else:
                update_values[key] = data[key]

    if not update_values:
        return api_response(400, "No valid fields provided for update")

    updated_str = datetime.now().strftime(DATE_FORMAT)
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    try:
        conn.start_transaction()
        cursor.execute("SELECT * FROM task WHERE task_id=%s AND is_active=1", (task_id,))
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
