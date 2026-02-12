
# routes/qc_afd.py

from flask import Blueprint, request
from config import get_db_connection
from utils.response import api_response
from datetime import datetime

qc_afd_bp = Blueprint("qc_afd", __name__)


def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# -----------------------------
# ADD (supports single or bulk insert)
# -----------------------------
@qc_afd_bp.route("/add", methods=["POST"])
def add_qc_afd():
    """
    Accepts either:
      - Single object: {"afd_id": 1, "afd_name": "...", "afd_points": 10, "afd_category_id": 1}
      - Array of objects: [{...}, {...}]
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
    for idx, data in enumerate(records):
        if not data.get("afd_name"):
            return api_response(400, f"Record {idx + 1}: afd_name is required")
        if data.get("afd_points") in [None, ""]:
            return api_response(400, f"Record {idx + 1}: afd_points is required")
        if data.get("afd_category_id") in [None, ""]:
            return api_response(400, f"Record {idx + 1}: afd_category_id is required")

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    try:
        inserted_ids = []
        skipped = []

        for idx, data in enumerate(records):
            afd_id = int(data.get("afd_id") or 1)  # default 1 if not provided
            afd_name = str(data["afd_name"]).strip()
            afd_points = int(data["afd_points"])
            afd_category_id = int(data["afd_category_id"])
            created_at = str(data.get("created_at") or now_str())
            updated_at = str(data.get("updated_at") or now_str())

            # Check for duplicate (same afd_name + afd_category_id)
            cursor.execute(
                """
                SELECT qc_afd_id
                FROM qc_afd
                WHERE afd_name=%s AND afd_category_id=%s
                """,
                (afd_name, afd_category_id)
            )
            if cursor.fetchone():
                skipped.append({
                    "index": idx + 1,
                    "afd_name": afd_name,
                    "afd_category_id": afd_category_id,
                    "reason": "Already exists"
                })
                continue

            cursor.execute(
                """
                INSERT INTO qc_afd
                    (afd_id, afd_name, afd_points, afd_category_id, created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (afd_id, afd_name, afd_points, afd_category_id, created_at, updated_at)
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
                "qc_afd_ids": inserted_ids,
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
@qc_afd_bp.route("/update", methods=["POST"])
def update_qc_afd():
    data = request.get_json() or {}

    if not data.get("qc_afd_id"):
        return api_response(400, "qc_afd_id is required")

    qc_afd_id = int(data["qc_afd_id"])

    updates = []
    params = []

    if "afd_id" in data and data["afd_id"] not in [None, ""]:
        updates.append("afd_id=%s")
        params.append(int(data["afd_id"]))

    if "afd_name" in data and data["afd_name"] not in [None, ""]:
        updates.append("afd_name=%s")
        params.append(str(data["afd_name"]).strip())

    if "afd_points" in data and data["afd_points"] not in [None, ""]:
        updates.append("afd_points=%s")
        params.append(int(data["afd_points"]))

    if "afd_category_id" in data and data["afd_category_id"] not in [None, ""]:
        updates.append("afd_category_id=%s")
        params.append(int(data["afd_category_id"]))

    if not updates:
        return api_response(400, "No fields provided to update")

    # Always update updated_at
    updates.append("updated_at=%s")
    params.append(now_str())

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    try:
        # Check if record exists
        cursor.execute(
            "SELECT qc_afd_id, afd_name, afd_category_id FROM qc_afd WHERE qc_afd_id=%s",
            (qc_afd_id,)
        )
        current = cursor.fetchone()
        if not current:
            return api_response(404, "Record not found")

        # Check for duplicate if updating afd_name or afd_category_id
        if ("afd_name" in data and data["afd_name"] not in [None, ""]) or \
           ("afd_category_id" in data and data["afd_category_id"] not in [None, ""]):
            
            final_afd_name = str(data["afd_name"]).strip() if ("afd_name" in data and data["afd_name"] not in [None, ""]) else current["afd_name"]
            final_afd_category_id = int(data["afd_category_id"]) if ("afd_category_id" in data and data["afd_category_id"] not in [None, ""]) else current["afd_category_id"]

            cursor.execute(
                """
                SELECT qc_afd_id
                FROM qc_afd
                WHERE afd_name=%s AND afd_category_id=%s AND qc_afd_id<>%s
                """,
                (final_afd_name, final_afd_category_id, qc_afd_id)
            )
            if cursor.fetchone():
                return api_response(409, "AFD with this name and category already exists")

        params.append(qc_afd_id)
        query = f"""
            UPDATE qc_afd
            SET {', '.join(updates)}
            WHERE qc_afd_id=%s
        """
        cursor.execute(query, tuple(params))
        conn.commit()

        return api_response(200, "AFD updated successfully")

    except Exception as e:
        conn.rollback()
        return api_response(500, f"Update failed: {str(e)}")
    finally:
        cursor.close()
        conn.close()


# -----------------------------
# DELETE
# -----------------------------
@qc_afd_bp.route("/delete", methods=["POST"])
def delete_qc_afd():
    data = request.get_json() or {}

    if not data.get("qc_afd_id"):
        return api_response(400, "qc_afd_id is required")

    qc_afd_id = int(data["qc_afd_id"])

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    try:
        cursor.execute(
            "SELECT qc_afd_id FROM qc_afd WHERE qc_afd_id=%s",
            (qc_afd_id,)
        )
        if not cursor.fetchone():
            return api_response(404, "Record not found")

        cursor.execute(
            "DELETE FROM qc_afd WHERE qc_afd_id=%s",
            (qc_afd_id,)
        )
        conn.commit()

        return api_response(200, "AFD deleted successfully")

    except Exception as e:
        conn.rollback()
        return api_response(500, f"Delete failed: {str(e)}")
    finally:
        cursor.close()
        conn.close()


# -----------------------------
# LIST (with optional filters)
# -----------------------------
@qc_afd_bp.route("/list", methods=["POST"])
def list_qc_afd():
    data = request.get_json() or {}

    params = []
    where = "WHERE 1=1"

    if data.get("qc_afd_id"):
        where += " AND qc_afd_id=%s"
        params.append(int(data["qc_afd_id"]))

    if data.get("afd_id"):
        where += " AND afd_id=%s"
        params.append(int(data["afd_id"]))

    if data.get("afd_category_id") is not None and data.get("afd_category_id") != "":
        where += " AND afd_category_id=%s"
        params.append(int(data["afd_category_id"]))

    if data.get("afd_name"):
        where += " AND afd_name LIKE %s"
        params.append(f"%{data['afd_name']}%")

    limit = int(data.get("limit") or 200)
    offset = int(data.get("offset") or 0)

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    try:
        query = f"""
            SELECT
                qc_afd_id,
                afd_id,
                afd_name,
                afd_points,
                afd_category_id,
                created_at,
                updated_at
            FROM qc_afd
            {where}
            ORDER BY afd_category_id ASC, qc_afd_id ASC
            LIMIT %s OFFSET %s
        """
        cursor.execute(query, tuple(params + [limit, offset]))
        rows = cursor.fetchall()

        count_query = f"""
            SELECT COUNT(*) AS total
            FROM qc_afd
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


# -----------------------------
# LIST GROUPED BY CATEGORY
# -----------------------------
@qc_afd_bp.route("/list_by_category", methods=["POST"])
def list_qc_afd_by_category():
    """
    Returns AFD items grouped by afd_category_id
    """
    data = request.get_json() or {}

    params = []
    where = "WHERE 1=1"

    if data.get("afd_id"):
        where += " AND afd_id=%s"
        params.append(int(data["afd_id"]))

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    try:
        query = f"""
            SELECT
                qc_afd_id,
                afd_id,
                afd_name,
                afd_points,
                afd_category_id,
                created_at,
                updated_at
            FROM qc_afd
            {where}
            ORDER BY afd_category_id ASC, qc_afd_id ASC
        """
        cursor.execute(query, tuple(params))
        rows = cursor.fetchall()

        # Group by category
        grouped = {}
        for row in rows:
            cat_id = row["afd_category_id"]
            if cat_id not in grouped:
                grouped[cat_id] = []
            grouped[cat_id].append(row)

        return api_response(200, "Records fetched successfully", {
            "total": len(rows),
            "categories": grouped
        })

    except Exception as e:
        return api_response(500, f"List failed: {str(e)}")
    finally:
        cursor.close()
        conn.close()
