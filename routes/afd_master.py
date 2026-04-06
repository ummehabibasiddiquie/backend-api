from flask import Blueprint, request
from datetime import datetime
from config import get_db_connection
from utils.validators import validate_request
from utils.response import api_response

afd_master_bp = Blueprint("afd_master", __name__, url_prefix="/qc/afd-master")

AFD_TABLE = "afd"


def _today():
    # matches your created_date format: YYYY-MM-DD
    return datetime.now().strftime("%Y-%m-%d")


@afd_master_bp.route("/create", methods=["POST"])
def create_afd():
    data, err = validate_request(required=["afd_name"])
    if err:
        return err

    afd_name = (data.get("afd_name") or "").strip()
    if not afd_name:
        return api_response(message="afd_name is required", status=400)

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        # prevent duplicates (case-insensitive) among active rows
        cursor.execute(f"""
            SELECT afd_id
            FROM {AFD_TABLE}
            WHERE is_active=1
              AND LOWER(TRIM(afd_name)) = LOWER(TRIM(%s))
            LIMIT 1
        """, (afd_name,))
        if cursor.fetchone():
            return api_response(message="AFD already exists", status=409)

        cursor.execute(f"""
            INSERT INTO {AFD_TABLE} (afd_name, is_active, created_date)
            VALUES (%s, %s, %s)
        """, (afd_name, 1, _today()))
        conn.commit()

        return api_response(
            message="AFD created successfully",
            status=201,
            data={"afd_id": cursor.lastrowid}
        )
    except Exception as e:
        conn.rollback()
        return api_response(message=f"Failed to create AFD: {str(e)}", status=500)
    finally:
        cursor.close()
        conn.close()


@afd_master_bp.route("/update", methods=["POST"])
def update_afd():
    data, err = validate_request(required=["afd_id"])
    if err:
        return err

    afd_id = data.get("afd_id")
    afd_name = data.get("afd_name")          # optional
    is_active = data.get("is_active")        # optional

    updates = []
    params = []

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        # exists?
        cursor.execute(f"SELECT afd_id, afd_name, is_active FROM {AFD_TABLE} WHERE afd_id=%s LIMIT 1", (afd_id,))
        existing = cursor.fetchone()
        if not existing:
            return api_response(message="AFD not found", status=404)

        if afd_name is not None:
            afd_name = (afd_name or "").strip()
            if not afd_name:
                return api_response(message="afd_name cannot be empty", status=400)

            # prevent duplicate name among active rows (excluding current)
            cursor.execute(f"""
                SELECT afd_id
                FROM {AFD_TABLE}
                WHERE is_active=1
                  AND LOWER(TRIM(afd_name)) = LOWER(TRIM(%s))
                  AND afd_id <> %s
                LIMIT 1
            """, (afd_name, afd_id))
            if cursor.fetchone():
                return api_response(message="Another active AFD with same name already exists", status=409)

            updates.append("afd_name=%s")
            params.append(afd_name)

        if is_active is not None:
            # accept 0/1 or "0"/"1"
            try:
                is_active_val = int(is_active)
            except:
                return api_response(message="is_active must be 0 or 1", status=400)
            if is_active_val not in (0, 1):
                return api_response(message="is_active must be 0 or 1", status=400)

            updates.append("is_active=%s")
            params.append(is_active_val)

        if not updates:
            return api_response(message="Nothing to update", status=400)

        params.append(afd_id)
        cursor.execute(f"""
            UPDATE {AFD_TABLE}
            SET {", ".join(updates)}
            WHERE afd_id=%s
        """, tuple(params))
        conn.commit()

        return api_response(message="AFD updated successfully", status=200)
    except Exception as e:
        conn.rollback()
        return api_response(message=f"Failed to update AFD: {str(e)}", status=500)
    finally:
        cursor.close()
        conn.close()


@afd_master_bp.route("/delete", methods=["POST"])
def delete_afd():
    """
    Soft delete: sets is_active = 0
    """
    data, err = validate_request(required=["afd_id"])
    if err:
        return err

    afd_id = data.get("afd_id")

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute(f"SELECT afd_id FROM {AFD_TABLE} WHERE afd_id=%s LIMIT 1", (afd_id,))
        if not cursor.fetchone():
            return api_response(message="AFD not found", status=404)

        cursor.execute(f"UPDATE {AFD_TABLE} SET is_active=0 WHERE afd_id=%s", (afd_id,))
        conn.commit()

        return api_response(message="AFD deleted (disabled) successfully", status=200)
    except Exception as e:
        conn.rollback()
        return api_response(message=f"Failed to delete AFD: {str(e)}", status=500)
    finally:
        cursor.close()
        conn.close()


@afd_master_bp.route("/list", methods=["POST"])
def list_afd():
    data = request.get_json(silent=True) or {}

    # optional filters
    is_active = data.get("is_active")  # default: only active
    search = (data.get("search") or "").strip()

    where = []
    params = []

    if is_active is None:
        where.append("is_active=1")
    else:
        try:
            is_active_val = int(is_active)
        except:
            return api_response(message="is_active must be 0 or 1", status=400)
        where.append("is_active=%s")
        params.append(is_active_val)

    if search:
        where.append("LOWER(afd_name) LIKE %s")
        params.append(f"%{search.lower()}%")

    where_sql = "WHERE " + " AND ".join(where) if where else ""

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute(f"""
            SELECT afd_id, afd_name, is_active, created_date
            FROM {AFD_TABLE}
            {where_sql}
            ORDER BY afd_id ASC
        """, tuple(params))

        rows = cursor.fetchall() or []
        return api_response(message="AFD list fetched", status=200, data={"items": rows, "count": len(rows)})
    except Exception as e:
        return api_response(message=f"Failed to list AFD: {str(e)}", status=500)
    finally:
        cursor.close()
        conn.close()