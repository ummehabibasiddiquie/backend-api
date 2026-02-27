from flask import Blueprint, request, jsonify
from datetime import datetime
from config import get_db_connection

qc_bp = Blueprint("qc", __name__)

QC_DATE_COL = "date"  # change if your column name is different

def response(status, message, data=None, code=200):
    return jsonify({"status": status, "message": message, "data": data}), code

# ---------------------------
# DAILY ASSIGNED HOURS (NEW)
# ---------------------------
@qc_bp.route("/assign-daily-hours", methods=["POST"])
def assign_daily_hours():
    """
    Scheduled job endpoint.
    Assigns 9 hours to all active agents for the current day.
    """
    now = datetime.now()
    today_str = now.strftime("%Y-%m-%d")
    now_str = now.strftime("%Y-%m-%d %H:%M:%S")

    conn = None
    cur = None
    try:
        conn = get_db_connection()
        cur = conn.cursor(dictionary=True)

        # 1. Get all active agent user_ids
        cur.execute("""
            SELECT u.user_id
            FROM tfs_user u
            JOIN user_role ur ON u.role_id = ur.role_id
            WHERE ur.role_name = 'agent' AND u.is_active = 1 AND u.is_delete = 1
        """)
        agent_rows = cur.fetchall()
        agent_ids = [row['user_id'] for row in agent_rows]

        if not agent_ids:
            return response(True, "No active agents found to assign hours.", None, 200)

        # 2. Bulk insert/update
        # Use ON DUPLICATE KEY UPDATE to be idempotent
        sql = f"""
            INSERT INTO temp_qc (user_id, assigned_hours, {QC_DATE_COL}, updated_date)
            VALUES (%s, 9, %s, %s)
            ON DUPLICATE KEY UPDATE
                assigned_hours = VALUES(assigned_hours),
                updated_date = VALUES(updated_date)
        """
        
        # Prepare data for executemany
        data_to_insert = [(agent_id, today_str, now_str) for agent_id in agent_ids]
        
        cur.executemany(sql, data_to_insert)
        
        conn.commit()

        return response(True, f"Successfully assigned 9 hours to {cur.rowcount} agents for {today_str}.", None, 200)

    except Exception as e:
        if conn:
            conn.rollback()
        return response(False, f"An error occurred: {str(e)}", None, 500)
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()


# ---------------------------
# UPSERT (EXISTING)
# ---------------------------
@qc_bp.route("/temp-qc", methods=["POST"])
def upsert_temp_qc():
    data = request.get_json(silent=True) or {}

    user_id = data.get("user_id")
    qc_date = (data.get("date") or "").strip()  # YYYY-MM-DD

    # OPTIONAL fields (can come separately)
    qc_score = data.get("qc_score")            # can be missing
    assigned_hours = data.get("assigned_hours") # can be missing

    if not user_id:
        return response(False, "user_id is required", None, 400)

    if not qc_date:
        return response(False, "date is required (YYYY-MM-DD)", None, 400)

    try:
        datetime.strptime(qc_date, "%Y-%m-%d")
    except ValueError:
        return response(False, "Invalid date format. Use YYYY-MM-DD", None, 400)

    # At least one of qc_score/assigned_hours should be provided
    if qc_score is None and assigned_hours is None:
        return response(False, "Provide qc_score or assigned_hours (at least one).", None, 400)

    updated_date = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    conn = None
    cur = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()

        sql = f"""
            INSERT INTO temp_qc (user_id, qc_score, assigned_hours, {QC_DATE_COL}, updated_date)
            VALUES (%s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                qc_score = COALESCE(VALUES(qc_score), qc_score),
                assigned_hours = COALESCE(VALUES(assigned_hours), assigned_hours),
                updated_date = VALUES(updated_date)
        """

        cur.execute(sql, (user_id, qc_score, assigned_hours, qc_date, updated_date))
        conn.commit()

        return response(True, "QC saved successfully", {"user_id": user_id, "date": qc_date}, 200)

    except Exception as e:
        if conn:
            conn.rollback()
        return response(False, f"QC save failed: {str(e)}", None, 500)

    finally:
        try:
            if cur: cur.close()
            if conn: conn.close()
        except:
            pass