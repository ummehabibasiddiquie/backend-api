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
# ADD (supports single or bulk insert)
# -----------------------------
@project_monthly_tracker_bp.route("/add", methods=["POST"])
def add_project_monthly_tracker():
    """
    Accepts either:
      - Single object: {"project_id": 1, "month_year": "Feb2026", "monthly_target": "100"}
      - Array of objects: [{"project_id": 1, ...}, {"project_id": 2, ...}]
    """
    raw_data = request.get_json(silent=True)

    # Normalize to list
    if isinstance(raw_data, list):
        records = raw_data
    elif isinstance(raw_data, dict):
        records = [raw_data]
    else:
        return api_response(400, "Invalid JSON payload")

    if not records:
        return api_response(400, "No records provided")

    # Validate all records first
    required_fields = ["project_id", "month_year", "monthly_target"]
    for idx, data in enumerate(records):
        err = validate_required(data, required_fields)
        if err:
            return api_response(400, f"Record {idx + 1}: {err}")

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    try:
        inserted_ids = []
        skipped = []

        for idx, data in enumerate(records):
            project_id = int(data["project_id"])
            month_year = str(data["month_year"]).strip()
            monthly_target = str(data["monthly_target"]).strip()
            created_date = str(data.get("created_date") or now_str())

            # Check project exists
            if not project_exists(cursor, project_id):
                skipped.append({"index": idx + 1, "project_id": project_id, "reason": "Project not found or inactive"})
                continue

            # Check for duplicate (project + month)
            cursor.execute(
                """
                SELECT project_monthly_tracker_id
                FROM project_monthly_tracker
                WHERE project_id=%s AND month_year=%s AND is_active=1
                """,
                (project_id, month_year)
            )
            if cursor.fetchone():
                skipped.append({"index": idx + 1, "project_id": project_id, "month_year": month_year, "reason": "Already exists"})
                continue

            cursor.execute(
                """
                INSERT INTO project_monthly_tracker
                    (project_id, month_year, monthly_target, created_date, is_active)
                VALUES (%s, %s, %s, %s, 1)
                """,
                (project_id, month_year, monthly_target, created_date)
            )
            inserted_ids.append(cursor.lastrowid)

        conn.commit()

        # Response
        if not inserted_ids and skipped:
            return api_response(409, "No records inserted", {"skipped": skipped})

        return api_response(
            201,
            f"{len(inserted_ids)} record(s) added successfully",
            {
                "inserted_count": len(inserted_ids),
                "project_monthly_tracker_ids": inserted_ids,
                "skipped_count": len(skipped),
                "skipped": skipped if skipped else None,
            },
        )

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

    # -----------------------
    # 1) Filters for pmt list
    # -----------------------
    pmt_params = []
    where_pmt = "WHERE pmt.is_active=1"

    if data.get("project_monthly_tracker_id"):
        where_pmt += " AND pmt.project_monthly_tracker_id=%s"
        pmt_params.append(int(data["project_monthly_tracker_id"]))

    if data.get("project_id"):
        where_pmt += " AND pmt.project_id=%s"
        pmt_params.append(int(data["project_id"]))

    if data.get("month_year"):
        where_pmt += " AND pmt.month_year=%s"
        pmt_params.append(str(data["month_year"]).strip())

    if data.get("project_name"):
        where_pmt += " AND p.project_name LIKE %s"
        pmt_params.append(f"%{data['project_name']}%")

    # -----------------------------
    # 2) Filters for achieved hours
    # -----------------------------
    twt_params = []
    where_twt = "WHERE twt.is_active=1"

    # keep achieved aligned with list filters
    if data.get("project_id"):
        where_twt += " AND twt.project_id=%s"
        twt_params.append(int(data["project_id"]))

    if data.get("month_year"):
        # month_year match using DATE_FORMAT -> "Feb2026"
        where_twt += " AND DATE_FORMAT(twt.date_time, '%b%Y')=%s"
        twt_params.append(str(data["month_year"]).strip())

    # optional filters (use if your UI sends them)
    if data.get("task_id"):
        where_twt += " AND twt.task_id=%s"
        twt_params.append(int(data["task_id"]))

    if data.get("user_id"):
        where_twt += " AND twt.user_id=%s"
        twt_params.append(int(data["user_id"]))

    # date range filters
    # send: "date_from": "2026-02-01", "date_to": "2026-02-29"
    if data.get("date_from"):
        where_twt += " AND twt.date_time >= %s"
        twt_params.append(str(data["date_from"]).strip() + " 00:00:00")

    if data.get("date_to"):
        where_twt += " AND twt.date_time <= %s"
        twt_params.append(str(data["date_to"]).strip() + " 23:59:59")

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

                COALESCE(twt_sum.achieved_hours, 0) AS achieved_hours,
                (
                    COALESCE(CAST(pmt.monthly_target AS DECIMAL(12,2)), 0)
                    - COALESCE(twt_sum.achieved_hours, 0)
                ) AS pending_hours,

                pmt.created_date,
                pmt.is_active
            FROM project_monthly_tracker pmt
            LEFT JOIN project p ON p.project_id = pmt.project_id

            LEFT JOIN (
                SELECT
                    twt.project_id,
                    DATE_FORMAT(twt.date_time, '%b%Y') AS month_year,
                    SUM(
                        CASE
                            WHEN twt.actual_billable_hours REGEXP '^[0-9]+(\\.[0-9]+)?$'
                            THEN CAST(twt.actual_billable_hours AS DECIMAL(12,2))
                            ELSE 0
                        END
                    ) AS achieved_hours
                FROM task_work_tracker twt
                {where_twt}
                GROUP BY twt.project_id, DATE_FORMAT(twt.date_time, '%b%Y')
            ) twt_sum
                ON twt_sum.project_id = pmt.project_id
               AND twt_sum.month_year = pmt.month_year

            {where_pmt}
            ORDER BY pmt.project_monthly_tracker_id DESC
            LIMIT %s OFFSET %s
        """

        # IMPORTANT: twt params come first because the subquery appears first in SQL
        cursor.execute(query, tuple(twt_params + pmt_params + [limit, offset]))
        rows = cursor.fetchall()

        count_query = f"""
            SELECT COUNT(*) AS total
            FROM project_monthly_tracker pmt
            LEFT JOIN project p ON p.project_id = pmt.project_id
            {where_pmt}
        """
        cursor.execute(count_query, tuple(pmt_params))
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
