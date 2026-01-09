from flask import Blueprint, request
from config import get_db_connection, UPLOAD_FOLDER, UPLOAD_SUBDIRS
from utils.response import api_response
from utils.task_file_utils import save_base64_tracker_file
from datetime import datetime
import os

tracker_bp = Blueprint("tracker", __name__)

# Helper function for target calculation
def calculate_targets(base_target, user_tenure):
    user_tenure = float(user_tenure)
    base_target = float(base_target)
    actual_target = base_target * 1
    tenure_target = round(base_target * user_tenure, 2)
    return actual_target, tenure_target


# ------------------------
# ADD TRACKER
# ------------------------
@tracker_bp.route("/add", methods=["POST"])
def add_tracker():
    data = request.get_json()
    required_fields = ["project_id", "task_id", "user_id", "production"]

    for field in required_fields:
        if field not in data:
            return api_response(400, f"{field} is required")

    project_id = data["project_id"]
    task_id = data["task_id"]
    user_id = data["user_id"]
    production = float(data["production"])
    tenure_target = float(data["tenure_target"])
    task_file_base64 = data.get("task_file")
    task_file = None
    is_active = 1

    if task_file_base64:
        task_file = save_base64_tracker_file(task_file_base64)

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    try:
        # Fetch user tenure
        cursor.execute("SELECT task_target FROM task WHERE task_id=%s", (task_id,))
        user = cursor.fetchone()
        if not user:
            return api_response(404, "Task not found")

        actual_target = user["task_target"]
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        cursor.execute("""
            INSERT INTO task_work_tracker
            (project_id, task_id, user_id, production, actual_target, tenure_target, task_file, task_file_base64, is_active, date_time)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """, (project_id, task_id, user_id, production, actual_target, tenure_target, task_file, task_file_base64, is_active, now))

        conn.commit()
        tracker_id = cursor.lastrowid
        return api_response(201, "Tracker added successfully", {"tracker_id": tracker_id})

    except Exception as e:
        conn.rollback()
        return api_response(500, f"Failed to add tracker: {str(e)}")
    finally:
        cursor.close()
        conn.close()


# ------------------------
# UPDATE TRACKER
# ------------------------
@tracker_bp.route("/update", methods=["POST"])
def update_tracker():
    data = request.get_json()
    tracker_id = data.get("tracker_id")
    if not tracker_id:
        return api_response(400, "tracker_id is required")

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    try:
        cursor.execute("SELECT * FROM task_work_tracker WHERE tracker_id=%s", (tracker_id,))
        tracker = cursor.fetchone()
        if not tracker:
            return api_response(404, "Tracker not found")
        print(tracker)
        new_user_id = tracker["user_id"]
        print(new_user_id)
        cursor.execute("SELECT user_tenure FROM tfs_user WHERE user_id=%s", (new_user_id,))
        user = cursor.fetchone()
        if not user:
            return api_response(404, "User not found")

        production = float(data.get("production", tracker["production"]))
        base_target = float(data.get("base_target", tracker["actual_target"]))

        task_file_base64 = data.get("task_file_base64")
        task_file = tracker["task_file"]
        if task_file_base64:
            task_file = save_base64_tracker_file(task_file_base64)

        actual_target, tenure_target = calculate_targets(base_target, user["user_tenure"])
        updated_date = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        cursor.execute("""
            UPDATE task_work_tracker
            SET user_id=%s, production=%s, actual_target=%s, tenure_target=%s, task_file=%s, task_file_base64=%s, updated_date=%s
            WHERE tracker_id=%s
        """, (new_user_id, production, actual_target, tenure_target, task_file, task_file_base64, updated_date, tracker_id))

        conn.commit()
        return api_response(200, "Tracker updated successfully")

    except Exception as e:
        conn.rollback()
        return api_response(500, f"Failed to update tracker: {str(e)}")
    finally:
        cursor.close()
        conn.close()


# ------------------------
# VIEW TRACKERS
# ------------------------
@tracker_bp.route("/view", methods=["POST"])
def view_trackers():
    """
    Fully dynamic tracker view.
    Backend applies ONLY the filters provided in request.
    Any combination of filters is supported.
    """
    data = request.get_json() or {}

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    try:
        query = "SELECT * FROM task_work_tracker WHERE is_active != 0"
        params = []

        # 🔹 Dynamic filters
        if data.get("user_id"):
            query += " AND user_id=%s"
            params.append(data["user_id"])

        if data.get("project_id"):
            query += " AND project_id=%s"
            params.append(data["project_id"])

        if data.get("task_id"):
            query += " AND task_id=%s"
            params.append(data["task_id"])

        if data.get("date_from"):
            query += " AND date_time >= %s"
            params.append(data["date_from"])

        if data.get("date_to"):
            query += " AND date_time <= %s"
            params.append(data["date_to"])

        # Optional active filter (recommended)
        if data.get("is_active") is not None:
            query += " AND is_active=%s"
            params.append(data["is_active"])

        query += " ORDER BY date_time DESC"

        cursor.execute(query, tuple(params))
        trackers = cursor.fetchall()

        return api_response(
            200,
            "Trackers fetched successfully",
            {
                "count": len(trackers),
                "trackers": trackers
            }
        )

    except Exception as e:
        return api_response(500, f"Failed to fetch trackers: {str(e)}")

    finally:
        cursor.close()
        conn.close()

# ------------------------
# DELETE TRACKER (SOFT DELETE)
# ------------------------
@tracker_bp.route("/delete", methods=["POST"])
def delete_tracker():
    data = request.get_json() or {}

    tracker_id = data.get("tracker_id")
    if not tracker_id:
        return api_response(400, "tracker_id is required")

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    try:
        # Check tracker exists
        cursor.execute(
            "SELECT tracker_id FROM task_work_tracker WHERE tracker_id=%s",
            (tracker_id,)
        )
        tracker = cursor.fetchone()

        if not tracker:
            return api_response(404, "Tracker not found")

        # Soft delete
        cursor.execute("""
            UPDATE task_work_tracker
            SET is_active = 0
            WHERE tracker_id = %s
        """, (tracker_id,))

        conn.commit()
        return api_response(200, "Tracker deleted successfully")

    except Exception as e:
        conn.rollback()
        return api_response(500, f"Failed to delete tracker: {str(e)}")

    finally:
        cursor.close()
        conn.close()
