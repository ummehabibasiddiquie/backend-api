from flask import Blueprint, request, jsonify
from datetime import datetime
from config import get_db_connection

qc_bp = Blueprint("qc", __name__)

QC_DATE_COL = "date"  # change if your column name is different

def response(status, message, data=None, code=200):
    return jsonify({"status": status, "message": message, "data": data}), code

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