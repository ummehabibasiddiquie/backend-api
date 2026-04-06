# routes/project_category.py

from flask import Blueprint, request
from utils.response import api_response
from config import get_db_connection
from datetime import datetime

project_category_bp = Blueprint("project_category", __name__)

# ---------------- CREATE PROJECT CATEGORY ---------------- #
@project_category_bp.route("/create", methods=["POST"])
def create_project_category():
    data = request.get_json(silent=True) or {}

    project_category_name = (data.get("project_category_name") or "").strip()
    if not project_category_name:
        return api_response(400, "project_category_name is required")

    afd_id = data.get("afd_id")

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    try:
        # Check if category already exists
        cursor.execute(
            "SELECT project_category_id FROM project_category WHERE LOWER(project_category_name) = LOWER(%s) AND is_active = 1",
            (project_category_name,)
        )
        if cursor.fetchone():
            return api_response(400, "Project category already exists")

        cursor.execute(
            """
            INSERT INTO project_category (project_category_name, afd_id, created_date, updated_date, is_active)
            VALUES (%s, %s, %s, %s, 1)
            """,
            (project_category_name, afd_id, now_str, now_str)
        )
        conn.commit()

        return api_response(201, "Project category created successfully", {
            "project_category_id": cursor.lastrowid
        })

    except Exception as e:
        conn.rollback()
        return api_response(500, f"Project category creation failed: {str(e)}")
    finally:
        cursor.close()
        conn.close()


# ---------------- UPDATE PROJECT CATEGORY ---------------- #
@project_category_bp.route("/update", methods=["POST"])
def update_project_category():
    data = request.get_json(silent=True) or {}

    project_category_id = data.get("project_category_id")
    if not project_category_id:
        return api_response(400, "project_category_id is required")

    project_category_name = (data.get("project_category_name") or "").strip()
    if not project_category_name:
        return api_response(400, "project_category_name is required")

    afd_id = data.get("afd_id")

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    updated_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    try:
        # Check if category exists
        cursor.execute(
            "SELECT project_category_id FROM project_category WHERE project_category_id = %s AND is_active = 1",
            (project_category_id,)
        )
        if not cursor.fetchone():
            return api_response(404, "Project category not found")

        # Check if another category with same name exists
        cursor.execute(
            """
            SELECT project_category_id FROM project_category 
            WHERE LOWER(project_category_name) = LOWER(%s) AND project_category_id != %s AND is_active = 1
            """,
            (project_category_name, project_category_id)
        )
        if cursor.fetchone():
            return api_response(400, "Another project category with this name already exists")

        cursor.execute(
            """
            UPDATE project_category 
            SET project_category_name = %s, afd_id = %s, updated_date = %s 
            WHERE project_category_id = %s
            """,
            (project_category_name, afd_id, updated_str, project_category_id)
        )
        conn.commit()

        return api_response(200, "Project category updated successfully")

    except Exception as e:
        conn.rollback()
        return api_response(500, f"Project category update failed: {str(e)}")
    finally:
        cursor.close()
        conn.close()


# ---------------- DELETE PROJECT CATEGORY (soft delete) ---------------- #
@project_category_bp.route("/delete", methods=["POST"])
def delete_project_category():
    data = request.get_json(silent=True) or {}

    project_category_id = data.get("project_category_id")
    if not project_category_id:
        return api_response(400, "project_category_id is required")

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    updated_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    try:
        # Check if category exists
        cursor.execute(
            "SELECT project_category_id FROM project_category WHERE project_category_id = %s AND is_active = 1",
            (project_category_id,)
        )
        if not cursor.fetchone():
            return api_response(404, "Project category not found or already deleted")

        cursor.execute(
            "UPDATE project_category SET is_active = 0, updated_date = %s WHERE project_category_id = %s",
            (updated_str, project_category_id)
        )
        conn.commit()

        return api_response(200, "Project category deleted successfully")

    except Exception as e:
        conn.rollback()
        return api_response(500, f"Project category deletion failed: {str(e)}")
    finally:
        cursor.close()
        conn.close()


# ---------------- LIST PROJECT CATEGORIES ---------------- #
@project_category_bp.route("/list", methods=["POST"])
def list_project_categories():
    data = request.get_json(silent=True) or {}
    project_category_id = data.get("project_category_id")

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    try:
        query = """
            SELECT 
                pc.project_category_id,
                pc.project_category_name,

                a.afd_id,
                a.afd_name,

                q.qc_afd_id,
                q.afd_name AS qc_afd_name,
                q.afd_points,
                q.afd_category_id

            FROM project_category pc
            LEFT JOIN afd a 
                ON a.afd_id = pc.afd_id 
                AND a.is_active = 1

            LEFT JOIN qc_afd q 
                ON q.afd_id = a.afd_id

            WHERE pc.is_active = 1
        """

        params = []

        # 🔥 Project Category Filter
        if project_category_id:
            query += " AND pc.project_category_id = %s"
            params.append(project_category_id)

        query += " ORDER BY pc.project_category_name, a.afd_name, q.qc_afd_id"

        cursor.execute(query, params)
        rows = cursor.fetchall()

        if not rows:
            return api_response(200, "No data found", [])

        # -----------------------------
        # Build Nested Structure
        # -----------------------------
        result = {}

        for row in rows:
            pc_id = row["project_category_id"]
            afd_id = row["afd_id"]
            qc_id = row["qc_afd_id"]

            if pc_id not in result:
                result[pc_id] = {
                    "project_category_id": pc_id,
                    "project_category_name": row["project_category_name"],
                    "afd": {}
                }

            # Only process AFD data if afd_id exists
            if afd_id is not None and afd_id not in result[pc_id]["afd"]:
                result[pc_id]["afd"][afd_id] = {
                    "afd_id": afd_id,
                    "afd_name": row["afd_name"],
                    "afd_categories": {}
                }

            # Only process QC data if qc_id exists
            if qc_id and afd_id is not None:
                qc_data = {
                    "qc_afd_id": qc_id,
                    "qc_afd_name": row["qc_afd_name"],
                    "afd_points": row["afd_points"],
                    "afd_sub_categories": []
                }

                # Main Category
                if row["afd_category_id"] == 0:
                    result[pc_id]["afd"][afd_id]["afd_categories"][qc_id] = qc_data
                else:
                    parent_id = row["afd_category_id"]
                    if parent_id in result[pc_id]["afd"][afd_id]["afd_categories"]:
                        result[pc_id]["afd"][afd_id]["afd_categories"][parent_id]["afd_sub_categories"].append(qc_data)

        # Convert dict to list
        final_result = []
        for pc in result.values():
            afd_list = []
            for afd in pc["afd"].values():
                afd["afd_categories"] = list(afd["afd_categories"].values())
                afd_list.append(afd)

            pc["afd"] = afd_list
            final_result.append(pc)

        return api_response(200, "Data fetched successfully", final_result)

    except Exception as e:
        return api_response(500, f"Failed to fetch data: {str(e)}")

    finally:
        cursor.close()
        conn.close()