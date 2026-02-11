from flask import Blueprint, request
from config import get_db_connection
from utils.response import api_response
from datetime import datetime

project_monthly_tracker_bp = Blueprint("project_monthly_tracker",__name__)

def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def validate_required(data: dict, fields: list[str]) -> str | None:
    for f in fields:
        if data.get(f) in [None, ""]:
            return f"{f} is required"
    return None

def project_exists(cursor, project_id: int) -> bool:
    cursor.execute(
        "SELECT project_id FROM project WHERE project_id=%s AND is_active=1",
        (project_id,)
    )
    return cursor.fetchone() is not None


# -----------------------------
# ADD
# -----------------------------
@project_monthly_tracker_bp.route("/add", methods=["POST"])
def add_project_monthly_tracker():
    data = request.get_json() or {}

    err = validate_required(data, ["project_id", "month_year", "monthly_target"])
    if err:
        return api_response(400, err)

    project_id = int(data["project_id"])
    month_year = str(data["month_year"]).strip()
    monthly_target = str(data["monthly_target"]).strip()
    created_date = str(data.get("created_date") or now_str())

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    try:
        if not project_exists(cursor, project_id):
            return api_response(404, "Project not found or inactive")

        # prevent duplicate active rows for same project+month
        cursor.execute(
            """
            SELECT project_monthly_tracker_id
            FROM project_monthly_tracker
            WHERE project_id=%s AND month_year=%s AND is_active=1
            """,
            (project_id, month_year)
        )
        if cursor.fetchone():
            return api_response(409, "Monthly target for this project and month already exists")

        cursor.execute(
            """
            INSERT INTO project_monthly_tracker
                (project_id, month_year, monthly_target, created_date, is_active)
            VALUES (%s, %s, %s, %s, 1)
            """,
            (project_id, month_year, monthly_target, created_date)
        )
        conn.commit()

        return api_response(201, "Project monthly target added successfully", {
            "project_monthly_tracker_id": cursor.lastrowid
        })

    except Exception as e:
        conn.rollback()
        return api_response(500, f"Add failed: {str(e)}")
    finally:
        cursor.close()
        conn.close()


# -----------------------------
# UPDATE
# -----------------------------
@project_monthly_tracker_bp.route("/update", methods=["POST"])
def update_project_monthly_tracker():
    data = request.get_json() or {}

    err = validate_required(data, ["project_monthly_tracker_id"])
    if err:
        return api_response(400, err)

    pm_id = int(data["project_monthly_tracker_id"])

    updates = []
    params = []

    if "project_id" in data and data["project_id"] not in [None, ""]:
        updates.append("project_id=%s")
        params.append(int(data["project_id"]))

    if "month_year" in data and data["month_year"] not in [None, ""]:
        updates.append("month_year=%s")
        params.append(str(data["month_year"]).strip())

    if "monthly_target" in data and data["monthly_target"] not in [None, ""]:
        updates.append("monthly_target=%s")
        params.append(str(data["monthly_target"]).strip())

    if "created_date" in data and data["created_date"] not in [None, ""]:
        updates.append("created_date=%s")
        params.append(str(data["created_date"]).strip())

    # optional
    if "is_active" in data and data["is_active"] in [0, 1, "0", "1"]:
        updates.append("is_active=%s")
        params.append(int(data["is_active"]))

    if not updates:
        return api_response(400, "No fields provided to update")

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    try:
        cursor.execute(
            """
            SELECT project_id, month_year
            FROM project_monthly_tracker
            WHERE project_monthly_tracker_id=%s
            """,
            (pm_id,)
        )
        current = cursor.fetchone()
        if not current:
            return api_response(404, "Record not found")

        # validate project if updating it
        if "project_id" in data and data["project_id"] not in [None, ""]:
            if not project_exists(cursor, int(data["project_id"])):
                return api_response(404, "Project not found or inactive")

        # prevent duplicate active rows for final (project_id, month_year)
        if ("project_id" in data and data["project_id"] not in [None, ""]) or ("month_year" in data and data["month_year"] not in [None, ""]):
            final_project_id = int(data["project_id"]) if ("project_id" in data and data["project_id"] not in [None, ""]) else int(current["project_id"])
            final_month_year = str(data["month_year"]).strip() if ("month_year" in data and data["month_year"] not in [None, ""]) else str(current["month_year"])

            cursor.execute(
                """
                SELECT project_monthly_tracker_id
                FROM project_monthly_tracker
                WHERE project_id=%s AND month_year=%s
                  AND is_active=1
                  AND project_monthly_tracker_id<>%s
                """,
                (final_project_id, final_month_year, pm_id)
            )
            if cursor.fetchone():
                return api_response(409, "Monthly target for this project and month already exists")

        params.append(pm_id)
        query = f"""
            UPDATE project_monthly_tracker
            SET {', '.join(updates)}
            WHERE project_monthly_tracker_id=%s
        """
        cursor.execute(query, tuple(params))
        conn.commit()

        return api_response(200, "Project monthly target updated successfully")

    except Exception as e:
        conn.rollback()
        return api_response(500, f"Update failed: {str(e)}")
    finally:
        cursor.close()
        conn.close()


# -----------------------------
# DELETE (SOFT)
# -----------------------------
@project_monthly_tracker_bp.route("/delete", methods=["POST"])
def delete_project_monthly_tracker():
    data = request.get_json() or {}

    err = validate_required(data, ["project_monthly_tracker_id"])
    if err:
        return api_response(400, err)

    pm_id = int(data["project_monthly_tracker_id"])

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    try:
        cursor.execute(
            """
            SELECT project_monthly_tracker_id
            FROM project_monthly_tracker
            WHERE project_monthly_tracker_id=%s AND is_active=1
            """,
            (pm_id,)
        )
        if not cursor.fetchone():
            return api_response(404, "Active record not found")

        cursor.execute(
            """
            UPDATE project_monthly_tracker
            SET is_active=0
            WHERE project_monthly_tracker_id=%s
            """,
            (pm_id,)
        )
        conn.commit()
        return api_response(200, "Project monthly target deleted successfully")

    except Exception as e:
        conn.rollback()
        return api_response(500, f"Delete failed: {str(e)}")
    finally:
        cursor.close()
        conn.close()


# -----------------------------
# LIST (active only, optional filters)
# -----------------------------
@project_monthly_tracker_bp.route("/list", methods=["POST"])
def list_project_monthly_tracker():
    data = request.get_json() or {}

    params = []
    where = "WHERE pmt.is_active=1"

    # ✅ now list can also return one record if id is passed
    if data.get("project_monthly_tracker_id"):
        where += " AND pmt.project_monthly_tracker_id = %s"
        params.append(int(data["project_monthly_tracker_id"]))

    if data.get("project_id"):
        where += " AND pmt.project_id = %s"
        params.append(int(data["project_id"]))

    if data.get("month_year"):
        where += " AND pmt.month_year = %s"
        params.append(str(data["month_year"]).strip())

    if data.get("project_name"):
        where += " AND p.project_name LIKE %s"
        params.append(f"%{data['project_name']}%")

    limit = int(data.get("limit") or 200)
    offset = int(data.get("offset") or 0)

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    try:
        query = f"""
            SELECT
                pmt.project_monthly_tracker_id,
                pmt.project_id,
                p.project_name,
                pmt.month_year,
                pmt.monthly_target,
                pmt.created_date,
                pmt.is_active
            FROM project_monthly_tracker pmt
            LEFT JOIN project p ON p.project_id = pmt.project_id
            {where}
            ORDER BY pmt.project_monthly_tracker_id DESC
            LIMIT %s OFFSET %s
        """
        cursor.execute(query, tuple(params + [limit, offset]))
        rows = cursor.fetchall()

        count_query = f"""
            SELECT COUNT(*) AS total
            FROM project_monthly_tracker pmt
            LEFT JOIN project p ON p.project_id = pmt.project_id
            {where}
        """
        cursor.execute(count_query, tuple(params))
        total = (cursor.fetchone() or {}).get("total", 0)

        return api_response(200, "Records fetched successfully", {
            "total": total,
            "limit": limit,
            "offset": offset,
            "rows": rows
        })

    except Exception as e:
        return api_response(500, f"List failed: {str(e)}")
    finally:
        cursor.close()
        conn.close()
