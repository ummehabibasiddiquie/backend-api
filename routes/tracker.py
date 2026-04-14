from flask import Blueprint, request
from config import get_db_connection
from utils.response import api_response
from utils.api_log_utils import log_api_call
from utils.cloudinary_utils import upload_to_cloudinary, delete_from_cloudinary, FOLDER_TRACKER
from datetime import datetime, timedelta
import re
import os

tracker_bp = Blueprint("tracker", __name__)


# ------------------------
# HELPERS
# ------------------------

def calculate_targets(base_target, user_tenure):
    user_tenure = float(user_tenure)
    base_target = float(base_target)
    actual_target = round(base_target * 1, 2)
    tenure_target = round(base_target * user_tenure, 2)
    return actual_target, tenure_target


def normalize_month_year(month_year: str) -> str:
    month_year = (month_year or "").strip()
    if not month_year:
        return ""

    s = month_year.lower()
    month_abbr = s[:3].capitalize()
    year_part = s[3:]
    return f"{month_abbr}{year_part}"


def get_role_context(cursor, user_id: int) -> dict:
    cursor.execute(
        """
        SELECT
            u.role_id AS user_role_id,
            r.role_name AS user_role_name,
            (
                SELECT ur2.role_id
                FROM user_role ur2
                WHERE LOWER(TRIM(ur2.role_name)) = 'agent'
                LIMIT 1
            ) AS agent_role_id
        FROM tfs_user u
        JOIN user_role r ON r.role_id = u.role_id
        WHERE u.user_id=%s AND u.is_delete=1
        """,
        (int(user_id),),
    )
    row = cursor.fetchone() or {}
    return {
        "user_role_id": row.get("user_role_id"),
        "user_role_name": (row.get("user_role_name") or "").strip().lower(),
        "agent_role_id": row.get("agent_role_id"),
    }


def cleaned_csv_col(col_sql: str) -> str:
    return f"REPLACE(REPLACE(REPLACE({col_sql}, '[', ''), ']', ''), ' ', '')"


# ---------- NEW: filename helpers (tracker-specific, NOT in file_utils)

def _clean_part(value: str) -> str:
    value = (value or "").strip()
    value = re.sub(r"\s+", "_", value)
    value = re.sub(r"[^A-Za-z0-9_]", "", value)
    return value or "NA"


def build_tracker_filename(project_code: str, task_name: str, user_name: str, original_filename: str) -> str:
    """
    Keep your exact format:
    projectcode_taskname_username_date_time
    time format: hours with AM/PM (as you had)
    """
    if "." not in (original_filename or ""):
        raise ValueError("Uploaded file has no extension")

    ext = original_filename.rsplit(".", 1)[1].lower().strip()
    now = datetime.now()
    date_part = now.strftime("%d-%b-%Y")   # 05-Feb-2026
    time_part = now.strftime("%I%p")       # 10AM (kept exactly)
    return (
        f"{_clean_part(project_code)}_"
        f"{_clean_part(task_name)}_"
        f"{_clean_part(user_name)}_"
        f"{date_part}_{time_part}.{ext}"
    )


def safe_delete_cloudinary_tracker(url_or_public_id: str) -> None:
    """
    Silently delete a tracker file from Cloudinary.
    Errors are logged but never surface to the caller.
    """
    if not url_or_public_id:
        return
    try:
        delete_from_cloudinary(url_or_public_id, resource_type="raw")
    except Exception as e:
        print(f"Cloudinary tracker delete failed: {e} | ref={url_or_public_id}")

# ------------------------
# ADD TRACKER  (multipart + custom filename)
# ------------------------
@tracker_bp.route("/add", methods=["POST"])
def add_tracker():
    now_str = None
    form = request.form

    required_fields = ["project_id", "task_id", "user_id", "production", "tenure_target"]
    for f in required_fields:
        if not form.get(f):
            return api_response(400, f"{f} is required")

    project_id = int(form["project_id"])
    task_id = int(form["task_id"])
    user_id = int(form["user_id"])
    production = float(form["production"])
    tenure_target = float(form["tenure_target"])
    shift = form.get("shift", "DAY").upper()
    now_str = form.get("date")
    print(now_str)

    billable_hours = production / tenure_target if tenure_target else 0

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    try:
        # --- validate task + get task_target
        cursor.execute("SELECT task_target, task_name FROM task WHERE task_id=%s", (task_id,))
        task_row = cursor.fetchone()
        if not task_row:
            return api_response(404, "Task not found")
        
        if shift not in ["DAY", "NIGHT"]:
            return api_response(400, "Shift must be DAY or NIGHT")

        actual_target = task_row["task_target"]
        actual_billable_hours = production / actual_target if actual_target else 0
        task_name = task_row.get("task_name") or "Task"

        # --- get project_code
        cursor.execute("SELECT project_code FROM project WHERE project_id=%s", (project_id,))
        proj_row = cursor.fetchone() or {}
        project_code = proj_row.get("project_code") or "PROJECT"

        # --- get user_name
        cursor.execute("SELECT user_name FROM tfs_user WHERE user_id=%s", (user_id,))
        usr_row = cursor.fetchone() or {}
        user_name = usr_row.get("user_name") or "USER"

        # ✅ file upload to Cloudinary
        tracker_file = None
        uploaded = request.files.get("tracker_file")
        if uploaded and uploaded.filename:
            try:
                custom_name = build_tracker_filename(project_code, task_name, user_name, uploaded.filename)
                # public_id includes extension (raw resource)
                cloudinary_url, _ = upload_to_cloudinary(
                    uploaded, FOLDER_TRACKER, display_name=custom_name, resource_type="raw"
                )
                print(f"Cloudinary upload successful: {cloudinary_url}")
                tracker_file = cloudinary_url
            except ValueError as e:
                return api_response(400, str(e))
            except Exception as e:
                return api_response(500, f"File upload failed: {str(e)}")

        # now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if now_str is None:
            print("Received date:", now_str)
            now = datetime.now()
            # If NIGHT shift and time is between 00:00–09:00
            if shift == "NIGHT" and now.hour < 9:
                adjusted_datetime = now - timedelta(days=1)
            else:
                adjusted_datetime = now
                
            now_str = adjusted_datetime.strftime("%Y-%m-%d %H:%M:%S")
        
        tracker_note = form.get("tracker_note")  # optional, can be null

        cursor.execute(
            """
            INSERT INTO task_work_tracker
            (project_id, task_id, user_id, production, actual_target, tenure_target, billable_hours, actual_billable_hours,
             tracker_file, tracker_note, shift, is_active, date_time, updated_date)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """,
            (
                project_id, task_id, user_id, production, actual_target, tenure_target,
                billable_hours, actual_billable_hours, tracker_file, tracker_note, shift, 1, now_str, now_str
            ),
        )
        conn.commit()
        tracker_id = cursor.lastrowid

        device_id = form.get("device_id")
        device_type = form.get("device_type")
        api_call_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        log_api_call("add_tracker", user_id, device_id, device_type, api_call_time)

        return api_response(201, "Tracker added successfully", {"tracker_id": tracker_id})

    except Exception as e:
        conn.rollback()
        return api_response(500, f"Failed to add tracker: {str(e)}")

    finally:
        cursor.close()
        conn.close()


# ------------------------
# UPDATE TRACKER (multipart + optional file replace + custom filename)
# ------------------------
@tracker_bp.route("/update", methods=["POST","PUT"])
def update_tracker():
    form = request.form
    tracker_id = form.get("tracker_id")
    if not tracker_id:
        return api_response(400, "tracker_id is required")

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    # for rollback safety if DB update fails after saving file
    new_file_saved = None

    try:
        cursor.execute("SELECT * FROM task_work_tracker WHERE tracker_id=%s", (tracker_id,))
        tracker = cursor.fetchone()
        if not tracker:
            return api_response(404, "Tracker not found")

        old_file = tracker.get("tracker_file")  # may be filename OR url/path

        # update numeric fields (optional)
        production = float(form.get("production", tracker["production"]))
        date_time = form.get("date_time", tracker["date_time"])
        project_id = form.get("project_id", tracker["project_id"])
        task_id = form.get("task_id", tracker["task_id"])
        print(date_time)

        # tenure + user_name
        cursor.execute("SELECT user_tenure, user_name FROM tfs_user WHERE user_id=%s", (tracker["user_id"],))
        user_row = cursor.fetchone()
        if not user_row:
            return api_response(404, "User not found")

        # compute targets only if base_target is explicitly provided
        # NOTE: The base_target field in API contains tenure_target value
        if "base_target" in form:
            tenure_target = float(form.get("base_target"))  # API sends tenure_target as base_target
            user_tenure = float(user_row["user_tenure"])
            base_target = tenure_target / user_tenure if user_tenure else 0
            actual_target = round(base_target, 2)
        else:
            # keep existing targets from tracker (convert to float)
            actual_target = float(tracker["actual_target"]) if tracker["actual_target"] else 0
            tenure_target = float(tracker["tenure_target"]) if tracker["tenure_target"] else 0

        actual_billable_hours = production / actual_target if actual_target else 0

        tracker_file = old_file
        uploaded = request.files.get("tracker_file")

        shift = form.get("shift", tracker.get("shift", "DAY")).upper()
        if shift not in ["DAY", "NIGHT"]:
            return api_response(400, "Shift must be DAY or NIGHT")

        # ✅ Replace file only if new file provided
        if uploaded and uploaded.filename:
            # project_code  
            cursor.execute("SELECT project_code FROM project WHERE project_id=%s", (tracker["project_id"],))
            proj = cursor.fetchone() or {}
            project_code = proj.get("project_code") or "PROJECT"

            # task_name
            cursor.execute("SELECT task_name FROM task WHERE task_id=%s", (tracker["task_id"],))
            trow = cursor.fetchone() or {}
            task_name = trow.get("task_name") or "TASK"

            user_name = user_row.get("user_name") or "USER"

            custom_filename = build_tracker_filename(project_code, task_name, user_name, uploaded.filename)

            # ✅ Upload new file to Cloudinary first
            cloudinary_url, _ = upload_to_cloudinary(
                uploaded, FOLDER_TRACKER, display_name=custom_filename, resource_type="raw"
            )
            new_file_saved = cloudinary_url

            # ✅ Delete old Cloudinary file (only if it differs)
            if old_file and old_file != cloudinary_url:
                safe_delete_cloudinary_tracker(old_file)

            tracker_file = cloudinary_url

        # updated_date = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        now = datetime.now()
        # if shift == "NIGHT" and now.hour < 6:
        #     adjusted_datetime = now - timedelta(days=1)
        # else:
        #     adjusted_datetime = now

        updated_date = now.strftime("%Y-%m-%d %H:%M:%S")
        # date_time = adjusted_datetime.strftime("%Y-%m-%d %H:%M:%S")
        
        tracker_note = form.get("tracker_note", tracker.get("tracker_note"))  # optional, keep existing if not provided

        cursor.execute(
            """
            UPDATE task_work_tracker
            SET production=%s,
                actual_target=%s,
                tenure_target=%s,
                billable_hours=(%s / NULLIF(%s, 0)),
                actual_billable_hours=%s,
                tracker_file=%s,
                tracker_note=%s,
                shift=%s,
                updated_date=%s,
                date_time=%s,
                project_id=%s,
                task_id=%s
            WHERE tracker_id=%s
            """,
            (
                production,
                actual_target,
                tenure_target,
                production,
                tenure_target,
                actual_billable_hours,
                tracker_file,
                tracker_note,
                shift,
                updated_date,
                date_time,
                project_id,
                task_id,
                tracker_id,
            ),
        )
        conn.commit()

        # if DB commit succeeded, clear rollback marker
        new_file_saved = None

        device_id = form.get("device_id")
        device_type = form.get("device_type")
        api_call_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        log_api_call("update_tracker", tracker["user_id"], device_id, device_type, api_call_time)

        return api_response(200, "Tracker updated successfully")

    except ValueError as e:
        conn.rollback()
        # rollback: if Cloudinary upload succeeded but DB failed, delete newly uploaded file
        if new_file_saved:
            safe_delete_cloudinary_tracker(new_file_saved)
        return api_response(400, str(e))

    except Exception as e:
        conn.rollback()
        if new_file_saved:
            safe_delete_cloudinary_tracker(new_file_saved)
        return api_response(500, f"Failed to update tracker: {str(e)}")

    finally:
        cursor.close()
        conn.close()


# ------------------------
# DELETE TRACKER
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
        cursor.execute(
            "SELECT tracker_id, user_id, tracker_file FROM task_work_tracker WHERE tracker_id=%s",
            (tracker_id,),
        )
        tracker = cursor.fetchone()
        if not tracker:
            return api_response(404, "Tracker not found")

        # ✅ soft delete DB
        cursor.execute(
            "UPDATE task_work_tracker SET is_active = 0 WHERE tracker_id = %s",
            (tracker_id,),
        )        
        
        # ✅ delete associated tracker_records (Node.js backend ingestion data)
        tracker_file = tracker.get("tracker_file")
        if tracker_file:
            cursor.execute(
                "DELETE FROM tracker_records WHERE file_path = %s",
                (tracker_file,)
            )
 
        conn.commit()
 
        # ✅ delete from Cloudinary
        safe_delete_cloudinary_tracker(tracker_file)

        device_id = data.get("device_id")
        device_type = data.get("device_type")
        api_call_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        log_api_call("delete_tracker", tracker["user_id"], device_id, device_type, api_call_time)

        return api_response(200, "Tracker deleted successfully")

    except Exception as e:
        conn.rollback()
        return api_response(500, f"Failed to delete tracker: {str(e)}")

    finally:
        cursor.close()
        conn.close()

# ------------------------
# VIEW TRACKERS (with totals) - NO MONTH/YEAR LOGIC
# ------------------------
@tracker_bp.route("/view", methods=["POST"])
def view_trackers():
    print("====== INSIDE /tracker/view ======")
    data = request.get_json() or {}

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    try:
        params = []

        logged_in_user_id = data.get("logged_in_user_id")
        if not logged_in_user_id:
            return api_response(400, "logged_in_user_id is required")

        ctx = get_role_context(cursor, int(logged_in_user_id))
        role_name = ctx["user_role_name"]

        # -----------------------------
        # Main Tracker Query
        # -----------------------------
        query = """
        SELECT 
            twt.*, u.user_id, u.user_id AS agent_id, u.user_name, u.user_email,
            (SELECT GROUP_CONCAT(DISTINCT am.user_id) 
             FROM tfs_user am 
             WHERE u.asst_manager_id = am.user_id 
                OR JSON_CONTAINS(u.asst_manager_id, JSON_ARRAY(am.user_id))
            ) AS assistant_manager_id,
            (SELECT GROUP_CONCAT(DISTINCT am.user_name) 
             FROM tfs_user am 
             WHERE u.asst_manager_id = am.user_id 
                OR JSON_CONTAINS(u.asst_manager_id, JSON_ARRAY(am.user_id))
            ) AS assistant_manager_name,
            (SELECT GROUP_CONCAT(DISTINCT am.user_email) 
             FROM tfs_user am 
             WHERE u.asst_manager_id = am.user_id 
                OR JSON_CONTAINS(u.asst_manager_id, JSON_ARRAY(am.user_id))
            ) AS assistant_manager_email,
            p.project_id, p.project_name, p.project_category_id, pc.afd_id,
            tk.task_name, tk.qc_percentage, t.team_name,
            (twt.production / NULLIF(twt.tenure_target, 0)) AS billable_hours
        FROM task_work_tracker twt
        LEFT JOIN tfs_user u ON u.user_id = twt.user_id
        LEFT JOIN project p ON p.project_id = twt.project_id
        LEFT JOIN task tk ON tk.task_id = twt.task_id
        LEFT JOIN project_category pc ON pc.project_category_id = p.project_category_id
        LEFT JOIN team t ON u.team_id = t.team_id
        WHERE twt.is_active != 0
        """

        # Dynamic filters
        if data.get("team_id"):
            query += " AND u.team_id=%s"
            params.append(data["team_id"])
        if data.get("user_id"):
            user_ids_filter = data["user_id"]

            # if single value convert to list
            if not isinstance(user_ids_filter, list):
                user_ids_filter = [user_ids_filter]

            placeholders = ",".join(["%s"] * len(user_ids_filter))
            query += f" AND twt.user_id IN ({placeholders})"

            params.extend(user_ids_filter)
        elif role_name not in ("admin", "super admin", "project manager"):
            manager_id_str = str(logged_in_user_id)
            manager_id_int = int(logged_in_user_id)
            query += """
                AND twt.user_id IN (
                    SELECT tu.user_id
                    FROM tfs_user tu
                    WHERE tu.is_delete = 1
                    AND (
                        tu.project_manager_id = %s 
                        OR tu.project_manager_id = %s
                        OR tu.asst_manager_id = %s 
                        OR tu.asst_manager_id = %s
                        OR tu.qa_id = %s 
                        OR tu.qa_id = %s
                        OR tu.user_id = %s
                        OR (JSON_VALID(tu.project_manager_id) AND JSON_CONTAINS(tu.project_manager_id, JSON_ARRAY(%s)))
                        OR (JSON_VALID(tu.project_manager_id) AND JSON_CONTAINS(tu.project_manager_id, JSON_ARRAY(CAST(%s AS UNSIGNED))))
                        OR (JSON_VALID(tu.asst_manager_id) AND JSON_CONTAINS(tu.asst_manager_id, JSON_ARRAY(%s)))
                        OR (JSON_VALID(tu.asst_manager_id) AND JSON_CONTAINS(tu.asst_manager_id, JSON_ARRAY(CAST(%s AS UNSIGNED))))
                        OR (JSON_VALID(tu.qa_id) AND JSON_CONTAINS(tu.qa_id, JSON_ARRAY(%s)))
                        OR (JSON_VALID(tu.qa_id) AND JSON_CONTAINS(tu.qa_id, JSON_ARRAY(CAST(%s AS UNSIGNED))))
                    )
                )
            """
            params.extend([
                manager_id_str, manager_id_int,  # project_manager_id
                manager_id_str, manager_id_int,  # asst_manager_id
                manager_id_str, manager_id_int,  # qa_id
                manager_id_int,  # user_id
                manager_id_str, manager_id_str,  # project_manager_id JSON
                manager_id_str, manager_id_str,  # asst_manager_id JSON
                manager_id_str, manager_id_str   # qa_id JSON
            ])
        if data.get("project_id"):
            query += " AND twt.project_id=%s"
            params.append(data["project_id"])
        if data.get("task_id"):
            query += " AND twt.task_id=%s"
            params.append(data["task_id"])
        if data.get("shift"):
            query += " AND twt.shift=%s"
            params.append(data["shift"].upper())
        if data.get("date_from"):
            df = data["date_from"]
            if len(df) == 10: df += " 00:00:00"
            query += " AND CAST(twt.date_time AS DATETIME) >= %s"
            params.append(df)   
        if data.get("date_to"):
            dt_ = data["date_to"]
            if len(dt_) == 10: dt_ += " 23:59:59"
            query += " AND CAST(twt.date_time AS DATETIME) <= %s"
            params.append(dt_)
        if data.get("is_active") is not None:
            query += " AND twt.is_active=%s"
            params.append(data["is_active"])
        if data.get("qc_pending") is not None:
            query += " AND twt.qc_status = %s"
            params.append(data["qc_pending"])

            # ensure tracker file exists
            query += " AND twt.tracker_file IS NOT NULL AND twt.tracker_file != ''"

        query += " ORDER BY CAST(twt.date_time AS DATETIME) DESC"
        cursor.execute(query, tuple(params))
        trackers = cursor.fetchall()

        # Normalize tracker_file
        for t in trackers:
            file_path = t.get("tracker_file")
            t["agent_id"] = t.get("user_id")

            if not file_path:
                t["tracker_file"] = None
                continue

            # If already cloudinary URL, keep as is
            if file_path.startswith("http"):
                t["tracker_file"] = file_path

            # If mistakenly prefixed with python path
            elif "https://" in file_path:
                t["tracker_file"] = file_path[file_path.index("https://"):]

            else:
                t["tracker_file"] = file_path

        # -----------------------------
        # Totals
        # -----------------------------
        # Total assigned hours from temp_qc
        assigned_query = "SELECT COALESCE(SUM(assigned_hours),0) AS total_assigned FROM temp_qc WHERE 1=1"
        assigned_params = []

        if trackers:
            user_ids = [t["user_id"] for t in trackers if t.get("user_id")]
            in_ph = ",".join(["%s"]*len(user_ids))
            assigned_query += f" AND user_id IN ({in_ph})"
            assigned_params.extend(user_ids)

        if data.get("date_from") and data.get("date_to"):
            assigned_query += " AND DATE(date) BETWEEN %s AND %s"
            assigned_params.extend([data["date_from"], data["date_to"]])

        cursor.execute(assigned_query, tuple(assigned_params))
        total_assigned_hours = float((cursor.fetchone() or {}).get("total_assigned") or 0)

        
        totals = {
            "total_tenure_target": round(sum(float(t.get("tenure_target") or 0) for t in trackers), 2),
            "total_billable_hours": round(sum(float(t.get("billable_hours") or 0) for t in trackers), 2),
            "total_production": round(sum(float(t.get("production") or 0) for t in trackers), 2),
            "total_assigned_hours": round(total_assigned_hours, 2),
            "total_active_agents": len(set(t["user_id"] for t in trackers if t.get("user_id")))
        }

        # -----------------------------
        # Log API call
        # -----------------------------
        log_api_call("view_trackers", logged_in_user_id, data.get("device_id"), data.get("device_type"), datetime.now().strftime("%Y-%m-%d %H:%M:%S"))

        return api_response(
            200,
            "Trackers fetched successfully",
            {
                "count": len(trackers),
                "trackers": trackers,
                "totals": totals
            }
        )

    except Exception as e:
        return api_response(500, f"Failed to fetch trackers: {str(e)}")

    finally:
        cursor.close()
        conn.close()


def normalize_month_year(val):
    """
    Accepts: Jan2026 / jan2026 / JAN2026
    Returns: Jan2026
    """
    if not val:
        return None
    s = str(val).strip()
    if not s:
        return None
    try:
        dt = datetime.strptime(s.title(), "%b%Y")
        return dt.strftime("%b%Y")
    except Exception:
        return None


def cleaned_csv_col(col_name: str) -> str:
    """
    For columns that store CSV-like ids e.g. "[111, 113]"
    Makes it "111,113" so FIND_IN_SET works.
    """
    return f"REPLACE(REPLACE(REPLACE({col_name}, '[', ''), ']', ''), ' ', '')"


@tracker_bp.route("/view_daily", methods=["POST"])
def view_daily_trackers():
    data = request.get_json() or {}

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    # temp_qc date column is TEXT storing 'YYYY-MM-DD'
    QC_DATE_COL = "date"

    try:
        params = []

        logged_in_user_id = data.get("logged_in_user_id")
        if not logged_in_user_id:
            return api_response(400, "logged_in_user_id is required")

        # ---------- Smart Month Detection ----------
        month_year = None

        # 1️⃣ If date filter exists → derive month from date_to OR date_from
        if data.get("date_from") or data.get("date_to"):
            try:
                ref_date = data.get("date_to") or data.get("date_from")
                ref_date = str(ref_date)[:10]  # ensure YYYY-MM-DD
                dt_obj = datetime.strptime(ref_date, "%Y-%m-%d")
                month_year = dt_obj.strftime("%b%Y")
            except Exception:
                month_year = None

        # 2️⃣ Else use explicit month_year
        if not month_year:
            month_year = normalize_month_year(data.get("month_year"))

        # 3️⃣ Else fallback to current month
        if not month_year:
            cursor.execute("SELECT DATE_FORMAT(CURDATE(), '%b%Y') AS m")
            month_year = normalize_month_year((cursor.fetchone() or {}).get("m") or "")


        # -------- Role check
        cursor.execute(
            """
            SELECT LOWER(TRIM(r.role_name)) AS role_name
            FROM tfs_user u
            JOIN user_role r ON r.role_id = u.role_id
            WHERE u.user_id=%s
            LIMIT 1
            """,
            (int(logged_in_user_id),),
        )
        role_name = ((cursor.fetchone() or {}).get("role_name") or "").lower()

        # -------- WHERE (same filters as /view)
        where = "WHERE twt.is_active != 0"

        # Month filter
        try:
            dt = datetime.strptime(month_year, "%b%Y")
            where += " AND YEAR(CAST(twt.date_time AS DATETIME))=%s AND MONTH(CAST(twt.date_time AS DATETIME))=%s"
            params.extend([dt.year, dt.month])
        except Exception:
            pass

        # Team filter
        if data.get("team_id"):
            where += " AND u.team_id=%s"
            params.append(data["team_id"])

        # Project/task filters
        if data.get("project_id"):
            where += " AND twt.project_id=%s"
            params.append(data["project_id"])

        if data.get("task_id"):
            where += " AND twt.task_id=%s"
            params.append(data["task_id"])

        if data.get("shift"):
            where += " AND twt.shift = %s"
            params.append(data["shift"].upper())

        # Date range filters
        if data.get("date_from"):
            date_from = str(data["date_from"])
            if len(date_from) == 10:
                date_from += " 00:00:00"
            where += " AND CAST(twt.date_time AS DATETIME) >= %s"
            params.append(date_from)

        if data.get("date_to"):
            date_to = str(data["date_to"])
            if len(date_to) == 10:
                date_to += " 23:59:59"
            where += " AND CAST(twt.date_time AS DATETIME) <= %s"
            params.append(date_to)

        if data.get("is_active") is not None:
            where += " AND twt.is_active=%s"
            params.append(data["is_active"])

        # User filter OR restriction (manager logic)
        if data.get("user_id"):
            where += " AND twt.user_id=%s"
            params.append(data["user_id"])
        else:
            if "admin" not in role_name and "project manager" not in role_name:
                manager_id_str = str(logged_in_user_id)
                manager_id_int = int(logged_in_user_id)
                where += f"""
                    AND twt.user_id IN (
                        SELECT tu.user_id
                        FROM tfs_user tu
                        WHERE tu.is_delete = 1
                          AND (
                                tu.project_manager_id = %s
                                OR tu.project_manager_id = %s
                                OR tu.asst_manager_id = %s
                                OR tu.asst_manager_id = %s
                                OR tu.qa_id = %s
                                OR tu.qa_id = %s
                                OR tu.user_id = %s
                                OR (JSON_VALID(tu.project_manager_id) AND JSON_CONTAINS(tu.project_manager_id, JSON_ARRAY(%s)))
                                OR (JSON_VALID(tu.project_manager_id) AND JSON_CONTAINS(tu.project_manager_id, JSON_ARRAY(CAST(%s AS UNSIGNED))))
                                OR (JSON_VALID(tu.asst_manager_id) AND JSON_CONTAINS(tu.asst_manager_id, JSON_ARRAY(%s)))
                                OR (JSON_VALID(tu.asst_manager_id) AND JSON_CONTAINS(tu.asst_manager_id, JSON_ARRAY(CAST(%s AS UNSIGNED))))
                                OR (JSON_VALID(tu.qa_id) AND JSON_CONTAINS(tu.qa_id, JSON_ARRAY(%s)))
                                OR (JSON_VALID(tu.qa_id) AND JSON_CONTAINS(tu.qa_id, JSON_ARRAY(CAST(%s AS UNSIGNED))))
                          )
                    )
                """
                params.extend([
                    manager_id_str, manager_id_int,
                    manager_id_str, manager_id_int,
                    manager_id_str, manager_id_int,
                    manager_id_int,
                    manager_id_str, manager_id_str,
                    manager_id_str, manager_id_str,
                    manager_id_str, manager_id_str
                ])

        # -------- Daily aggregation + cumulative + daily required
        query = f"""
            WITH daily AS (
                SELECT DISTINCT
                    twt.user_id,
                    twt.shift,
                    DATE(CAST(twt.date_time AS DATETIME)) AS work_date,
                    COALESCE(SUM(twt.billable_hours), 0) AS total_billable_hours_day,
                    COUNT(*) AS trackers_count_day
                FROM task_work_tracker twt
                LEFT JOIN tfs_user u ON u.user_id = twt.user_id
                {where}
                GROUP BY twt.user_id, twt.shift, DATE(CAST(twt.date_time AS DATETIME))
            ),
            daily_with_cum AS (
                SELECT
                    d.*,
                    SUM(d.total_billable_hours_day)
                        OVER (PARTITION BY d.user_id ORDER BY d.work_date)
                        AS cumulative_billable_hours_till_day,
                    -- Conditional working days logic based on date
                    CASE 
                        -- Before April 2025: Use task_work_tracker count (old logic)
                        WHEN d.work_date < '2025-04-01' THEN
                            COUNT(*) OVER (PARTITION BY d.user_id ORDER BY d.work_date)
                        -- April 2025 onwards: Use roster-based calculation (new logic)
                        ELSE
                            DATEDIFF(
                                d.work_date,
                                (
                                    SELECT MIN(work_date) 
                                    FROM daily d2 
                                    WHERE d2.user_id = d.user_id 
                                    AND d2.work_date >= (
                                        SELECT DATE(CONCAT('01-', %s))
                                    )
                                )
                            ) + 1
                    END AS worked_days_till_day
                FROM daily d
            )
            SELECT
                dwc.user_id,
                dwc.shift,
                u.user_name,

                -- ✅ team info in response
                t.team_id,
                t.team_name,

                -- ✅ assistant manager info (GROUP_CONCAT to prevent duplicates)
                (SELECT GROUP_CONCAT(DISTINCT am.user_id) 
                 FROM tfs_user am 
                 WHERE u.asst_manager_id = am.user_id 
                    OR JSON_CONTAINS(u.asst_manager_id, JSON_ARRAY(am.user_id))
                ) AS assistant_manager_id,
                (SELECT GROUP_CONCAT(DISTINCT am.user_name) 
                 FROM tfs_user am 
                 WHERE u.asst_manager_id = am.user_id 
                    OR JSON_CONTAINS(u.asst_manager_id, JSON_ARRAY(am.user_id))
                ) AS assistant_manager_name,

                dwc.work_date,

                ROUND(dwc.total_billable_hours_day, 4) AS total_billable_hours_day,
                dwc.trackers_count_day,

                ROUND(dwc.cumulative_billable_hours_till_day, 4)
                    AS cumulative_billable_hours_till_day,

                -- QC data from separate tables (temp_qc takes priority for historical data)
                COALESCE(tqc.qc_score, qr.qc_score) AS qc_score,
                tqc.assigned_hours AS assigned_hours,

                r.roster_id,
                COALESCE(CAST(r.final_target AS DECIMAL(10,2)), 0) AS monthly_target,
                COALESCE(r.extra_assigned_hours, 0) AS extra_assigned_hours,
                (
                  COALESCE(CAST(r.final_target AS DECIMAL(10,2)), 0)
                  + COALESCE(r.extra_assigned_hours, 0)
                ) AS monthly_total_target,

                CAST(r.working_days AS SIGNED) AS working_days,

                GREATEST(
                    COALESCE(CAST(r.working_days AS SIGNED), 0)
                    - COALESCE(dwc.worked_days_till_day, 0),
                    0
                ) AS pending_days_after_this_day,

                CASE
                  WHEN r.roster_id IS NULL THEN NULL
                  WHEN GREATEST(
                        COALESCE(CAST(r.working_days AS SIGNED), 0)
                        - COALESCE(dwc.worked_days_till_day, 0),
                        0
                      ) = 0 THEN NULL
                  ELSE
                    (
                      (
                        COALESCE(CAST(r.final_target AS DECIMAL(10,2)), 0)
                        + COALESCE(r.extra_assigned_hours, 0)
                      )
                      - COALESCE(dwc.cumulative_billable_hours_till_day, 0)
                    )
                    / NULLIF(
                        GREATEST(
                            COALESCE(CAST(r.working_days AS SIGNED), 0)
                            - COALESCE(dwc.worked_days_till_day, 0),
                            0
                        ),
                        0
                      )
                END AS daily_required_hours
            FROM daily_with_cum dwc
            JOIN tfs_user u ON u.user_id = dwc.user_id
            LEFT JOIN team t ON t.team_id = u.team_id

            LEFT JOIN (
                SELECT
                    agent_id,
                    DATE(date_of_file_submission) AS qc_date,
                    ROUND(AVG(qc_score), 2) AS qc_score
                FROM qc_records
                GROUP BY agent_id, DATE(date_of_file_submission)
            ) qr
            ON qr.agent_id = dwc.user_id
            AND qr.qc_date = dwc.work_date

            LEFT JOIN temp_qc tqc
              ON tqc.user_id = dwc.user_id
             AND tqc.date = DATE_FORMAT(dwc.work_date, '%Y-%m-%d')

            LEFT JOIN (
                -- Hybrid approach: Use rosters for April 2025 onwards, user_monthly_tracker for before April
                SELECT 
                    user_id,
                    final_target as monthly_target,
                    working_days,
                    extra_assigned_hours,
                    roster_id,
                    weekoff_days,
                    holiday_days,
                    base_target,
                    final_target,
                    month_year
                FROM rosters 
                WHERE month_year >= '202504'  -- April 2025 onwards
                UNION ALL
                SELECT 
                    user_id,
                    monthly_target,
                    working_days,
                    extra_assigned_hours,
                    NULL as roster_id,
                    NULL as weekoff_days,
                    NULL as holiday_days,
                    NULL as base_target,
                    monthly_target as final_target,
                    month_year
                FROM user_monthly_tracker
                WHERE month_year < '202504'   -- Before April 2025
            ) r
              ON r.user_id = dwc.user_id
             AND r.month_year = %s

            ORDER BY dwc.work_date DESC, u.user_name ASC
        """

        # Debug: Check if rosters data exists for this month
        debug_query = """
            SELECT user_id, roster_id, month_year, working_days, final_target
            FROM rosters 
            WHERE month_year = %s
            LIMIT 5
        """
        cursor.execute(debug_query, (month_year,))
        rosters_debug = cursor.fetchall()
        print(f"DEBUG: Rosters data for {month_year}: {len(rosters_debug)} records")
        if rosters_debug:
            print(f"DEBUG: Sample rosters: {[{'user_id': r['user_id'], 'roster_id': r['roster_id']} for r in rosters_debug]}")

        final_params = list(params) + [month_year, month_year]
        print(f"DEBUG: Tracker query params: {final_params}")
        print(f"DEBUG: Month year being queried: {month_year}")
        cursor.execute(query, tuple(final_params))
        rows = cursor.fetchall()
        print(f"DEBUG: Number of rows returned: {len(rows)}")
        if rows:
            print(f"DEBUG: User IDs returned: {[r.get('user_id') for r in rows[:5]]}")  # First 5 users

        # -------- month_summary
        user_ids = sorted({r.get("user_id") for r in rows if r.get("user_id") is not None})
        month_summary = []

        if user_ids:
            in_ph = ",".join(["%s"] * len(user_ids))
            team_id = data.get("team_id")  # may be None

            summary_query = f"""
                SELECT
                    u.user_id,
                    u.user_name,

                    -- ✅ team info
                    t.team_id,
                    t.team_name,

                    m.mon AS month_year,
                    r.roster_id,
                    COALESCE(CAST(r.final_target AS DECIMAL(10,2)), 0) AS monthly_target,
                    COALESCE(r.extra_assigned_hours, 0) AS extra_assigned_hours,
                    (
                      COALESCE(CAST(r.final_target AS DECIMAL(10,2)), 0)
                    ) AS monthly_total_target,
                    COALESCE((
                      SELECT SUM(twt3.production / NULLIF(twt3.tenure_target, 0))
                      FROM task_work_tracker twt3
                      WHERE twt3.user_id = u.user_id
                        AND twt3.is_active = 1
                        AND (YEAR(CAST(twt3.date_time AS DATETIME))*100 + MONTH(CAST(twt3.date_time AS DATETIME))) = m.yyyymm
                    ), 0) AS total_billable_hours_month,
                    CASE
                      WHEN r.roster_id IS NULL THEN NULL
                      ELSE GREATEST(
                             COALESCE(CAST(r.working_days AS SIGNED), 0)
                             - COALESCE((
                                 -- Conditional logic: use different calculations based on date
                                 CASE 
                                     -- Before April 2025: Use task_work_tracker count (old logic)
                                     WHEN m.cutoff < '2025-04-01' THEN
                                         (SELECT COUNT(DISTINCT DATE(CAST(twt2.date_time AS DATETIME)))
                                         FROM task_work_tracker twt2
                                         WHERE twt2.user_id = u.user_id
                                           AND twt2.is_active = 1
                                           AND (YEAR(CAST(twt2.date_time AS DATETIME))*100 + MONTH(CAST(twt2.date_time AS DATETIME))) = m.yyyymm
                                           AND DATE(CAST(twt2.date_time AS DATETIME)) <= m.cutoff)
                                     -- April 2025 onwards: Use roster-based calculation (new logic)
                                     ELSE
                                         DATEDIFF(m.cutoff, DATE(CONCAT('01-', m.mon))) + 1
                                 END
                             ), 0),
                             0
                           )
                    END AS pending_days,
                    CASE
                      WHEN r.roster_id IS NULL THEN NULL
                      WHEN GREATEST(
                             COALESCE(CAST(r.working_days AS SIGNED), 0)
                             - COALESCE((
                                 -- Conditional logic: use different calculations based on date
                                 CASE 
                                     -- Before April 2025: Use task_work_tracker count (old logic)
                                     WHEN m.cutoff < '2025-04-01' THEN
                                         (SELECT COUNT(DISTINCT DATE(CAST(twt2.date_time AS DATETIME)))
                                         FROM task_work_tracker twt2
                                         WHERE twt2.user_id = u.user_id
                                           AND twt2.is_active = 1
                                           AND (YEAR(CAST(twt2.date_time AS DATETIME))*100 + MONTH(CAST(twt2.date_time AS DATETIME))) = m.yyyymm
                                           AND DATE(CAST(twt2.date_time AS DATETIME)) <= m.cutoff)
                                     -- April 2025 onwards: Use roster-based calculation (new logic)
                                     ELSE
                                         DATEDIFF(m.cutoff, DATE(CONCAT('01-', m.mon))) + 1
                                 END
                             ), 0),
                             0
                           ) = 0 THEN NULL
                      ELSE
                        (
                          (
                            COALESCE(CAST(r.final_target AS DECIMAL(10,2)), 0)
                          )
                          - COALESCE((
                              SELECT SUM(twt3.production / NULLIF(twt3.tenure_target, 0))
                              FROM task_work_tracker twt3
                              WHERE twt3.user_id = u.user_id
                                AND twt3.is_active = 1
                                AND (YEAR(CAST(twt3.date_time AS DATETIME))*100 + MONTH(CAST(twt3.date_time AS DATETIME))) = m.yyyymm
                            ), 0)
                        )
                        / NULLIF(
                            GREATEST(
                              COALESCE(CAST(r.working_days AS SIGNED), 0)
                              - COALESCE((
                                  -- Conditional logic: use different calculations based on date
                                  CASE 
                                      -- Before April 2025: Use task_work_tracker count (old logic)
                                      WHEN m.cutoff < '2025-04-01' THEN
                                          (SELECT COUNT(DISTINCT DATE(CAST(twt2.date_time AS DATETIME)))
                                          FROM task_work_tracker twt2
                                          WHERE twt2.user_id = u.user_id
                                            AND twt2.is_active = 1
                                            AND (YEAR(CAST(twt2.date_time AS DATETIME))*100 + MONTH(CAST(twt2.date_time AS DATETIME))) = m.yyyymm
                                            AND DATE(CAST(twt2.date_time AS DATETIME)) <= m.cutoff)
                                      -- April 2025 onwards: Use roster-based calculation (new logic)
                                      ELSE
                                          DATEDIFF(m.cutoff, DATE(CONCAT('01-', m.mon))) + 1
                                  END
                              ), 0),
                              0
                            ),
                            0
                          )
                    END AS daily_required_hours
                FROM tfs_user u
                LEFT JOIN team t ON t.team_id = u.team_id
                CROSS JOIN (
                    SELECT
                      %s AS mon,
                      CAST(DATE_FORMAT(STR_TO_DATE(CONCAT('01-', %s), '%d-%b%Y'), '%Y%m') AS UNSIGNED) AS yyyymm,
                      CASE
                        WHEN (YEAR(CURDATE())*100 + MONTH(CURDATE())) =
                             CAST(DATE_FORMAT(STR_TO_DATE(CONCAT('01-', %s), '%d-%b%Y'), '%Y%m') AS UNSIGNED)
                        THEN CURDATE()
                        WHEN (YEAR(CURDATE())*100 + MONTH(CURDATE())) >
                             CAST(DATE_FORMAT(STR_TO_DATE(CONCAT('01-', %s), '%d-%b%Y'), '%Y%m') AS UNSIGNED)
                        THEN LAST_DAY(STR_TO_DATE(CONCAT('01-', %s), '%d-%b%Y'))
                        ELSE DATE_SUB(STR_TO_DATE(CONCAT('01-', %s), '%d-%b%Y'), INTERVAL 1 DAY)
                      END AS cutoff
                ) m
                LEFT JOIN (
                    -- Hybrid approach: Use rosters for April 2025 onwards, user_monthly_tracker for before April
                    SELECT 
                        user_id,
                        final_target as monthly_target,
                        working_days,
                        extra_assigned_hours,
                        roster_id,
                        weekoff_days,
                        holiday_days,
                        base_target,
                        final_target,
                        month_year
                    FROM rosters 
                    WHERE month_year >= '202504'  -- April 2025 onwards
                    UNION ALL
                    SELECT 
                        user_id,
                        monthly_target,
                        working_days,
                        extra_assigned_hours,
                        NULL as roster_id,
                        NULL as weekoff_days,
                        NULL as holiday_days,
                        NULL as base_target,
                        monthly_target as final_target,
                        month_year
                    FROM user_monthly_tracker
                    WHERE month_year < '202504'   -- Before April 2025
                ) r
                  ON r.user_id = u.user_id
                 AND r.month_year = m.mon
                WHERE u.user_id IN ({in_ph})
                  -- ✅ team filter applied to summary too
                  AND (%s IS NULL OR u.team_id = %s)
            """

            summary_params = [month_year] * 6 + user_ids + [team_id, team_id]
            cursor.execute(summary_query, tuple(summary_params))
            month_summary = cursor.fetchall()

        # -------- Response KEYS SAME AS /view
        return api_response(
            200,
            "Trackers fetched successfully",
            {
                "count": len(rows),
                "month_year": month_year,
                "trackers": rows,        # daily aggregated rows
                "month_summary": month_summary  # ✅ now included + team info + team filter
            }
        )

    except Exception as e:
        return api_response(500, f"Failed to fetch daily trackers: {str(e)}")

    finally:
        cursor.close()
        conn.close()
