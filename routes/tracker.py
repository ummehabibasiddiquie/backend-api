from flask import Blueprint, request
from config import get_db_connection,UPLOAD_FOLDER, UPLOAD_SUBDIRS
from utils.response import api_response
from utils.file_utils import save_base64_file
from datetime import datetime
import os

tracker_bp = Blueprint("tracker", __name__)

UPLOAD_URL_PREFIX = "/uploads"

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
    tracker_file_base64 = data.get("tracker_file")
    tracker_file = None
    is_active = 1
    billable_hours = production / tenure_target

    if tracker_file_base64:
        tracker_file = save_base64_file(tracker_file_base64, UPLOAD_SUBDIRS['TRACKER_FILES'])

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
            (project_id, task_id, user_id, production, actual_target, tenure_target, billable_hours, tracker_file, tracker_file_base64, is_active, date_time, updated_date)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """, (project_id, task_id, user_id, production, actual_target, tenure_target, billable_hours, tracker_file, tracker_file_base64, is_active, now, now))

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
        # print(tracker)
        new_user_id = tracker["user_id"]
        # print(new_user_id)
        cursor.execute("SELECT user_tenure FROM tfs_user WHERE user_id=%s", (new_user_id,))
        user = cursor.fetchone()
        if not user:
            return api_response(404, "User not found")

        production = float(data.get("production", tracker["production"]))
        base_target = float(data.get("base_target", tracker["actual_target"]))

        tracker_file_base64 = data.get("tracker_file_base64")
        tracker_file = tracker["tracker_file"]
        if tracker_file_base64:
            tracker_file = save_base64_file(tracker_file_base64, UPLOAD_SUBDIRS['TRACKER_FILES'])

        actual_target, tenure_target = calculate_targets(base_target, user["user_tenure"])
        updated_date = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        cursor.execute("""
            UPDATE task_work_tracker
            SET user_id=%s, production=%s, actual_target=%s, tenure_target=%s, tracker_file=%s, tracker_file_base64=%s, updated_date=%s
            WHERE tracker_id=%s
        """, (new_user_id, production, actual_target, tenure_target, tracker_file, tracker_file_base64, updated_date, tracker_id))

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
    data = request.get_json() or {}
    # print(data)

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    try:
        query = "SELECT *, production / tenure_target as billable_hours FROM task_work_tracker WHERE is_active != 0"
        params = []

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

        if data.get("is_active") is not None:
            query += " AND is_active=%s"
            params.append(data["is_active"])

        query += " ORDER BY date_time DESC"
        # print(query)
        cursor.execute(query, tuple(params))
        trackers = cursor.fetchall()

        tracker_files_url = f"{UPLOAD_FOLDER}/{UPLOAD_SUBDIRS['TRACKER_FILES']}/"
        tracker_file_temp = ""
        for t in trackers:
            tracker_file_temp = t.get("tracker_file")
            if t.get("tracker_file"):
                t["tracker_file"] = tracker_files_url + tracker_file_temp
            else:
                t["tracker_file"] = None

        return api_response(
            200,
            "Trackers fetched successfully",
            {"count": len(trackers), "trackers": trackers}
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
