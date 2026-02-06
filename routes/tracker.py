from flask import Blueprint, request
from config import get_db_connection, BASE_UPLOAD_URL, UPLOAD_SUBDIRS, UPLOAD_FOLDER
from utils.response import api_response
from utils.file_utils import save_base64_file  # kept (not used now in update)
from utils.api_log_utils import log_api_call
from datetime import datetime
import re
import os

tracker_bp = Blueprint("tracker", __name__)

UPLOAD_URL_PREFIX = "/uploads"


# ------------------------
# HELPERS
# ------------------------

def calculate_targets(base_target, user_tenure):
    user_tenure = float(user_tenure)
    base_target = float(base_target)
    actual_target = base_target * 1
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
        WHERE u.user_id=%s AND u.is_active=1 AND u.is_delete=1
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


def get_tracker_file_path(filename: str) -> str:
    # physical path for tracker file
    return os.path.join(UPLOAD_FOLDER, UPLOAD_SUBDIRS["TRACKER_FILES"], filename)

def safe_remove_tracker_file(filename: str) -> bool:
    """
    Deletes file from tracker_files folder if exists.
    Returns True if deleted, False if not found / nothing deleted.
    """
    if not filename:
        return False

    file_path = get_tracker_file_path(filename)

    # Safety: ensure deletion stays inside tracker_files directory
    tracker_dir = os.path.abspath(os.path.join(UPLOAD_FOLDER, UPLOAD_SUBDIRS["TRACKER_FILES"]))
    abs_path = os.path.abspath(file_path)
    if not abs_path.startswith(tracker_dir + os.sep):
        # prevents path traversal like ../../something
        raise ValueError("Invalid file path")
    print("Deleting file at:", abs_path)
    if os.path.exists(abs_path):
        os.remove(abs_path)
        return True

    return False

def safe_remove_tracker_file(filename: str) -> bool:
    """
    Deletes file from uploads/<TRACKER_FILES>/ if exists.
    Works even if DB stored a URL/path (we basename it).
    """
    if not filename:
        return False

    # ✅ normalize in case DB stored URL/path
    filename = os.path.basename(str(filename))

    tracker_dir = os.path.abspath(os.path.join(UPLOAD_FOLDER, UPLOAD_SUBDIRS["TRACKER_FILES"]))
    abs_path = os.path.abspath(os.path.join(tracker_dir, filename))

    # ✅ safety: ensure deletion stays inside tracker_dir
    if not abs_path.startswith(tracker_dir + os.sep):
        raise ValueError(f"Invalid file path: {abs_path}")

    if os.path.exists(abs_path):
        os.remove(abs_path)
        return True

    return False

# ------------------------
# ADD TRACKER  (multipart + custom filename)
# ------------------------
@tracker_bp.route("/add", methods=["POST"])
def add_tracker():
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

    billable_hours = production / tenure_target if tenure_target else 0

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    try:
        # --- validate task + get task_target
        cursor.execute("SELECT task_target, task_name FROM task WHERE task_id=%s", (task_id,))
        task_row = cursor.fetchone()
        if not task_row:
            return api_response(404, "Task not found")

        actual_target = task_row["task_target"]
        task_name = task_row.get("task_name") or "Task"

        # --- get project_code
        cursor.execute("SELECT project_code FROM project WHERE project_id=%s", (project_id,))
        proj_row = cursor.fetchone() or {}
        project_code = proj_row.get("project_code") or "PROJECT"

        # --- get user_name
        cursor.execute("SELECT user_name FROM tfs_user WHERE user_id=%s", (user_id,))
        usr_row = cursor.fetchone() or {}
        user_name = usr_row.get("user_name") or "USER"

        # ✅ file save (multipart)
        tracker_file = None
        uploaded = request.files.get("tracker_file")
        if uploaded and uploaded.filename:
            try:
                from utils.file_utils import save_uploaded_file  # generic
                custom_name = build_tracker_filename(project_code, task_name, user_name, uploaded.filename)
                tracker_file = save_uploaded_file(uploaded, UPLOAD_SUBDIRS["TRACKER_FILES"], custom_name)
            except ValueError as e:
                return api_response(400, str(e))

        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        cursor.execute(
            """
            INSERT INTO task_work_tracker
            (project_id, task_id, user_id, production, actual_target, tenure_target, billable_hours,
             tracker_file, is_active, date_time, updated_date)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """,
            (
                project_id, task_id, user_id, production, actual_target, tenure_target,
                billable_hours, tracker_file, 1, now, now
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
@tracker_bp.route("/update", methods=["POST"])
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
        base_target = float(form.get("base_target", tracker["actual_target"]))

        # tenure + user_name
        cursor.execute("SELECT user_tenure, user_name FROM tfs_user WHERE user_id=%s", (tracker["user_id"],))
        user_row = cursor.fetchone()
        if not user_row:
            return api_response(404, "User not found")

        # compute targets (keep your existing calculate_targets)
        actual_target, tenure_target = calculate_targets(base_target, user_row["user_tenure"])

        tracker_file = old_file
        uploaded = request.files.get("tracker_file")

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

            from utils.file_utils import save_uploaded_file

            # ✅ Save new file first
            new_file = save_uploaded_file(uploaded, UPLOAD_SUBDIRS["TRACKER_FILES"], custom_filename)
            new_file_saved = new_file

            # ✅ Delete old file (only if old exists AND not same)
            try:
                if old_file:
                    old_file_norm = os.path.basename(str(old_file))
                else:
                    old_file_norm = None

                if old_file_norm and old_file_norm != new_file:
                    safe_remove_tracker_file(old_file_norm)
            except Exception as e:
                # DO NOT fail update if old deletion fails, but don't hide it
                print("DELETE FAILED (update):", str(e), " old_file=", old_file)

            tracker_file = new_file

        updated_date = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        cursor.execute(
            """
            UPDATE task_work_tracker
            SET production=%s,
                actual_target=%s,
                tenure_target=%s,
                billable_hours=(%s / NULLIF(%s, 0)),
                tracker_file=%s,
                updated_date=%s
            WHERE tracker_id=%s
            """,
            (
                production,
                actual_target,
                tenure_target,
                production,
                tenure_target,
                tracker_file,
                updated_date,
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
        # rollback: if file was saved but DB update failed, remove newly saved file
        if new_file_saved:
            try:
                safe_remove_tracker_file(new_file_saved)
            except Exception:
                pass
        return api_response(400, str(e))

    except Exception as e:
        conn.rollback()
        if new_file_saved:
            try:
                safe_remove_tracker_file(new_file_saved)
            except Exception:
                pass
        return api_response(500, f"Failed to update tracker: {str(e)}")

    finally:
        cursor.close()
        conn.close()


# ------------------------
# VIEW TRACKERS (UNCHANGED)
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
        conn.commit()

        # ✅ delete physical file
        try:
            f = tracker.get("tracker_file")
            if f:
                f = os.path.basename(str(f))
            if f:
                safe_remove_tracker_file(f)
        except Exception as e:
            print("DELETE FAILED (delete api):", str(e), " file=", tracker.get("tracker_file"))

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
# VIEW TRACKERS (your existing logic + month_year normalization + robust manager matching)
# ------------------------
@tracker_bp.route("/view", methods=["POST"])
def view_trackers():
    data = request.get_json() or {}

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    try:
        params = []

        logged_in_user_id = data.get("logged_in_user_id")
        if not logged_in_user_id:
            return api_response(400, "logged_in_user_id is required")

        # month_year default current month
        month_year = normalize_month_year(data.get("month_year"))
        if not month_year:
            cursor.execute("SELECT DATE_FORMAT(CURDATE(), '%b%Y') AS m")
            month_year = normalize_month_year((cursor.fetchone() or {}).get("m") or "")

        ctx = get_role_context(cursor, int(logged_in_user_id))
        role_name = ctx["user_role_name"]

        query = """
            SELECT 
                twt.*,
                u.user_name,
                p.project_name,
                tk.task_name,
                t.team_name,
                (twt.production / NULLIF(twt.tenure_target, 0)) AS billable_hours
            FROM task_work_tracker twt
            LEFT JOIN tfs_user u ON u.user_id = twt.user_id
            LEFT JOIN project p ON p.project_id = twt.project_id
            LEFT JOIN task tk ON tk.task_id = twt.task_id
            LEFT JOIN team t ON u.team_id = t.team_id
            WHERE twt.is_active != 0
        """

        # month filter
        try:
            dt = datetime.strptime(month_year, "%b%Y")
            query += " AND YEAR(CAST(twt.date_time AS DATETIME)) = %s AND MONTH(CAST(twt.date_time AS DATETIME)) = %s"
            params.extend([dt.year, dt.month])
        except Exception:
            pass

        if data.get("team_id"):
            query += " AND u.team_id=%s"
            params.append(data["team_id"])

        if data.get("user_id"):
            query += " AND twt.user_id=%s"
            params.append(data["user_id"])
        else:
            if role_name in ("admin", "super admin"):
                pass
            else:
                manager_id_str = str(logged_in_user_id)
                query += f"""
                    AND twt.user_id IN (
                        SELECT tu.user_id
                        FROM tfs_user tu
                        WHERE tu.is_active = 1
                          AND tu.is_delete = 1
                          AND (
                                tu.project_manager_id = %s
                                OR tu.asst_manager_id = %s
                                OR tu.qa_id = %s
                                OR tu.user_id = %s
                                OR FIND_IN_SET(%s, {cleaned_csv_col("tu.project_manager_id")}) > 0
                                OR FIND_IN_SET(%s, {cleaned_csv_col("tu.asst_manager_id")}) > 0
                                OR FIND_IN_SET(%s, {cleaned_csv_col("tu.qa_id")}) > 0
                          )
                    )
                """
                params.extend([manager_id_str] * 7)

        if data.get("project_id"):
            query += " AND twt.project_id=%s"
            params.append(data["project_id"])

        if data.get("task_id"):
            query += " AND twt.task_id=%s"
            params.append(data["task_id"])

        if data.get("date_from"):
            date_from = data["date_from"]
            if len(date_from) == 10:
                date_from += " 00:00:00"
            query += " AND CAST(twt.date_time AS DATETIME) >= %s"
            params.append(date_from)

        if data.get("date_to"):
            date_to = data["date_to"]
            if len(date_to) == 10:
                date_to += " 23:59:59"
            query += " AND CAST(twt.date_time AS DATETIME) <= %s"
            params.append(date_to)

        if data.get("is_active") is not None:
            query += " AND twt.is_active=%s"
            params.append(data["is_active"])

        query += " ORDER BY CAST(twt.date_time AS DATETIME) DESC"

        cursor.execute(query, tuple(params))
        trackers = cursor.fetchall()

        tracker_files_url = f"{BASE_UPLOAD_URL}/{UPLOAD_SUBDIRS['TRACKER_FILES']}/"
        for t in trackers:
            tracker_file_temp = t.get("tracker_file")
            t["tracker_file"] = (tracker_files_url + tracker_file_temp) if tracker_file_temp else None

        # Month-wise summary (your logic, but month_year is normalized now)
        user_ids = sorted({t.get("user_id") for t in trackers if t.get("user_id") is not None})
        month_summary = []

        if user_ids:
            in_ph = ",".join(["%s"] * len(user_ids))

            summary_query = f"""
                SELECT
                    u.user_id,
                    u.user_name,
                    m.mon AS month_year,
                    umt.user_monthly_tracker_id,
                    COALESCE(CAST(umt.monthly_target AS DECIMAL(10,2)), 0) AS monthly_target,
                    COALESCE(umt.extra_assigned_hours, 0) AS extra_assigned_hours,
                    (
                      COALESCE(CAST(umt.monthly_target AS DECIMAL(10,2)), 0)
                      + COALESCE(umt.extra_assigned_hours, 0)
                    ) AS monthly_total_target,
                    COALESCE((
                      SELECT SUM(twt3.production / NULLIF(twt3.tenure_target, 0))
                      FROM task_work_tracker twt3
                      WHERE twt3.user_id = u.user_id
                        AND twt3.is_active = 1
                        AND (YEAR(CAST(twt3.date_time AS DATETIME))*100 + MONTH(CAST(twt3.date_time AS DATETIME))) = m.yyyymm
                    ), 0) AS total_billable_hours_month,
                    CASE
                      WHEN umt.user_monthly_tracker_id IS NULL THEN NULL
                      ELSE GREATEST(
                             COALESCE(CAST(umt.working_days AS SIGNED), 0)
                             - COALESCE((
                                 SELECT COUNT(DISTINCT DATE(CAST(twt2.date_time AS DATETIME)))
                                 FROM task_work_tracker twt2
                                 WHERE twt2.user_id = u.user_id
                                   AND twt2.is_active = 1
                                   AND (YEAR(CAST(twt2.date_time AS DATETIME))*100 + MONTH(CAST(twt2.date_time AS DATETIME))) = m.yyyymm
                                   AND DATE(CAST(twt2.date_time AS DATETIME)) <= m.cutoff
                               ), 0),
                             0
                           )
                    END AS pending_days,
                    CASE
                      WHEN umt.user_monthly_tracker_id IS NULL THEN NULL
                      WHEN GREATEST(
                             COALESCE(CAST(umt.working_days AS SIGNED), 0)
                             - COALESCE((
                                 SELECT COUNT(DISTINCT DATE(CAST(twt2.date_time AS DATETIME)))
                                 FROM task_work_tracker twt2
                                 WHERE twt2.user_id = u.user_id
                                   AND twt2.is_active = 1
                                   AND (YEAR(CAST(twt2.date_time AS DATETIME))*100 + MONTH(CAST(twt2.date_time AS DATETIME))) = m.yyyymm
                                   AND DATE(CAST(twt2.date_time AS DATETIME)) <= m.cutoff
                               ), 0),
                             0
                           ) = 0 THEN NULL
                      ELSE
                        (
                          (
                            COALESCE(CAST(umt.monthly_target AS DECIMAL(10,2)), 0)
                            + COALESCE(umt.extra_assigned_hours, 0)
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
                              COALESCE(CAST(umt.working_days AS SIGNED), 0)
                              - COALESCE((
                                  SELECT COUNT(DISTINCT DATE(CAST(twt2.date_time AS DATETIME)))
                                  FROM task_work_tracker twt2
                                  WHERE twt2.user_id = u.user_id
                                    AND twt2.is_active = 1
                                    AND (YEAR(CAST(twt2.date_time AS DATETIME))*100 + MONTH(CAST(twt2.date_time AS DATETIME))) = m.yyyymm
                                    AND DATE(CAST(twt2.date_time AS DATETIME)) <= m.cutoff
                                ), 0),
                              0
                            ),
                            0
                          )
                    END AS daily_required_hours
                FROM tfs_user u
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
                LEFT JOIN user_monthly_tracker umt
                  ON umt.user_id = u.user_id
                 AND umt.is_active = 1
                 AND umt.month_year = m.mon
                WHERE u.user_id IN ({in_ph})
            """

            summary_params = [month_year] * 6 + user_ids
            cursor.execute(summary_query, tuple(summary_params))
            month_summary = cursor.fetchall()

            device_id = data.get("device_id")
            device_type = data.get("device_type")
            api_call_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            log_api_call("view_trackers", logged_in_user_id, device_id, device_type, api_call_time)

        return api_response(
            200,
            "Trackers fetched successfully",
            {
                "count": len(trackers),
                "month_year": month_year,
                "trackers": trackers,
                "month_summary": month_summary,
            },
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

    # ✅ temp_qc date column is now "date" (TEXT storing 'YYYY-MM-DD')
    QC_DATE_COL = "date"

    try:
        params = []

        logged_in_user_id = data.get("logged_in_user_id")
        if not logged_in_user_id:
            return api_response(400, "logged_in_user_id is required")

        # -------- Month (case-insensitive, same behavior as view)
        month_year = normalize_month_year(data.get("month_year"))
        if not month_year:
            cursor.execute("SELECT DATE_FORMAT(CURDATE(), '%b%Y') AS m")
            month_year = normalize_month_year((cursor.fetchone() or {}).get("m") or "")

        # -------- Role check (DO NOT depend on is_delete)
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

        where = "WHERE twt.is_active != 0"

        # -------- Month filter
        try:
            dt = datetime.strptime(month_year, "%b%Y")
            where += " AND YEAR(CAST(twt.date_time AS DATETIME))=%s AND MONTH(CAST(twt.date_time AS DATETIME))=%s"
            params.extend([dt.year, dt.month])
        except Exception:
            pass

        # -------- Same filters as /view
        if data.get("team_id"):
            where += " AND u.team_id=%s"
            params.append(data["team_id"])

        if data.get("project_id"):
            where += " AND twt.project_id=%s"
            params.append(data["project_id"])

        if data.get("task_id"):
            where += " AND twt.task_id=%s"
            params.append(data["task_id"])

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

        # -------- User filter OR restriction (same logic as view)
        if data.get("user_id"):
            where += " AND twt.user_id=%s"
            params.append(data["user_id"])
        else:
            if "admin" not in role_name:
                manager_id = str(logged_in_user_id)
                where += f"""
                    AND twt.user_id IN (
                        SELECT tu.user_id
                        FROM tfs_user tu
                        WHERE tu.is_active = 1
                          AND tu.is_delete = 1
                          AND (
                                tu.project_manager_id = %s
                                OR tu.asst_manager_id = %s
                                OR tu.qa_id = %s
                                OR tu.user_id = %s
                                OR FIND_IN_SET(%s, {cleaned_csv_col("tu.project_manager_id")}) > 0
                                OR FIND_IN_SET(%s, {cleaned_csv_col("tu.asst_manager_id")}) > 0
                                OR FIND_IN_SET(%s, {cleaned_csv_col("tu.qa_id")}) > 0
                          )
                    )
                """
                params.extend([manager_id] * 7)

        # -------- Daily aggregation + cumulative + daily required
        # ❌ total_production_day removed
        # ✅ qc_score + assigned_hours added from temp_qc (user_id + date)
        query = f"""
            WITH daily AS (
                SELECT
                    twt.user_id,
                    DATE(CAST(twt.date_time AS DATETIME)) AS work_date,
                    SUM(COALESCE(twt.production, 0) / NULLIF(twt.tenure_target, 0)) AS total_billable_hours_day,
                    COUNT(*) AS trackers_count_day
                FROM task_work_tracker twt
                LEFT JOIN tfs_user u ON u.user_id = twt.user_id
                {where}
                GROUP BY twt.user_id, DATE(CAST(twt.date_time AS DATETIME))
            ),
            daily_with_cum AS (
                SELECT
                    d.*,
                    SUM(d.total_billable_hours_day)
                        OVER (PARTITION BY d.user_id ORDER BY d.work_date)
                        AS cumulative_billable_hours_till_day,
                    COUNT(*) OVER (PARTITION BY d.user_id ORDER BY d.work_date)
                        AS worked_days_till_day
                FROM daily d
            )
            SELECT
                dwc.user_id,
                u.user_name,
                dwc.work_date,

                ROUND(dwc.total_billable_hours_day, 4) AS total_billable_hours_day,
                dwc.trackers_count_day,

                ROUND(dwc.cumulative_billable_hours_till_day, 4)
                    AS cumulative_billable_hours_till_day,

                -- ✅ QC data for that day (temp_qc.date is TEXT 'YYYY-MM-DD')
                tq.qc_score AS qc_score,
                tq.assigned_hours AS assigned_hours,

                umt.user_monthly_tracker_id,
                COALESCE(CAST(umt.monthly_target AS DECIMAL(10,2)), 0) AS monthly_target,
                COALESCE(umt.extra_assigned_hours, 0) AS extra_assigned_hours,
                (
                  COALESCE(CAST(umt.monthly_target AS DECIMAL(10,2)), 0)
                  + COALESCE(umt.extra_assigned_hours, 0)
                ) AS monthly_total_target,

                CAST(umt.working_days AS SIGNED) AS working_days,

                GREATEST(
                    COALESCE(CAST(umt.working_days AS SIGNED), 0)
                    - COALESCE(dwc.worked_days_till_day, 0),
                    0
                ) AS pending_days_after_this_day,

                CASE
                  WHEN umt.user_monthly_tracker_id IS NULL THEN NULL
                  WHEN GREATEST(
                        COALESCE(CAST(umt.working_days AS SIGNED), 0)
                        - COALESCE(dwc.worked_days_till_day, 0),
                        0
                      ) = 0 THEN NULL
                  ELSE
                    (
                      (
                        COALESCE(CAST(umt.monthly_target AS DECIMAL(10,2)), 0)
                        + COALESCE(umt.extra_assigned_hours, 0)
                      )
                      - COALESCE(dwc.cumulative_billable_hours_till_day, 0)
                    )
                    / NULLIF(
                        GREATEST(
                            COALESCE(CAST(umt.working_days AS SIGNED), 0)
                            - COALESCE(dwc.worked_days_till_day, 0),
                            0
                        ),
                        0
                      )
                END AS daily_required_hours
            FROM daily_with_cum dwc
            JOIN tfs_user u ON u.user_id = dwc.user_id

            LEFT JOIN temp_qc tq
              ON tq.user_id = dwc.user_id
             AND tq.{QC_DATE_COL} = DATE_FORMAT(dwc.work_date, '%Y-%m-%d')

            LEFT JOIN user_monthly_tracker umt
              ON umt.user_id = dwc.user_id
             AND umt.is_active = 1
             AND umt.month_year = %s

            ORDER BY dwc.work_date DESC, u.user_name ASC
        """

        final_params = list(params) + [month_year]
        cursor.execute(query, tuple(final_params))
        rows = cursor.fetchall()

        # -------- Response KEYS SAME AS /view
        return api_response(
            200,
            "Trackers fetched successfully",
            {
                "count": len(rows),
                "month_year": month_year,
                "trackers": rows,      # 🔑 SAME KEY AS VIEW
                "month_summary": []    # 🔑 KEPT FOR FRONTEND COMPATIBILITY
            },
        )

    except Exception as e:
        return api_response(500, f"Failed to fetch daily trackers: {str(e)}")

    finally:
        cursor.close()
        conn.close()