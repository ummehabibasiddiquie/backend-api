from flask import Blueprint, request
from config import get_db_connection
from utils.response import api_response

qc_history_user_bp = Blueprint("qc_history_user", __name__)


@qc_history_user_bp.route("/view_qc_history_user_based", methods=["POST"])
def view_qc_history_user_based():
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    try:
        data = request.json
        logged_in_user_id = data.get("logged_in_user_id")

        if not logged_in_user_id:
            return api_response(400, "logged_in_user_id is required")

        # ✅ 1. Get role
        cursor.execute("""
            SELECT ur.role_name
            FROM tfs_user u
            JOIN user_role ur ON u.role_id = ur.role_id
            WHERE u.user_id = %s
        """, (logged_in_user_id,))
        user = cursor.fetchone()

        if not user:
            return api_response(404, "User not found")

        role = user["role_name"].strip().lower()

        # 2. Base Query
        base_query = """
        SELECT
            qr.*,
            u.user_name AS agent_name,
            u.team_id AS user_team_id,
            t.team_name,
            p.project_name,
            task.task_name,
            qa.user_name AS qa_agent_name
        FROM qc_records qr
        LEFT JOIN task_work_tracker twt ON qr.tracker_id = twt.tracker_id
        LEFT JOIN tfs_user u ON u.user_id = twt.user_id
        LEFT JOIN team t ON u.team_id = t.team_id
        LEFT JOIN project p ON p.project_id = twt.project_id
        LEFT JOIN task task ON task.task_id = twt.task_id
        LEFT JOIN tfs_user qa ON qa.user_id = qr.qa_user_id
        """

        params = []

        # 3. Role-based filtering (JSON ARRAY SUPPORT)
        if "admin" in role:
            pass

        elif "project manager" in role:
            base_query += """
            WHERE (
                JSON_CONTAINS(u.project_manager_id, %s)
                OR u.user_id = %s
            )
            """
            params.extend([f"{logged_in_user_id}", logged_in_user_id])

        elif "assistant manager" in role:
            base_query += """
            WHERE (
                JSON_CONTAINS(u.asst_manager_id, %s)
                OR u.user_id = %s
            )
            """
            params.extend([f"{logged_in_user_id}", logged_in_user_id])

        elif "qa" in role:
            base_query += """
            WHERE (
                JSON_CONTAINS(u.qa_id, %s)
                OR u.user_id = %s
            )
            """
            params.extend([f"{logged_in_user_id}", logged_in_user_id])

        else:
            base_query += " WHERE u.user_id = %s "
            params.append(logged_in_user_id)

        base_query += " ORDER BY qr.id DESC"

        print("QUERY:", base_query)
        print("PARAMS:", params)

        # ✅ 4. Execute
        cursor.execute(base_query, tuple(params))
        qc_records = cursor.fetchall()

        if not qc_records:
            return api_response(200, "No QC records found", {"count": 0, "records": []})

        qc_record_ids = [r["id"] for r in qc_records]

        # 5. Reworks
        cursor.execute(f"""
            SELECT 
                *,
                rework_status as review_status
            FROM qc_rework_history
            WHERE qc_record_id IN ({','.join(['%s'] * len(qc_record_ids))})
            ORDER BY qc_rework_id DESC
        """, tuple(qc_record_ids))
        reworks = cursor.fetchall()

        # 6. Corrections
        cursor.execute(f"""
            SELECT 
                *,
                correction_status as review_status
            FROM qc_correction_history
            WHERE qc_record_id IN ({','.join(['%s'] * len(qc_record_ids))})
            ORDER BY qc_correction_id DESC
        """, tuple(qc_record_ids))
        corrections = cursor.fetchall()

        # 7. Mapping
        rework_map = {}
        for r in reworks:
            rework_map.setdefault(r["qc_record_id"], []).append(r)

        correction_map = {}
        for c in corrections:
            correction_map.setdefault(c["qc_record_id"], []).append(c)

        # 8. Merge
        final_data = []
        for record in qc_records:
            record["qc_rework"] = rework_map.get(record["id"], [])
            record["qc_correction"] = correction_map.get(record["id"], [])
            final_data.append(record)

        return api_response(
            200,
            "QC history fetched successfully",
            {
                "count": len(final_data),
                "records": final_data
            }
        )

    except Exception as e:
        return api_response(500, f"Error: {str(e)}")

    finally:
        cursor.close()
        conn.close()