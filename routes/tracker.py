from flask import Blueprint, request
from config import get_db_connection
from utils.response import api_response
from utils.api_log_utils import log_api_call
from utils.cloudinary_utils import upload_to_cloudinary, delete_from_cloudinary, FOLDER_TRACKER
from datetime import datetime, timedelta, date
from collections import defaultdict
from utils.qc_auto_score import AUTO_QC_DAYS_SQL, MANUAL_QC_DAYS_SQL, sync_auto_qc_score_for_day
import logging
import re
import os

tracker_bp = Blueprint("tracker", __name__)
logger = logging.getLogger(__name__)
TRACKER_UPLOAD_FAILURE_MESSAGE = "File upload failed. Your tracker has not been submitted. Please upload the file again and retry."
TRACKER_UPDATE_UPLOAD_FAILURE_MESSAGE = "File upload failed. Your tracker has not been updated. Please upload the file again and retry."


# ------------------------
# HELPERS
# ------------------------

def calculate_targets(base_target, user_tenure):
    user_tenure = float(user_tenure)
    base_target = float(base_target)
    actual_target = round(base_target * 1, 2)
    tenure_target = round(base_target * user_tenure, 2)
    return actual_target, tenure_target


def _roster_working_weights(cursor, user_ids: list, month_year: str) -> dict:
    """user_id -> [(YYYY-MM-DD, weight), ...] for Working roster days only."""
    if not user_ids or not month_year:
        return {}
    in_ph = ",".join(["%s"] * len(user_ids))
    cursor.execute(
        f"""
        SELECT rm.user_id, DATE(rd.roster_date) AS roster_date,
            CASE
              WHEN COALESCE(rd.working_type, 'Full') = 'Half' THEN 0.5
              ELSE 1
            END AS day_weight
        FROM roster_month rm
        JOIN roster_day rd
          ON rd.roster_month_id = rm.roster_month_id
         AND rd.is_active = 1
        WHERE rm.is_active = 1
          AND UPPER(rm.month_year) = UPPER(%s)
          AND rd.day_type = 'Working'
          AND rm.user_id IN ({in_ph})
        """,
        tuple([month_year] + [int(uid) for uid in user_ids]),
    )
    weights_by_user = defaultdict(list)
    for row in cursor.fetchall() or []:
        d = row.get("roster_date")
        d_str = d.strftime("%Y-%m-%d") if hasattr(d, "strftime") else str(d or "")[:10]
        if not d_str:
            continue
        weights_by_user[int(row["user_id"])].append((d_str, float(row.get("day_weight") or 0)))
    return weights_by_user


def _remaining_from_weights(weights: list, start_date: str, *, include_start: bool) -> float:
    total = 0.0
    for d_str, w in weights:
        if include_start:
            if d_str >= start_date:
                total += w
        elif d_str > start_date:
            total += w
    return round(total, 2)


def _apply_roster_pending_days(cursor, rows: list, month_year: str) -> None:
    """
    Pending days = roster Working days still ahead.
    Past Working days are gone even with no tracker/assigned hours.
    Email/yesterday row: remaining starts tomorrow's calendar from that row (includes today).
    Tracker UI today: exclude today when today's tracker is already present.
    """
    today_str = date.today().isoformat()
    user_ids = sorted({int(r["user_id"]) for r in rows if r.get("user_id") is not None})
    weights_by_user = _roster_working_weights(cursor, user_ids, month_year)

    for r in rows:
        if r.get("user_id") is None:
            continue
        uid = int(r["user_id"])
        wd = str(r.get("work_date") or "")[:10]
        if not wd:
            continue
        try:
            trackers = int(r.get("trackers_count_day") or 0)
        except (TypeError, ValueError):
            trackers = 0
        include_start = wd == today_str and trackers <= 0
        remaining = _remaining_from_weights(
            weights_by_user.get(uid, []),
            wd,
            include_start=include_start,
        )
        r["pending_days_after_this_day"] = remaining
        try:
            monthly_total = float(r.get("monthly_total_target") or r.get("monthly_target") or 0)
        except (TypeError, ValueError):
            monthly_total = 0.0
        try:
            cumulative = float(r.get("cumulative_billable_hours_till_day") or 0)
        except (TypeError, ValueError):
            cumulative = 0.0
        pending_hours = monthly_total - cumulative
        if r.get("user_monthly_tracker_id") is None and monthly_total == 0:
            continue
        r["daily_required_hours"] = (pending_hours / remaining) if remaining else pending_hours


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


def can_access_task_eod_report(role_context: dict) -> bool:
    role_id = int(role_context.get("user_role_id") or 0)
    role_name = (role_context.get("user_role_name") or "").strip().lower()
    agent_role_id = int(role_context.get("agent_role_id") or 0)

    # Task EOD report should be accessible to all roles except Agent.
    # We check both role name and role id (if available) to be safe.
    if role_name == "agent":
        return False
    if agent_role_id and role_id == agent_role_id:
        return False

    return True


def normalize_eod_column_name(name) -> str:
    """
    Normalize Excel/CSV headers so visually identical names merge correctly.
    Agents often paste headers with non-breaking spaces (\\xa0) from Word/Sheets;
    those must match regular spaces used by other agents' files.
    """
    import unicodedata

    if name is None:
        return ""
    text = unicodedata.normalize("NFKC", str(name))
    text = "".join(" " if unicodedata.category(ch) == "Zs" else ch for ch in text)
    return " ".join(text.split())


def normalize_eod_dataframe_columns(df):
    """Rename dataframe columns with normalize_eod_column_name; merge any duplicates."""
    import pandas as pd

    df = df.copy()
    df.columns = [normalize_eod_column_name(c) for c in df.columns]
    if not df.columns.duplicated().any():
        return df

    # Same logical header appeared twice (e.g. NBSP + normal space variants).
    merged = pd.DataFrame(index=df.index)
    for col in dict.fromkeys(df.columns):
        same = df.loc[:, df.columns == col]
        if same.shape[1] == 1:
            merged[col] = same.iloc[:, 0]
        else:
            merged[col] = same.bfill(axis=1).iloc[:, 0]
    return merged


def cleaned_csv_col(col_sql: str) -> str:
    return f"REPLACE(REPLACE(REPLACE({col_sql}, '[', ''), ']', ''), ' ', '')"


def check_cloudinary_file_status(url: str):
    """
    Lightweight Cloudinary reachability check for existing stored URLs.

    Returns:
      - None if reachable/ok (HTTP < 400)
      - "file_not_found" if HTTP 404
      - "file_unreachable" for other HTTP >= 400 or network errors
    """
    if not url or "res.cloudinary.com" not in str(url):
        return None

    import time
    import requests

    last_err = None
    for attempt in range(1, 3):  # small retry for transient issues
        try:
            resp = requests.head(str(url), timeout=8, allow_redirects=True)
            if resp.status_code == 404:
                return "file_not_found"
            if resp.status_code >= 400:
                return "file_unreachable"
            return None
        except Exception as e:
            last_err = e
            if attempt < 2:
                time.sleep(1)

    return "file_unreachable" if last_err else None



# ---------- NEW: filename helpers (tracker-specific, NOT in file_utils)

def _clean_part(value: str) -> str:
    value = re.sub(r"\s+", "_", value)
    value = re.sub(r"[^A-Za-z0-9_]", "", value)
    return value or "NA"


def build_tracker_filename(
    project_code: str,
    task_name: str,
    user_name: str,
    original_filename: str,
    date_source_dt=None,
) -> str:
    """
    Format: projectcode_taskname_username_date_time.ext
    time format: hours-minutes-seconds with AM/PM (avoids same-hour overwrites)
    Example: PROJ_Task_User_23-Jul-2026_04-13-22PM.xlsx
    """
    if "." not in (original_filename or ""):
        raise ValueError("Uploaded file has no extension")

    ext = original_filename.rsplit(".", 1)[1].lower().strip()
    now = datetime.now()
    # During updates, allow the tracker datetime to drive the date/time parts
    # while keeping the same formatting.
    source_dt = date_source_dt or now
    date_part = source_dt.strftime("%d-%b-%Y")       # 05-Feb-2026
    time_part = source_dt.strftime("%I-%M-%S%p")     # 04-13-22PM
    return (
        f"{_clean_part(project_code)}_"
        f"{_clean_part(task_name)}_"
        f"{_clean_part(user_name)}_"
        f"{date_part}_{time_part}.{ext}"
    )


def _parse_tracker_date_source(date_time_value):
    """
    Best-effort parser to extract the tracker date to be used in file naming during updates.
    This intentionally affects ONLY the date portion of the filename (format unchanged).
    """
    if isinstance(date_time_value, datetime):
        return date_time_value

    s = str(date_time_value or "").strip()
    if not s:
        return None

    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt)
        except Exception:
            continue
    return None


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


def log_tracker_upload_failure(user_id, form, uploaded, error) -> None:
    logger.exception(
        "Tracker file upload failed | user_id=%s project_id=%s task_id=%s shift=%s production=%s file_name=%s timestamp=%s error=%s",
        user_id,
        form.get("project_id"),
        form.get("task_id"),
        form.get("shift"),
        form.get("production"),
        getattr(uploaded, "filename", None),
        datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        str(error),
    )

# ------------------------
# ADD TRACKER  (multipart + custom filename)
# ------------------------
@tracker_bp.route("/add", methods=["POST"])
def add_tracker():
    now_str = None
    form = request.form
    new_file_saved = None

    required_fields = ["project_id", "task_id", "user_id", "production", "tenure_target"]
    for f in required_fields:
        if not form.get(f):
            return api_response(400, f"{f} is required")

    project_id = int(form["project_id"])
    task_id = int(form["task_id"])
    user_id = int(form["user_id"])
    production = float(form["production"])
    shift = form.get("shift", "DAY").upper()
    now_str = form.get("date")
    print(now_str)

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

        # --- get user_name + tenure (if tenure < 1 → use 1)
        cursor.execute(
            "SELECT user_name, user_tenure FROM tfs_user WHERE user_id=%s",
            (user_id,),
        )
        usr_row = cursor.fetchone() or {}
        user_name = usr_row.get("user_name") or "USER"
        try:
            user_tenure = float(usr_row.get("user_tenure") or 1)
        except (TypeError, ValueError):
            user_tenure = 1.0
        if user_tenure < 1:
            user_tenure = 1.0
        tenure_target = round(float(actual_target or 0) * user_tenure, 2)
        billable_hours = production / tenure_target if tenure_target else 0

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
                print(f"Cloudi nary  upload successful: {cloudinary_url}")
                
                # Verify the uploaded file is accessible on Cloudinary
                file_status = check_cloudinary_file_status(cloudinary_url)
                if file_status == "file_not_found":
                    log_tracker_upload_failure(user_id, form, uploaded, "File not found on Cloudinary after upload")
                    return api_response(400, TRACKER_UPLOAD_FAILURE_MESSAGE)
                elif file_status == "file_unreachable":
                    log_tracker_upload_failure(user_id, form, uploaded, "File unreachable on Cloudinary after upload")
                    return api_response(500, TRACKER_UPLOAD_FAILURE_MESSAGE)
                
                tracker_file = cloudinary_url
                new_file_saved = cloudinary_url
            except ValueError as e:
                log_tracker_upload_failure(user_id, form, uploaded, e)
                return api_response(400, TRACKER_UPLOAD_FAILURE_MESSAGE)
            except Exception as e:
                log_tracker_upload_failure(user_id, form, uploaded, e)
                return api_response(500, TRACKER_UPLOAD_FAILURE_MESSAGE)

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
        tracker_id = cursor.lastrowid
        work_dt = _parse_tracker_date_source(now_str)
        sync_auto_qc_score_for_day(cursor, user_id, work_dt or now_str)
        conn.commit()

        device_id = form.get("device_id")
        device_type = form.get("device_type")
        api_call_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        log_api_call("add_tracker", user_id, device_id, device_type, api_call_time)

        return api_response(201, "Tracker added successfully", {"tracker_id": tracker_id})

    except Exception as e:
        conn.rollback()
        if new_file_saved:
            safe_delete_cloudinary_tracker(new_file_saved)
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
        # Tenure min=1 clamp applies only at ADD time — update keeps submitted / raw tenure math
        if "base_target" in form:
            tenure_target = float(form.get("base_target"))  # API sends tenure_target as base_target
            user_tenure = float(user_row["user_tenure"]) if user_row.get("user_tenure") is not None else 0
            base_target = tenure_target / user_tenure if user_tenure else 0
            actual_target = round(base_target, 2)
        else:
            # keep existing targets from tracker (convert to float)
            actual_target = float(tracker["actual_target"]) if tracker["actual_target"] else 0
            tenure_target = float(tracker["tenure_target"]) if tracker["tenure_target"] else 0

        actual_billable_hours = production / actual_target if actual_target else 0

        tracker_file = old_file
        uploaded = request.files.get("tracker_file")
        remove_flag = (
            str(form.get("remove_file") or request.values.get("remove_file") or "").strip().lower()
        )
        remove_file = remove_flag in ("1", "true", "yes")
        print(
            f"[tracker update] tracker_id={tracker_id} remove_file={remove_file} "
            f"has_upload={bool(uploaded and uploaded.filename)} old_file={bool(old_file)}"
        )

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

            # Use the tracker's stored date (date_time) for the DATE part of the filename.
            # Keep the filename format and upload flow unchanged.
            date_source_dt = _parse_tracker_date_source(date_time)
            custom_filename = build_tracker_filename(
                project_code,
                task_name,
                user_name,
                uploaded.filename,
                date_source_dt=date_source_dt,
            )

            # ✅ Upload new file to Cloudinary first
            try:
                cloudinary_url, _ = upload_to_cloudinary(
                    uploaded, FOLDER_TRACKER, display_name=custom_filename, resource_type="raw"
                )
                
                # Verify the uploaded file is accessible on Cloudinary
                file_status = check_cloudinary_file_status(cloudinary_url)
                if file_status == "file_not_found":
                    log_tracker_upload_failure(tracker.get("user_id"), form, uploaded, "File not found on Cloudinary after upload")
                    return api_response(400, TRACKER_UPDATE_UPLOAD_FAILURE_MESSAGE)
                elif file_status == "file_unreachable":
                    log_tracker_upload_failure(tracker.get("user_id"), form, uploaded, "File unreachable on Cloudinary after upload")
                    return api_response(500, TRACKER_UPDATE_UPLOAD_FAILURE_MESSAGE)
            except ValueError as e:
                log_tracker_upload_failure(tracker.get("user_id"), form, uploaded, e)
                return api_response(400, TRACKER_UPDATE_UPLOAD_FAILURE_MESSAGE)
            except Exception as e:
                log_tracker_upload_failure(tracker.get("user_id"), form, uploaded, e)
                return api_response(500, TRACKER_UPDATE_UPLOAD_FAILURE_MESSAGE)

            new_file_saved = cloudinary_url

            # ✅ Don't delete old file immediately - let Cloudinary handle overwrites
            # This prevents 404 errors due to timing issues between upload and delete
            # Cloudinary will automatically manage versions when overwrite=True is used
            tracker_file = cloudinary_url
            remove_file = False
        elif remove_file:
            tracker_file = None
            if old_file:
                cursor.execute(
                    "DELETE FROM tracker_records WHERE file_path = %s",
                    (old_file,),
                )

        # updated_date = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        now = datetime.now()
        # if shift == "NIGHT" and now.hour < 6:
        #     adjusted_datetime = now - timedelta(days=1)
        # else:
        #     adjusted_datetime = now

        updated_date = now.strftime("%Y-%m-%d %H:%M:%S")
        # date_time = adjusted_datetime.strftime("%Y-%m-%d %H:%M:%S")
        
        # If tracker_note is present in the form (even blank), use it so notes can be cleared.
        # If the key is omitted entirely, keep the existing note.
        if "tracker_note" in form:
            raw_note = form.get("tracker_note")
            tracker_note = (raw_note or "").strip() or None
        else:
            tracker_note = tracker.get("tracker_note")

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
        sync_auto_qc_score_for_day(
            cursor,
            tracker["user_id"],
            _parse_tracker_date_source(date_time) or date_time,
        )
        conn.commit()

        # if DB commit succeeded, clear rollback marker
        new_file_saved = None
        if remove_file and old_file:
            safe_delete_cloudinary_tracker(old_file)

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
            "SELECT tracker_id, user_id, tracker_file, date_time FROM task_work_tracker WHERE tracker_id=%s",
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

        sync_auto_qc_score_for_day(
            cursor,
            tracker["user_id"],
            _parse_tracker_date_source(tracker.get("date_time")) or tracker.get("date_time"),
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
    data = request.get_json(silent=True) or {}

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    try:
        logged_in_user_id = data.get("logged_in_user_id")
        if not logged_in_user_id:
            return api_response(400, "logged_in_user_id is required")

        ctx = get_role_context(cursor, int(logged_in_user_id))
        role_name = ctx["user_role_name"]

        pagination_requested = (
            bool(data.get("paginate"))
            or data.get("page") is not None
            or data.get("page_size") is not None
            or data.get("limit") is not None
        )

        current_page = 1
        page_size = 20

        if pagination_requested:
            try:
                current_page = int(data.get("page", 1))
                page_size = int(data.get("page_size", data.get("limit", 20)))
            except (TypeError, ValueError):
                return api_response(400, "page and page_size must be valid integers")

            if current_page < 1 or page_size < 1:
                return api_response(400, "page and page_size must be greater than 0")

            page_size = min(page_size, 500)

        params = []
        from_clause = """
        FROM task_work_tracker twt
        LEFT JOIN tfs_user u ON u.user_id = twt.user_id
        LEFT JOIN project p ON p.project_id = twt.project_id
        LEFT JOIN task tk ON tk.task_id = twt.task_id
        LEFT JOIN project_category pc ON pc.project_category_id = p.project_category_id
        LEFT JOIN team t ON u.team_id = t.team_id
        """
        where_clauses = ["twt.is_active != 0"]

        if data.get("team_id"):
            where_clauses.append("u.team_id=%s")
            params.append(data["team_id"])
        if data.get("user_id"):
            user_ids_filter = data["user_id"]

            if not isinstance(user_ids_filter, list):
                user_ids_filter = [user_ids_filter]

            placeholders = ",".join(["%s"] * len(user_ids_filter))
            where_clauses.append(f"twt.user_id IN ({placeholders})")
            params.extend(user_ids_filter)
        elif role_name not in ("admin", "super admin", "project manager"):
            manager_id_str = str(logged_in_user_id)
            manager_id_int = int(logged_in_user_id)
            where_clauses.append(
                """
                twt.user_id IN (
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
            )
            params.extend([
                manager_id_str, manager_id_int,
                manager_id_str, manager_id_int,
                manager_id_str, manager_id_int,
                manager_id_int,
                manager_id_str, manager_id_str,
                manager_id_str, manager_id_str,
                manager_id_str, manager_id_str
            ])
        if data.get("project_id"):
            where_clauses.append("twt.project_id=%s")
            params.append(data["project_id"])
        if data.get("task_id"):
            where_clauses.append("twt.task_id=%s")
            params.append(data["task_id"])
        if data.get("shift"):
            where_clauses.append("twt.shift=%s")
            params.append(data["shift"].upper())
        if data.get("date_from"):
            df = data["date_from"]
            if len(df) == 10:
                df += " 00:00:00"
            where_clauses.append("CAST(twt.date_time AS DATETIME) >= %s")
            params.append(df)
        if data.get("date_to"):
            dt_ = data["date_to"]
            if len(dt_) == 10:
                dt_ += " 23:59:59"
            where_clauses.append("CAST(twt.date_time AS DATETIME) <= %s")
            params.append(dt_)
        if data.get("is_active") is not None:
            where_clauses.append("twt.is_active=%s")
            params.append(data["is_active"])
        if data.get("qc_pending") is not None:
            where_clauses.append("twt.qc_status = %s")
            params.append(data["qc_pending"])
            where_clauses.append("twt.tracker_file IS NOT NULL")
            where_clauses.append("twt.tracker_file != ''")

        where_sql = " WHERE " + " AND ".join(where_clauses)
        filtered_query_suffix = f"{from_clause}{where_sql}"

        count_query = f"""
            SELECT COUNT(*) AS total_records
            {filtered_query_suffix}
        """
        cursor.execute(count_query, tuple(params))
        total_records = int((cursor.fetchone() or {}).get("total_records") or 0)

        total_pages = 1
        data_params = list(params)
        pagination_sql = ""

        if pagination_requested:
            total_pages = max((total_records + page_size - 1) // page_size, 1)
            current_page = min(current_page, total_pages)
            offset = (current_page - 1) * page_size
            pagination_sql = " LIMIT %s OFFSET %s"
            data_params.extend([page_size, offset])

        query = f"""
        SELECT 
            twt.*, u.user_id, u.user_id AS agent_id, u.user_name, u.user_email, u.user_tenure,
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
            {filtered_query_suffix}
            ORDER BY CAST(twt.date_time AS DATETIME) DESC
            {pagination_sql}
        """
        cursor.execute(query, tuple(data_params))
        trackers = cursor.fetchall()

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

        totals_query = f"""
            SELECT
                COALESCE(SUM(COALESCE(twt.tenure_target, 0)), 0) AS total_tenure_target,
                COALESCE(SUM(COALESCE(twt.production, 0)), 0) AS total_production,
                COALESCE(SUM(COALESCE(twt.production, 0) / NULLIF(twt.tenure_target, 0)), 0) AS total_billable_hours,
                COUNT(DISTINCT twt.user_id) AS total_active_agents
            {filtered_query_suffix}
        """
        cursor.execute(totals_query, tuple(params))
        totals_row = cursor.fetchone() or {}

        assigned_query = """
            SELECT COALESCE(SUM(tqc.assigned_hours), 0) AS total_assigned
            FROM (
                SELECT DISTINCT twt.user_id, DATE(CAST(twt.date_time AS DATETIME)) AS work_date
        """ + filtered_query_suffix + """
            ) filtered_days
            INNER JOIN temp_qc tqc
                ON tqc.user_id = filtered_days.user_id
                AND DATE(tqc.date) = filtered_days.work_date
        """
        cursor.execute(assigned_query, tuple(params))
        total_assigned_hours = float((cursor.fetchone() or {}).get("total_assigned") or 0)

        totals = {
            "total_tenure_target": round(float(totals_row.get("total_tenure_target") or 0), 2),
            "total_billable_hours": round(float(totals_row.get("total_billable_hours") or 0), 2),
            "total_production": round(float(totals_row.get("total_production") or 0), 2),
            "total_assigned_hours": round(total_assigned_hours, 2),
            "total_active_agents": int(totals_row.get("total_active_agents") or 0)
        }

        log_api_call("view_trackers", logged_in_user_id, data.get("device_id"), data.get("device_type"), datetime.now().strftime("%Y-%m-%d %H:%M:%S"))

        response_data = {
            "count": len(trackers),
            "trackers": trackers,
            "totals": totals
        }

        if pagination_requested:
            response_data["pagination"] = {
                "current_page": current_page,
                "page_size": page_size,
                "total_records": total_records,
                "total_pages": total_pages,
                "has_previous": current_page > 1,
                "has_next": current_page < total_pages
            }

        return api_response(200, "Trackers fetched successfully", response_data)

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

        # 1️⃣ If date filter exists → derive month from date_from (roster/target join)
        has_date_range = bool(data.get("date_from") or data.get("date_to"))
        if has_date_range:
            try:
                ref_date = data.get("date_from") or data.get("date_to")
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

        # Month filter — skip when date_from/date_to are the source of truth
        if not has_date_range:
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

        # Date range filters (inclusive by calendar day — avoids timezone off-by-one)
        if data.get("date_from"):
            date_from = str(data["date_from"]).strip()[:10]
            where += " AND DATE(CAST(twt.date_time AS DATETIME)) >= %s"
            params.append(date_from)

        if data.get("date_to"):
            date_to = str(data["date_to"]).strip()[:10]
            where += " AND DATE(CAST(twt.date_time AS DATETIME)) <= %s"
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
                    SUM(COALESCE(twt.production, 0) / NULLIF(twt.tenure_target, 0)) AS total_billable_hours_day,
                    COUNT(*) AS trackers_count_day
                FROM task_work_tracker twt
                LEFT JOIN tfs_user u ON u.user_id = twt.user_id
                {where}
                GROUP BY twt.user_id, twt.shift, DATE(CAST(twt.date_time AS DATETIME))
            ),
            worked_days AS (
                SELECT
                    twt.user_id,
                    DATE(CAST(twt.date_time AS DATETIME)) AS work_date,
                    -- Half/Full from roster (not assigned_hours: tenure 0.5 also has 4.5h full day)
                    CASE
                        WHEN MAX(rd.working_type) = 'Half' THEN 0.5
                        ELSE 1
                    END AS day_weight
                FROM task_work_tracker twt
                LEFT JOIN tfs_user u ON u.user_id = twt.user_id
                LEFT JOIN roster_month rm
                    ON rm.user_id = twt.user_id
                   AND rm.is_active = 1
                   AND UPPER(rm.month_year) = UPPER(%s)
                LEFT JOIN roster_day rd
                    ON rd.roster_month_id = rm.roster_month_id
                   AND rd.is_active = 1
                   AND DATE(rd.roster_date) = DATE(CAST(twt.date_time AS DATETIME))
                {where}
                GROUP BY twt.user_id, DATE(CAST(twt.date_time AS DATETIME))
            ),
            daily_with_cum AS (
                SELECT
                    d.*,
                    SUM(d.total_billable_hours_day)
                        OVER (PARTITION BY d.user_id ORDER BY d.work_date)
                        AS cumulative_billable_hours_till_day,
                    -- Consumed days through this work_date (this day never stays in "remaining")
                    COALESCE((
                        SELECT SUM(wd.day_weight)
                        FROM worked_days wd
                        WHERE wd.user_id = d.user_id
                          AND wd.work_date < d.work_date
                    ), 0)
                    + COALESCE((
                        SELECT wd.day_weight
                        FROM worked_days wd
                        WHERE wd.user_id = d.user_id
                          AND wd.work_date = d.work_date
                        LIMIT 1
                    ), 1) AS worked_days_till_day
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
                DAYNAME(dwc.work_date) AS day,

                ROUND(dwc.total_billable_hours_day, 4) AS total_billable_hours_day,
                dwc.trackers_count_day,

                ROUND(dwc.cumulative_billable_hours_till_day, 4)
                    AS cumulative_billable_hours_till_day,

                -- QC score from temp_qc / qc_records.
                -- Assigned hours: source of truth is temp_qc (written by assign_daily_hours cron).
                -- Fallback to roster Working/Half + tenure only when no temp_qc row yet.
                COALESCE(tqc.qc_score, qr.qc_score, auto_qc.auto_qc_score) AS qc_score,
                COALESCE(manual_qc.can_manual_qc, 0) AS can_manual_qc,
                COALESCE(
                    tqc.assigned_hours,
                    CASE
                      WHEN rd.day_type = 'Leave' AND (
                            rd.working_type = 'Half'
                         OR COALESCE(rl.is_half_day, 0) = 1
                      ) THEN
                        ROUND(4.5 * LEAST(GREATEST(COALESCE(u.user_tenure, 1), 0), 1), 2)
                      WHEN rd.day_type IN ('Leave', 'WeekOff', 'Holiday', 'Left', 'PreJoin') THEN 0
                      WHEN rd.working_type = 'Half' THEN
                        ROUND(4.5 * LEAST(GREATEST(COALESCE(u.user_tenure, 1), 0), 1), 2)
                      WHEN rd.roster_day_id IS NOT NULL THEN
                        ROUND(9 * LEAST(GREATEST(COALESCE(u.user_tenure, 1), 0), 1), 2)
                      ELSE
                        ROUND(9 * LEAST(GREATEST(COALESCE(u.user_tenure, 1), 0), 1), 2)
                    END
                ) AS assigned_hours,
                COALESCE(rd.working_type, 'Full') AS working_type,
                COALESCE(rd.day_type, '') AS day_type,
                COALESCE(rl.is_half_day, 0) AS is_half_day,
                CASE
                  WHEN rd.day_type = 'WeekOff' THEN 'Week Off'
                  WHEN rd.day_type = 'Holiday' THEN 'Holiday'
                  WHEN rd.day_type = 'PreJoin' THEN 'Pre Join'
                  WHEN rd.day_type = 'Left' THEN 'Left'
                  WHEN rd.day_type = 'Leave' AND (
                        rd.working_type = 'Half'
                     OR COALESCE(rl.is_half_day, 0) = 1
                  ) THEN 'Half Day Leave'
                  WHEN rd.day_type = 'Leave' THEN 'Leave'
                  WHEN rd.day_type = 'Working' AND rd.working_type = 'Half' THEN 'Half Day'
                  WHEN rd.day_type = 'Working' THEN 'Working'
                  ELSE '—'
                END AS roster_status,

                umt.user_monthly_tracker_id,
                COALESCE(CAST(umt.monthly_target AS DECIMAL(10,2)), 0) AS monthly_target,
                COALESCE(umt.extra_assigned_hours, 0) AS extra_assigned_hours,
                (
                  COALESCE(CAST(umt.monthly_target AS DECIMAL(10,2)), 0)
                  + COALESCE(umt.extra_assigned_hours, 0)
                ) AS monthly_total_target,

                CAST(umt.working_days AS DECIMAL(10,2)) AS working_days,

                GREATEST(
                    COALESCE(CAST(umt.working_days AS DECIMAL(10,2)), 0)
                    - COALESCE(dwc.worked_days_till_day, 0),
                    0
                ) AS pending_days_after_this_day,

                CASE
                  WHEN umt.user_monthly_tracker_id IS NULL THEN NULL
                  WHEN GREATEST(
                        COALESCE(CAST(umt.working_days AS DECIMAL(10,2)), 0)
                        - COALESCE(dwc.worked_days_till_day, 0),
                        0
                      ) = 0 THEN
                    (
                      COALESCE(CAST(umt.monthly_target AS DECIMAL(10,2)), 0)
                      + COALESCE(umt.extra_assigned_hours, 0)
                    )
                    - COALESCE(dwc.cumulative_billable_hours_till_day, 0)
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
                            COALESCE(CAST(umt.working_days AS DECIMAL(10,2)), 0)
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
                {AUTO_QC_DAYS_SQL}
            ) auto_qc
              ON auto_qc.user_id = dwc.user_id
             AND auto_qc.work_date = dwc.work_date

            LEFT JOIN (
                {MANUAL_QC_DAYS_SQL}
            ) manual_qc
              ON manual_qc.user_id = dwc.user_id
             AND manual_qc.work_date = dwc.work_date

            LEFT JOIN roster_month rm
              ON rm.user_id = dwc.user_id
             AND rm.is_active = 1
             AND UPPER(rm.month_year) = UPPER(%s)
            LEFT JOIN roster_day rd
              ON rd.roster_month_id = rm.roster_month_id
             AND rd.is_active = 1
             AND DATE(rd.roster_date) = dwc.work_date
            LEFT JOIN roster_leave rl
              ON rl.leave_id = rd.leave_id
             AND rl.is_active = 1

            LEFT JOIN user_monthly_tracker umt
              ON umt.user_id = dwc.user_id
             AND umt.is_active = 1
             AND UPPER(umt.month_year) = UPPER(%s)

            ORDER BY dwc.work_date DESC, u.user_name ASC
        """

        # daily where + worked_days (month_year + where) + roster month_year + umt month_year
        final_params = list(params) + [month_year] + list(params) + [month_year, month_year]
        cursor.execute(query, tuple(final_params))
        rows = cursor.fetchall()

        from utils.roster_helpers import is_team_agent_user, roster_day_status_label

        for r in rows:
            if r.get("work_date") is not None:
                wd = r["work_date"]
                r["work_date"] = wd.strftime("%Y-%m-%d") if hasattr(wd, "strftime") else str(wd)[:10]
            status = (r.get("roster_status") or "").strip()
            if not status or status == "—":
                r["roster_status"] = roster_day_status_label(
                    r.get("day_type"),
                    r.get("working_type"),
                    r.get("is_half_day"),
                )
            if is_team_agent_user(r):
                r["assigned_hours"] = None

        _apply_roster_pending_days(cursor, rows, month_year)

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
                             COALESCE(CAST(umt.working_days AS DECIMAL(10,2)), 0)
                             - COALESCE((
                                 SELECT COUNT(DISTINCT DATE(CAST(twt2.date_time AS DATETIME)))
                                 FROM task_work_tracker twt2
                                 WHERE twt2.user_id = u.user_id
                                   AND twt2.is_active = 1
                                   AND (YEAR(CAST(twt2.date_time AS DATETIME))*100 + MONTH(CAST(twt2.date_time AS DATETIME))) = m.yyyymm
                                   AND (
                                        (m.is_current_month = 1 AND DATE(CAST(twt2.date_time AS DATETIME)) < m.cutoff)
                                     OR (m.is_current_month = 0 AND DATE(CAST(twt2.date_time AS DATETIME)) <= m.cutoff)
                                   )
                               ), 0)
                             - CASE
                                 WHEN m.is_current_month = 1 THEN COALESCE((
                                   SELECT CASE WHEN rd.working_type = 'Half' THEN 0.5 ELSE 1 END
                                   FROM roster_month rm
                                   INNER JOIN roster_day rd
                                     ON rd.roster_month_id = rm.roster_month_id
                                    AND rd.is_active = 1
                                    AND DATE(rd.roster_date) = m.cutoff
                                   WHERE rm.user_id = u.user_id
                                     AND rm.is_active = 1
                                     AND UPPER(rm.month_year) = UPPER(m.mon)
                                   LIMIT 1
                                 ), 1)
                                 ELSE 0
                               END,
                             0
                           )
                    END AS pending_days,
                    CASE
                      WHEN umt.user_monthly_tracker_id IS NULL THEN NULL
                      WHEN GREATEST(
                             COALESCE(CAST(umt.working_days AS DECIMAL(10,2)), 0)
                             - COALESCE((
                                 SELECT COUNT(DISTINCT DATE(CAST(twt2.date_time AS DATETIME)))
                                 FROM task_work_tracker twt2
                                 WHERE twt2.user_id = u.user_id
                                   AND twt2.is_active = 1
                                   AND (YEAR(CAST(twt2.date_time AS DATETIME))*100 + MONTH(CAST(twt2.date_time AS DATETIME))) = m.yyyymm
                                   AND (
                                        (m.is_current_month = 1 AND DATE(CAST(twt2.date_time AS DATETIME)) < m.cutoff)
                                     OR (m.is_current_month = 0 AND DATE(CAST(twt2.date_time AS DATETIME)) <= m.cutoff)
                                   )
                               ), 0)
                             - CASE
                                 WHEN m.is_current_month = 1 THEN COALESCE((
                                   SELECT CASE WHEN rd.working_type = 'Half' THEN 0.5 ELSE 1 END
                                   FROM roster_month rm
                                   INNER JOIN roster_day rd
                                     ON rd.roster_month_id = rm.roster_month_id
                                    AND rd.is_active = 1
                                    AND DATE(rd.roster_date) = m.cutoff
                                   WHERE rm.user_id = u.user_id
                                     AND rm.is_active = 1
                                     AND UPPER(rm.month_year) = UPPER(m.mon)
                                   LIMIT 1
                                 ), 1)
                                 ELSE 0
                               END,
                             0
                           ) = 0 THEN
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
                              COALESCE(CAST(umt.working_days AS DECIMAL(10,2)), 0)
                              - COALESCE((
                                  SELECT COUNT(DISTINCT DATE(CAST(twt2.date_time AS DATETIME)))
                                  FROM task_work_tracker twt2
                                  WHERE twt2.user_id = u.user_id
                                    AND twt2.is_active = 1
                                    AND (YEAR(CAST(twt2.date_time AS DATETIME))*100 + MONTH(CAST(twt2.date_time AS DATETIME))) = m.yyyymm
                                    AND (
                                         (m.is_current_month = 1 AND DATE(CAST(twt2.date_time AS DATETIME)) < m.cutoff)
                                      OR (m.is_current_month = 0 AND DATE(CAST(twt2.date_time AS DATETIME)) <= m.cutoff)
                                    )
                                ), 0)
                              - CASE
                                  WHEN m.is_current_month = 1 THEN COALESCE((
                                    SELECT CASE WHEN rd.working_type = 'Half' THEN 0.5 ELSE 1 END
                                    FROM roster_month rm
                                    INNER JOIN roster_day rd
                                      ON rd.roster_month_id = rm.roster_month_id
                                     AND rd.is_active = 1
                                     AND DATE(rd.roster_date) = m.cutoff
                                    WHERE rm.user_id = u.user_id
                                      AND rm.is_active = 1
                                      AND UPPER(rm.month_year) = UPPER(m.mon)
                                    LIMIT 1
                                  ), 1)
                                  ELSE 0
                                END,
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
                      END AS cutoff,
                      CASE
                        WHEN (YEAR(CURDATE())*100 + MONTH(CURDATE())) =
                             CAST(DATE_FORMAT(STR_TO_DATE(CONCAT('01-', %s), '%d-%b%Y'), '%Y%m') AS UNSIGNED)
                        THEN 1
                        ELSE 0
                      END AS is_current_month
                ) m
                LEFT JOIN user_monthly_tracker umt
                  ON umt.user_id = u.user_id
                 AND umt.is_active = 1
                 AND UPPER(umt.month_year) = UPPER(m.mon)
                WHERE u.user_id IN ({in_ph})
                  -- ✅ team filter applied to summary too
                  AND (%s IS NULL OR u.team_id = %s)
            """

            # month_year x7 (mon, yyyymm, 3x cutoff branches, is_current_month) + users + team filter
            summary_params = [month_year] * 7 + user_ids + [team_id, team_id]
            cursor.execute(summary_query, tuple(summary_params))
            month_summary = cursor.fetchall() or []
            today_str = date.today().isoformat()
            has_tracker_today = {
                int(r["user_id"])
                for r in rows
                if r.get("user_id") is not None
                and str(r.get("work_date") or "")[:10] == today_str
                and (r.get("trackers_count_day") or 0)
            }
            summary_weights = _roster_working_weights(cursor, user_ids, month_year)
            for s in month_summary:
                uid = s.get("user_id")
                if uid is None:
                    continue
                include_start = int(uid) not in has_tracker_today
                remaining = _remaining_from_weights(
                    summary_weights.get(int(uid), []),
                    today_str,
                    include_start=include_start,
                )
                s["pending_days"] = remaining
                try:
                    monthly_total = float(s.get("monthly_total_target") or 0)
                except (TypeError, ValueError):
                    monthly_total = 0.0
                try:
                    delivered = float(s.get("total_billable_hours_month") or 0)
                except (TypeError, ValueError):
                    delivered = 0.0
                pending_hours = monthly_total - delivered
                if s.get("user_monthly_tracker_id") is None and monthly_total == 0:
                    continue
                s["daily_required_hours"] = (pending_hours / remaining) if remaining else pending_hours

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


# ------------------------
# TASK EOD CONSOLIDATED REPORT
# ------------------------
@tracker_bp.route("/eod-report/list", methods=["POST"])
def get_eod_report_list():
    """
    Fetch valid task-wise data for the EOD consolidated report.
    Only returns tasks that have all required fields filled.
    """
    data = request.get_json(silent=True) or {}
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    
    try:
        logged_in_user_id = data.get("logged_in_user_id")
        if not logged_in_user_id:
            return api_response(400, "logged_in_user_id is required")

        role_context = get_role_context(cursor, int(logged_in_user_id))
        if not can_access_task_eod_report(role_context):
            return api_response(403, "Only Assistant Manager, Project Manager, and Admin can access Task EOD Report")

        from datetime import date, datetime
        today = date.today()
        default_from_date = today.replace(day=1)
        default_to_date = today
        from_date_str = data.get("from_date")
        to_date_str = data.get("to_date")

        from_date = default_from_date
        to_date = default_to_date

        if from_date_str:
            try:
                from_date = datetime.strptime(from_date_str, "%Y-%m-%d").date()
            except ValueError:
                return api_response(400, "from_date must be in YYYY-MM-DD format")

        if to_date_str:
            try:
                to_date = datetime.strptime(to_date_str, "%Y-%m-%d").date()
            except ValueError:
                return api_response(400, "to_date must be in YYYY-MM-DD format")

        if from_date > to_date:
            return api_response(400, "from_date cannot be greater than to_date")

        # Query to get valid tracker entries with all required fields
        # Validation: production, actual_target, tenure_target, tracker_file must NOT be NULL
        # Also exclude tasks that do not have important_columns configured.
        # Note: billable_hours is calculated as production/tenure_target, so we check the base fields
        query = """
            SELECT 
                twt.tracker_id,
                twt.project_id,
                twt.task_id,
                twt.user_id,
                DATE(twt.date_time) as work_date,
                twt.production,
                twt.actual_target,
                twt.tenure_target,
                twt.tracker_file,
                p.project_name,
                p.project_code,
                t.task_name,
                twt.date_time
            FROM task_work_tracker twt
            JOIN project p ON twt.project_id = p.project_id
            JOIN task t ON twt.task_id = t.task_id
            WHERE DATE(twt.date_time) BETWEEN %s AND %s
                AND twt.is_active = 1
                AND twt.actual_target IS NOT NULL
                AND twt.actual_target != ''
                AND twt.tenure_target IS NOT NULL
                AND twt.tenure_target != ''
                AND twt.tenure_target != '0'
                AND twt.tracker_file IS NOT NULL
                AND twt.tracker_file != ''
                AND t.important_columns IS NOT NULL
                AND TRIM(t.important_columns) != ''
                AND JSON_VALID(t.important_columns)
                AND JSON_LENGTH(t.important_columns) > 0
            ORDER BY twt.date_time DESC
        """
        
        cursor.execute(query, (from_date, to_date))
        all_trackers = cursor.fetchall()

        # Build unique task list for UI display (group by task_id, project_id, work_date)
        task_list = []
        seen_tasks = set()
        
        for tracker in all_trackers:
            task_key = (tracker['task_id'], tracker['project_id'], str(tracker['work_date']))
            if task_key not in seen_tasks:
                seen_tasks.add(task_key)
                task_list.append({
                    "report_date": str(tracker['work_date']),
                    "date": tracker['work_date'].strftime('%d-%b-%Y') if hasattr(tracker['work_date'], 'strftime') else str(tracker['work_date']),
                    "project_id": tracker['project_id'],
                    "project_name": tracker['project_name'],
                    "project_code": tracker['project_code'],
                    "task_id": tracker['task_id'],
                    "task_name": tracker['task_name'],
                    "tracker_count": len([t for t in all_trackers if t['task_id'] == tracker['task_id'] and t['project_id'] == tracker['project_id'] and str(t['work_date']) == str(tracker['work_date'])])
                })
        
        # Sort by latest date first, then project, then task
        task_list.sort(key=lambda x: (x['report_date'], x['project_name'], x['task_name']), reverse=True)
        
        return api_response(200, "EOD report list fetched successfully", {
            "from_date": str(from_date),
            "to_date": str(to_date),
            "total_tasks": len(task_list),
            "tasks": task_list
        })
        
    except Exception as e:
        return api_response(500, f"Failed to fetch EOD report list: {str(e)}")
    
    finally:
        cursor.close()
        conn.close()


@tracker_bp.route("/eod-report/trackers", methods=["POST"])
def get_eod_report_trackers():
    data = request.get_json(silent=True) or {}
    logged_in_user_id = data.get("logged_in_user_id")
    task_id = data.get("task_id")
    project_id = data.get("project_id")
    selected_date = data.get("date")

    if not logged_in_user_id:
        return api_response(400, "logged_in_user_id is required")

    if not task_id or not project_id or not selected_date:
        return api_response(400, "task_id, project_id, and date are required")

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    def is_blank(value):
        return value is None or str(value).strip() == ""

    try:
        role_context = get_role_context(cursor, int(logged_in_user_id))
        if not can_access_task_eod_report(role_context):
            return api_response(403, "Only Assistant Manager, Project Manager, and Admin can access Task EOD Report")

        cursor.execute(
            """
            SELECT
                twt.tracker_id,
                twt.user_id,
                usr.user_name,
                twt.date_time,
                twt.production,
                twt.actual_target,
                twt.tenure_target,
                twt.shift,
                twt.tracker_file,
                twt.is_active
            FROM task_work_tracker twt
            JOIN tfs_user usr ON twt.user_id = usr.user_id
            WHERE twt.task_id = %s
              AND twt.project_id = %s
              AND DATE(twt.date_time) = %s
            ORDER BY twt.date_time DESC
            """,
            (task_id, project_id, selected_date),
        )
        trackers = cursor.fetchall() or []

        valid_trackers = []
        invalid_trackers = []

        for row in trackers:
            reasons = []
            if int(row.get("is_active") or 0) != 1:
                reasons.append("inactive")

            if is_blank(row.get("actual_target")):
                reasons.append("actual_target_missing")

            tenure_target = row.get("tenure_target")
            if is_blank(tenure_target):
                reasons.append("tenure_target_missing")
            elif str(tenure_target).strip() == "0":
                reasons.append("tenure_target_zero")

            if is_blank(row.get("tracker_file")):
                reasons.append("tracker_file_missing")
            else:
                file_reason = check_cloudinary_file_status(row.get("tracker_file"))
                if file_reason:
                    reasons.append(file_reason)

            date_time_value = row.get("date_time")
            date_time_str = ""
            date_time_display = ""
            if hasattr(date_time_value, "strftime"):
                date_time_str = date_time_value.strftime("%Y-%m-%d %H:%M:%S")
                date_time_display = date_time_value.strftime("%m-%d-%Y %I:%M:%S %p")

            payload = {
                "tracker_id": row.get("tracker_id"),
                "user_id": row.get("user_id"),
                "user_name": row.get("user_name") or "-",
                "date_time": date_time_str,
                "date_time_display": date_time_display or date_time_str,
                "production": row.get("production"),
                "actual_target": row.get("actual_target"),
                "tenure_target": row.get("tenure_target"),
                "shift": row.get("shift"),
                "tracker_file": row.get("tracker_file"),
                "is_active": row.get("is_active"),
                "reasons": reasons,
            }

            if reasons:
                invalid_trackers.append(payload)
            else:
                valid_trackers.append(payload)

        return api_response(
            200,
            "EOD trackers fetched successfully",
            {
                "total_all": len(trackers),
                "total_valid": len(valid_trackers),
                "total_invalid": len(invalid_trackers),
                "valid_trackers": valid_trackers,
                "invalid_trackers": invalid_trackers,
            },
        )

    except Exception as e:
        return api_response(500, f"Failed to fetch EOD trackers: {str(e)}")

    finally:
        cursor.close()
        conn.close()


@tracker_bp.route("/eod-report/generate", methods=["POST"])
def generate_eod_report():
    """
    Generate consolidated EOD report for a specific task on a specific date.
    Downloads all tracker files, merges them, deduplicates based on important_columns, and returns Excel file.
    """
    data = request.get_json(silent=True) or {}
    logged_in_user_id = data.get("logged_in_user_id")
    task_id = data.get("task_id")
    project_id = data.get("project_id")
    selected_date = data.get("date")
    
    if not logged_in_user_id:
        return api_response(400, "logged_in_user_id is required")

    if not task_id or not project_id or not selected_date:
        return api_response(400, "task_id, project_id, and date are required")
    
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    
    try:
        role_context = get_role_context(cursor, int(logged_in_user_id))
        if not can_access_task_eod_report(role_context):
            return api_response(403, "Only Assistant Manager, Project Manager, and Admin can access Task EOD Report")

        cursor.execute(
            "SELECT task_id, task_name, important_columns, task_target FROM task WHERE task_id = %s",
            (task_id,)
        )
        task = cursor.fetchone()
        
        if not task:
            return api_response(404, "Task not found")
        
        import json
        import io
        import pandas as pd
        import requests
        important_columns = json.loads(task.get('important_columns') or '[]')
        
        query = """
            SELECT 
                twt.tracker_id,
                twt.project_id,
                twt.task_id,
                twt.user_id,
                DATE(twt.date_time) as work_date,
                twt.production,
                twt.actual_target,
                twt.tenure_target,
                twt.tracker_file,
                twt.tracker_note,
                twt.shift,
                p.project_name,
                p.project_code,
                t.task_name,
                t.task_target,
                usr.user_name,
                twt.date_time
            FROM task_work_tracker twt
            JOIN project p ON twt.project_id = p.project_id
            JOIN task t ON twt.task_id = t.task_id
            JOIN tfs_user usr ON twt.user_id = usr.user_id
            WHERE twt.task_id = %s
                AND twt.project_id = %s
                AND DATE(twt.date_time) = %s
                AND twt.is_active = 1
                AND twt.actual_target IS NOT NULL
                AND twt.actual_target != ''
                AND twt.tenure_target IS NOT NULL
                AND twt.tenure_target != ''
                AND twt.tenure_target != '0'
                AND twt.tracker_file IS NOT NULL
                AND twt.tracker_file != ''
            ORDER BY twt.date_time DESC
        """
        
        cursor.execute(query, (task_id, project_id, selected_date))
        trackers = cursor.fetchall()
        
        if not trackers:
            return api_response(404, "No valid tracker entries found for this task on the specified date")

        valid_trackers = trackers

        def is_blank(value):
            return value is None or str(value).strip() == ""

        cursor.execute(
            """
            SELECT
                twt.tracker_id,
                twt.project_id,
                twt.task_id,
                twt.user_id,
                usr.user_name,
                twt.date_time,
                twt.production,
                twt.actual_target,
                twt.tenure_target,
                twt.shift,
                twt.tracker_file,
                twt.is_active
            FROM task_work_tracker twt
            JOIN tfs_user usr ON twt.user_id = usr.user_id
            WHERE twt.task_id = %s
              AND twt.project_id = %s
              AND DATE(twt.date_time) = %s
              AND twt.tracker_file IS NOT NULL
              AND twt.tracker_file != ''
            ORDER BY twt.date_time DESC
            """,
            (task_id, project_id, selected_date),
        )
        trackers_with_file = cursor.fetchall() or []

        tracker_list_rows = []
        for tracker in trackers_with_file:
            reasons = []
            if int(tracker.get("is_active") or 0) != 1:
                reasons.append("inactive")

            if is_blank(tracker.get("actual_target")):
                reasons.append("actual_target_missing")

            tenure_target = tracker.get("tenure_target")
            if is_blank(tenure_target):
                reasons.append("tenure_target_missing")
            elif str(tenure_target).strip() == "0":
                reasons.append("tenure_target_zero")

            tracker_file = tracker.get("tracker_file")
            if is_blank(tracker_file):
                reasons.append("tracker_file_missing")
            else:
                file_reason = check_cloudinary_file_status(tracker_file)
                if file_reason:
                    reasons.append(file_reason)

            date_time_value = tracker.get("date_time")
            date_time_str = ""
            date_time_display = ""
            if hasattr(date_time_value, "strftime"):
                date_time_str = date_time_value.strftime("%Y-%m-%d %H:%M:%S")
                date_time_display = date_time_value.strftime("%m-%d-%Y %I:%M:%S %p")

            tracker_list_rows.append(
                {
                    "tracker_id": tracker.get("tracker_id"),
                    "project_id": tracker.get("project_id"),
                    "task_id": tracker.get("task_id"),
                    "user_id": tracker.get("user_id"),
                    "user_name": tracker.get("user_name") or "-",
                    "date_time": date_time_str,
                    "date_time_display": date_time_display or date_time_str,
                    "production": tracker.get("production"),
                    "actual_target": tracker.get("actual_target"),
                    "tenure_target": tracker.get("tenure_target"),
                    "shift": tracker.get("shift"),
                    "tracker_file": tracker.get("tracker_file"),
                    "status": "rejected" if reasons else "accepted",
                    "reason": ", ".join(reasons),
                }
            )

        all_dataframes = []
        failed_files = []

        import time

        def _download_with_retry(url: str, attempts: int = 3, timeout: int = 30) -> bytes:
            last_err = None
            for attempt in range(1, attempts + 1):
                try:
                    resp = requests.get(url, timeout=timeout)
                    resp.raise_for_status()
                    return resp.content
                except Exception as e:
                    last_err = e
                    if attempt < attempts:
                        # brief backoff for transient Cloudinary/network issues
                        time.sleep(2 * attempt)
            raise last_err
        
        for tracker in valid_trackers:
            file_url = tracker['tracker_file']
            if not file_url:
                continue

            # If the stored Cloudinary URL is missing/unreachable, fail early with a clear reason
            file_reason = check_cloudinary_file_status(file_url)
            if file_reason:
                failed_files.append(
                    {
                        "tracker_id": tracker.get("tracker_id"),
                        "user_id": tracker.get("user_id"),
                        "user_name": tracker.get("user_name"),
                        "file_url": file_url,
                        "error": file_reason,
                    }
                )
                continue
            
            try:
                # Download file from URL (retry to reduce transient failures)
                file_bytes = _download_with_retry(file_url, attempts=3, timeout=30)
                
                # Read file based on content type or extension
                file_ext = file_url.split('.')[-1].lower() if '.' in file_url else ''
                
                if file_ext == 'csv':
                    df = pd.read_csv(io.BytesIO(file_bytes))
                elif file_ext in ['xlsx', 'xls']:
                    df = pd.read_excel(io.BytesIO(file_bytes))
                else:
                    # Try to detect format
                    try:
                        df = pd.read_excel(io.BytesIO(file_bytes))
                    except:
                        df = pd.read_csv(io.BytesIO(file_bytes))

                # Normalize headers so NBSP / unicode spaces match across agent files
                df = normalize_eod_dataframe_columns(df)
                
                # Add metadata columns
                df['user_id'] = tracker['user_id']
                df['user_name'] = tracker['user_name']
                df['work_date'] = tracker['date_time']
                
                all_dataframes.append(df)
                
            except Exception as e:
                logger.exception(
                    "EOD report file download/process failed | tracker_id=%s user_id=%s file_url=%s error=%s",
                    tracker.get("tracker_id"),
                    tracker.get("user_id"),
                    file_url,
                    str(e),
                )
                failed_files.append(
                    {
                        "tracker_id": tracker.get("tracker_id"),
                        "user_id": tracker.get("user_id"),
                        "user_name": tracker.get("user_name"),
                        "file_url": file_url,
                        "error": str(e),
                    }
                )

        # IMPORTANT: do not generate partial reports; either all files are processed or we fail.
        if failed_files:
            return api_response(
                400,
                "Some tracker files were not found/reachable on Cloudinary. Report not generated. Please re-upload and retry.",
                {"failed_files": failed_files},
            )
        
        if not all_dataframes:
            return api_response(500, "No valid files could be processed")
        
        # Merge all dataframes
        merged_df = pd.concat(all_dataframes, ignore_index=True)
        # Safety: normalize again in case any frame was added without normalization
        merged_df = normalize_eod_dataframe_columns(merged_df)
        
        # Deduplicate based on important_columns
        if important_columns:
            # Normalize important_columns the same way as file headers
            normalized_important = [normalize_eod_column_name(c) for c in important_columns]
            # Filter to only include columns that exist in the dataframe
            existing_important_cols = [col for col in normalized_important if col in merged_df.columns]
            
            if existing_important_cols:
                # Drop duplicates based on important columns, keeping the first occurrence
                merged_df = merged_df.drop_duplicates(subset=existing_important_cols, keep='first')

        # Keep date columns readable in the exported workbook.
        for column in merged_df.columns:
            series = merged_df[column]
            column_name = str(column).strip().lower()

            include_time = column_name == 'work_date'

            if pd.api.types.is_datetime64_any_dtype(series):
                date_format = '%m-%d-%Y %I:%M:%S %p' if include_time else '%m-%d-%Y'
                merged_df[column] = series.dt.strftime(date_format)
                continue

            if 'date' not in column_name:
                continue

            parsed_series = pd.to_datetime(series, errors='coerce')
            if parsed_series.notna().sum() == 0:
                continue

            date_format = '%m-%d-%Y %I:%M:%S %p' if include_time else '%m-%d-%Y'
            merged_df[column] = parsed_series.dt.strftime(date_format).where(parsed_series.notna(), series)
        
        output = io.BytesIO()
        with pd.ExcelWriter(output, engine='openpyxl') as writer:
            merged_df.to_excel(writer, index=False, sheet_name='Consolidated Report')
            trackers_df = pd.DataFrame(tracker_list_rows)
            if not trackers_df.empty:
                ordered_columns = [
                    "tracker_id",
                    "project_id",
                    "task_id",
                    "user_id",
                    "user_name",
                    "date_time_display",
                    "date_time",
                    "production",
                    "actual_target",
                    "tenure_target",
                    "shift",
                    "status",
                    "reason",
                    "tracker_file",
                ]
                existing_columns = [col for col in ordered_columns if col in trackers_df.columns]
                remaining_columns = [col for col in trackers_df.columns if col not in existing_columns]
                trackers_df = trackers_df[existing_columns + remaining_columns]
            trackers_df.to_excel(writer, index=False, sheet_name='Tracker List')
        
        output.seek(0)

        formatted_selected_date = selected_date
        try:
            formatted_selected_date = datetime.strptime(selected_date, "%Y-%m-%d").strftime("%m-%d-%Y")
        except Exception:
            pass
        
        # Return file as response
        from flask import send_file
        return send_file(
            output,
            as_attachment=True,
            download_name=f"EOD_Report_{task['task_name']}_{formatted_selected_date}.xlsx",
            mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
        )
        
    except Exception as e:
        return api_response(500, f"Failed to generate EOD report: {str(e)}")
    
    finally:
        cursor.close()
        conn.close()
