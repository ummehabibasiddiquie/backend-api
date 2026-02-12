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
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    try:
        cursor.execute(
            """
            SELECT project_category_id, project_category_name, afd_id, created_date, updated_date
            FROM project_category
            WHERE is_active = 1
            ORDER BY project_category_name
            """
        )
        result = cursor.fetchall()

        return api_response(200, "Project categories fetched successfully", result)

    except Exception as e:
        return api_response(500, f"Failed to fetch project categories: {str(e)}")
    finally:
        cursor.close()
        conn.close()