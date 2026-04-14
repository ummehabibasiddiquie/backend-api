from flask import Blueprint, request
from utils.response import api_response
from config import get_db_connection
from utils.cloudinary_utils import upload_to_cloudinary, FOLDER_QC_REWORK
from datetime import datetime

qc_rework_bp = Blueprint("qc_rework", __name__)

# =========================================================
# ✅ ADD REWORK FILE API
# =========================================================
@qc_rework_bp.route("/add_rework_file", methods=["POST"])
def add_rework_file():
    form = request.form
    qc_record_id = form.get("qc_record_id")

    if not qc_record_id:
        return api_response(400, "qc_record_id is required")

    uploaded_file = request.files.get("rework_file_path")
    if not uploaded_file or not uploaded_file.filename:
        return api_response(400, "rework_file is required")

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    try:
        # -------------------------------------------------
        # 🔹 Get project, task, user details
        # -------------------------------------------------
        cursor.execute("""
            SELECT 
                p.project_code, 
                t.task_name, 
                u.user_name
            FROM qc_records qr
            JOIN task_work_tracker twt 
                ON qr.tracker_id = twt.tracker_id
            JOIN project p 
                ON twt.project_id = p.project_id
            JOIN task t 
                ON twt.task_id = t.task_id
            JOIN tfs_user u 
                ON twt.user_id = u.user_id
            WHERE qr.qc_record_id = %s
        """, (qc_record_id,))
        info = cursor.fetchone()

        if not info:
            return api_response(404, "QC record not found")

        # -------------------------------------------------
        # 🔹 Generate filename
        # -------------------------------------------------
        now = datetime.now()
        date_part = now.strftime("%d-%b-%Y")
        time_part = now.strftime("%I%p")

        ext = uploaded_file.filename.rsplit('.', 1)[1].lower() if '.' in uploaded_file.filename else 'file'

        clean_project = "".join(c if c.isalnum() else "_" for c in info["project_code"])
        clean_task = "".join(c if c.isalnum() else "_" for c in info["task_name"])
        clean_user = "".join(c if c.isalnum() else "_" for c in info["user_name"])

        filename = f"{clean_project}_{clean_task}_{clean_user}_{date_part}_{time_part}_rework.{ext}"

        # -------------------------------------------------
        # 🔹 Upload to Cloudinary
        # -------------------------------------------------
        cloudinary_url, _ = upload_to_cloudinary(
            uploaded_file,
            FOLDER_QC_REWORK,
            display_name=filename,
            resource_type="raw"
        )

        # -------------------------------------------------
        # 🔹 Check if record exists
        # -------------------------------------------------
        cursor.execute("""
            SELECT qc_rework_id, rework_count 
            FROM qc_rework_history 
            WHERE qc_record_id = %s
        """, (qc_record_id,))
        existing = cursor.fetchone()

        if existing:
            # 🔹 UPDATE existing
            cursor.execute("""
                UPDATE qc_rework_history
                SET 
                    rework_file_path = %s,
                    rework_count = COALESCE(rework_count, 0) + 1,
                    rework_status = 'completed',
                    updated_at = NOW()
                WHERE qc_record_id = %s
            """, (cloudinary_url, qc_record_id))

        else:
            # 🔹 INSERT new record
            cursor.execute("""
                INSERT INTO qc_rework_history (
                    qc_record_id,
                    rework_file_path,
                    rework_count,
                    rework_status,
                    rework_qc_score,
                    created_at,
                    updated_at
                )
                VALUES (%s, %s, 1, 'completed', 0, NOW(), NOW())
            """, (qc_record_id, cloudinary_url))

        conn.commit()

        return api_response(200, "Rework file uploaded successfully", {
            "rework_file_path": cloudinary_url
        })

    except Exception as e:
        conn.rollback()
        return api_response(500, f"Error: {str(e)}")

    finally:
        cursor.close()
        conn.close()


@qc_rework_bp.route("/view_all_qc_history", methods=["POST"])
def view_all_qc_history():
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    try:
        # 1️⃣ Fetch main qc_records with metadata
        query_qc_records = """
        SELECT
            qr.id AS qc_record_id,
            u.user_name AS agent_name,
            p.project_name,
            t.task_name,
            qr.qc_score,
            qr.status,
            qr.qc_status,
            qr.qc_file_path,
            qr.whole_file_path,
            qr.date_of_file_submission,
            qr.created_at,
            qr.updated_at,
            qr.error_list,
            qr.file_record_count,
            qr.qc_generated_count
        FROM qc_records qr
        LEFT JOIN task_work_tracker twt ON qr.tracker_id = twt.tracker_id
        LEFT JOIN tfs_user u ON u.user_id = twt.user_id
        LEFT JOIN project p ON p.project_id = twt.project_id
        LEFT JOIN task t ON t.task_id = twt.task_id
        ORDER BY qr.id DESC
        """
        cursor.execute(query_qc_records)
        qc_records = cursor.fetchall()

        if not qc_records:
            return api_response(200, "No QC records found", {"count": 0, "records": []})

        qc_record_ids = [r["qc_record_id"] for r in qc_records]

        # 2️⃣ Fetch related reworks
        query_reworks = f"""
        SELECT 
            *,
            rework_status as review_status,
            qc_data_generated_count
        FROM qc_rework_history
        WHERE qc_record_id IN ({','.join(map(str, qc_record_ids))})
        ORDER BY qc_rework_id DESC
        """
        cursor.execute(query_reworks)
        reworks = cursor.fetchall()

        # 3️⃣ Fetch related corrections
        query_corrections = f"""
        SELECT 
            *,
            correction_status as review_status
        FROM qc_correction_history
        WHERE qc_record_id IN ({','.join(map(str, qc_record_ids))})
        ORDER BY qc_correction_id DESC
        """
        cursor.execute(query_corrections)
        corrections = cursor.fetchall()

        # 4️⃣ Merge data
        rework_map = {}
        for r in reworks:
            rework_map.setdefault(r["qc_record_id"], []).append(r)

        correction_map = {}
        for c in corrections:
            correction_map.setdefault(c["qc_record_id"], []).append(c)

        final_data = []
        for record in qc_records:
            record_id = record["qc_record_id"]
            record["qc_rework"] = rework_map.get(record_id, [])
            record["qc_correction"] = correction_map.get(record_id, [])
            final_data.append(record)

        return api_response(
            200,
            "QC full history fetched successfully",
            {"count": len(final_data), "records": final_data}
        )

    except Exception as e:
        return api_response(500, f"Error: {str(e)}")

    finally:
        cursor.close()
        conn.close()

@qc_rework_bp.route("/view_pending_qc_files", methods=["POST"])
def view_pending_qc_dashboard():
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    try:
        # 1️⃣ Base QC records with metadata
        cursor.execute("""
            SELECT
                qr.id AS qc_record_id,
                qr.tracker_id,
                u.user_name AS agent_name,
                u.user_id AS agent_id,
                p.project_name,
                p.project_id,
                pc.project_category_id,
                pc.project_category_name,
                t.task_name,
                t.task_id,
                t.qc_percentage AS sampling_percentage,
                qr.qc_score,
                qr.error_list
            FROM qc_records qr
            LEFT JOIN task_work_tracker twt ON qr.tracker_id = twt.tracker_id
            LEFT JOIN tfs_user u ON u.user_id = twt.user_id
            LEFT JOIN project p ON p.project_id = twt.project_id
            LEFT JOIN project_category pc ON p.project_category_id = pc.project_category_id
            LEFT JOIN task t ON t.task_id = twt.task_id
            ORDER BY qr.id DESC
        """)
        qc_records = cursor.fetchall()

        if not qc_records:
            return api_response(200, "No QC records found", {
                "count": 0,
                "record": []
            })

        qc_ids = [str(r["qc_record_id"]) for r in qc_records]

        # 2️⃣ Latest pending REWORK
        cursor.execute(f"""
            SELECT r1.*
            FROM qc_rework_history r1
            WHERE r1.rework_file_qc_status = 'pending'
            AND r1.qc_rework_id = (
                SELECT MAX(r2.qc_rework_id)
                FROM qc_rework_history r2
                WHERE r2.qc_record_id = r1.qc_record_id
                AND r2.rework_file_qc_status = 'pending'
            )
            AND r1.qc_record_id IN ({','.join(qc_ids)})
        """)
        reworks = cursor.fetchall()

        # 3️⃣ Latest pending CORRECTION
        cursor.execute(f"""
            SELECT c1.*
            FROM qc_correction_history c1
            WHERE c1.correction_file_qc_status = 'pending'
            AND c1.qc_correction_id = (
                SELECT MAX(c2.qc_correction_id)
                FROM qc_correction_history c2
                WHERE c2.qc_record_id = c1.qc_record_id
                AND c2.correction_file_qc_status = 'pending'
            )
            AND c1.qc_record_id IN ({','.join(qc_ids)})
        """)
        corrections = cursor.fetchall()

        # 4️⃣ Maps
        rework_map = {r["qc_record_id"]: r for r in reworks}
        correction_map = {c["qc_record_id"]: c for c in corrections}

        records = []

        # 5️⃣ Loop
        for qc in qc_records:
            rid = qc["qc_record_id"]

            latest_rework = rework_map.get(rid)
            latest_correction = correction_map.get(rid)

            if not latest_rework and not latest_correction:
                continue

            # =========================
            # 🔁 PREVIOUS REWORK
            # =========================
            prev_rework_score = None
            prev_rework_errors = None

            if latest_rework:
                cursor.execute("""
                    SELECT rework_qc_score, rework_error_list
                    FROM qc_rework_history
                    WHERE qc_record_id = %s
                    AND qc_rework_id < %s
                    ORDER BY qc_rework_id DESC
                    LIMIT 1
                """, (rid, latest_rework["qc_rework_id"]))

                prev = cursor.fetchone()

                if prev:
                    prev_rework_score = prev["rework_qc_score"]
                    prev_rework_errors = prev["rework_error_list"]
                else:
                    prev_rework_score = qc["qc_score"]
                    prev_rework_errors = qc["error_list"]

            # =========================
            # 🔁 PREVIOUS CORRECTION
            # =========================
            prev_corr_score = qc["qc_score"]
            prev_corr_errors = None

            if latest_correction:
                cursor.execute("""
                    SELECT correction_error_list
                    FROM qc_correction_history
                    WHERE qc_record_id = %s
                    AND qc_correction_id < %s
                    ORDER BY qc_correction_id DESC
                    LIMIT 1
                """, (rid, latest_correction["qc_correction_id"]))

                prev = cursor.fetchone()

                if prev:
                    prev_corr_errors = prev["correction_error_list"]
                else:
                    prev_corr_errors = qc["error_list"]

            # 6️⃣ Final object
            records.append({
                "agent_name": qc["agent_name"],
                "agent_id": qc["agent_id"],
                "project_name": qc["project_name"],
                "project_id": qc["project_id"],
                "project_category_id": qc["project_category_id"],
                "project_category_name": qc["project_category_name"],
                "task_name": qc["task_name"],
                "task_id": qc["task_id"],
                "tracker_id": qc["tracker_id"],
                "sampling_percentage": qc["sampling_percentage"],

                "latest_rework": {
                    **latest_rework,
                    "previous_qc_score": prev_rework_score,
                    "previous_error_list": prev_rework_errors
                } if latest_rework else {},

                "latest_correction": {
                    **latest_correction,
                    "previous_qc_score": prev_corr_score,
                    "previous_error_list": prev_corr_errors
                } if latest_correction else {}
            })

        return api_response(
            200,
            "Pending QC dashboard fetched successfully",
            {
                "count": len(records),
                "record": records
            }
        )

    except Exception as e:
        return api_response(500, f"Error: {str(e)}")

    finally:
        cursor.close()
        conn.close()