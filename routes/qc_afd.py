# routes/qc_afd.py

from flask import Blueprint, request
from config import get_db_connection
from utils.response import api_response
from datetime import datetime

qc_afd_bp = Blueprint("qc_afd", __name__)


def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------
# ADD (Single / Bulk / Category + Subcategory Supported)
# ---------------------------------------------------------
# ---------------------------------------------------------
# ADD MASTER + MULTIPLE CATEGORIES + SUBCATEGORIES
# ---------------------------------------------------------
@qc_afd_bp.route("/add", methods=["POST"])
def add_qc_afd():
    print("Received data for add_qc_afd:", request.get_json())

    data = request.get_json() or {}

    master_afd_name = data.get("master_afd_name")
    categories = data.get("categories", [])

    if not master_afd_name:
        return api_response(400, "master_afd_name is required")

    if not categories or not isinstance(categories, list):
        return api_response(400, "categories list is required")

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    try:
        created_at = now_str()
        updated_at = now_str()

        # -------------------------------------------------
        # STEP 1: Get or Create AFD MASTER
        # -------------------------------------------------
        cursor.execute(
            "SELECT afd_id FROM afd WHERE afd_name=%s LIMIT 1",
            (master_afd_name.strip(),)
        )
        master = cursor.fetchone()

        if master:
            afd_id = master["afd_id"]
        else:
            cursor.execute("""
                INSERT INTO afd (afd_name, created_at, updated_at)
                VALUES (%s, %s, %s)
            """, (master_afd_name.strip(), created_at, updated_at))
            afd_id = cursor.lastrowid

        inserted_ids = []

        # -------------------------------------------------
        # STEP 2: Insert Categories
        # -------------------------------------------------
        for idx, category in enumerate(categories):

            cat_name = category.get("afd_name")
            cat_points = category.get("afd_points")
            subcategories = category.get("subcategories", [])

            if not cat_name:
                return api_response(400, f"Category {idx + 1}: afd_name is required")

            if cat_points is None:
                return api_response(400, f"Category {cat_name}: afd_points is required")

            # Check duplicate category under same master
            cursor.execute("""
                SELECT qc_afd_id FROM qc_afd
                WHERE afd_id=%s AND LOWER(afd_name)=LOWER(%s) AND afd_category_id=0
            """, (afd_id, cat_name.strip()))

            if cursor.fetchone():
                return api_response(409, f"Category '{cat_name}' already exists")

            # Insert category
            cursor.execute("""
                INSERT INTO qc_afd
                (afd_id, afd_name, afd_points, afd_category_id, created_at, updated_at)
                VALUES (%s, %s, %s, 0, %s, %s)
            """, (
                afd_id,
                cat_name.strip(),
                int(cat_points),
                created_at,
                updated_at
            ))

            category_id = cursor.lastrowid
            inserted_ids.append(category_id)

            # -------------------------------------------------
            # STEP 3: Insert Subcategories
            # -------------------------------------------------
            for sub in subcategories:

                sub_name = sub.get("afd_name")
                sub_points = sub.get("afd_points")

                if not sub_name:
                    return api_response(400, f"Subcategory name required under {cat_name}")

                if sub_points is None:
                    return api_response(400, f"Subcategory points required for {sub_name}")

                cursor.execute("""
                    SELECT qc_afd_id FROM qc_afd
                    WHERE LOWER(afd_name)=LOWER(%s) AND afd_category_id=%s
                """, (sub_name.strip(), category_id))

                if cursor.fetchone():
                    return api_response(409, f"Subcategory '{sub_name}' already exists under '{cat_name}'")

                cursor.execute("""
                    INSERT INTO qc_afd
                    (afd_id, afd_name, afd_points, afd_category_id, created_at, updated_at)
                    VALUES (%s, %s, %s, %s, %s, %s)
                """, (
                    afd_id,
                    sub_name.strip(),
                    int(sub_points),
                    category_id,
                    created_at,
                    updated_at
                ))

                inserted_ids.append(cursor.lastrowid)

        conn.commit()

        return api_response(
            201,
            "AFD structure created successfully",
            {
                "master_afd_id": afd_id,
                "inserted_qc_afd_ids": inserted_ids
            }
        )

    except Exception as e:
        conn.rollback()
        return api_response(500, f"Add failed: {str(e)}")

    finally:
        cursor.close()
        conn.close()


# ---------------------------------------------------------
# UPDATE
# ---------------------------------------------------------
@qc_afd_bp.route("/update", methods=["PUT"])
def update_full_qc_afd():
    data = request.get_json(silent=True) or {}

    master_id = data.get("master_afd_id")
    master_name = data.get("master_afd_name")
    categories = data.get("categories", [])

    if not master_id:
        return api_response(400, "master_afd_id is required")

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    try:
        # --------------------
        # Validate Master
        # --------------------
        cursor.execute("SELECT afd_id FROM afd WHERE afd_id=%s", (master_id,))
        if not cursor.fetchone():
            return api_response(404, "Master AFD not found")

        # --------------------
        # Update Master
        # --------------------
        if master_name:
            cursor.execute(
                "UPDATE afd SET afd_name=%s, updated_at=%s WHERE afd_id=%s",
                (master_name, datetime.now(), master_id)
            )

        # --------------------
        # Update Categories
        # --------------------
        # --------------------
        for cat in categories:

            cat_id = cat.get("qc_afd_id")

            # ==============================
            # UPDATE CATEGORY
            # ==============================
            if cat_id:
                cursor.execute(
                    """UPDATE qc_afd 
                    SET afd_name=%s, afd_points=%s, updated_at=%s
                    WHERE qc_afd_id=%s AND afd_id=%s""",
                    (
                        cat.get("afd_name"),
                        cat.get("afd_points"),
                        datetime.now(),
                        cat_id,
                        master_id
                    )
                )
            else:
                # ==============================
                # INSERT NEW CATEGORY
                # ==============================
                cursor.execute(
                    """INSERT INTO qc_afd
                    (afd_id, afd_name, afd_points, afd_category_id, created_at, updated_at)
                    VALUES (%s, %s, %s, %s, %s, %s)""",
                    (
                        master_id,
                        cat.get("afd_name"),
                        cat.get("afd_points"),
                        0,  # category has no parent
                        datetime.now(),
                        datetime.now()
                    )
                )
                cat_id = cursor.lastrowid  # important for subcategories

            # --------------------
            # Subcategories
            # --------------------
            subcategories = cat.get("subcategories", [])

            for sub in subcategories:
                sub_id = sub.get("qc_afd_id")

                if sub_id:
                    # UPDATE subcategory
                    cursor.execute(
                        """UPDATE qc_afd 
                        SET afd_name=%s,
                            afd_points=%s,
                            afd_category_id=%s,
                            updated_at=%s
                        WHERE qc_afd_id=%s AND afd_id=%s""",
                        (
                            sub.get("afd_name"),
                            sub.get("afd_points"),
                            cat_id,
                            datetime.now(),
                            sub_id,
                            master_id
                        )
                    )
                else:
                    # INSERT new subcategory
                    cursor.execute(
                        """INSERT INTO qc_afd
                        (afd_id, afd_name, afd_points, afd_category_id, created_at, updated_at)
                        VALUES (%s, %s, %s, %s, %s, %s)""",
                        (
                            master_id,
                            sub.get("afd_name"),
                            sub.get("afd_points"),
                            cat_id,
                            datetime.now(),
                            datetime.now()
                        )
                    )

        conn.commit()
        return api_response(200, "Master + Categories + Subcategories updated successfully")

    except Exception as e:
        conn.rollback()
        return api_response(500, f"Update failed: {str(e)}")

    finally:
        cursor.close()
        conn.close()

# ---------------------------------------------------------
# DELETE (Auto delete children if category)
# ---------------------------------------------------------

@qc_afd_bp.route("/delete", methods=["DELETE"])
def delete_qc_afd():

    data = request.get_json(silent=True) or {}

    afd_ids = data.get("afd_ids", [])
    qc_afd_ids = data.get("qc_afd_ids", [])

    if not afd_ids and not qc_afd_ids:
        return api_response(400, "afd_ids or qc_afd_ids is required")

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    try:

        # ----------------------------------
        # DELETE MULTIPLE MASTERS
        # ----------------------------------
        if afd_ids:
            format_strings = ",".join(["%s"] * len(afd_ids))

            # Delete children first
            cursor.execute(
                f"DELETE FROM qc_afd WHERE afd_id IN ({format_strings})",
                tuple(afd_ids)
            )

            # Delete masters
            cursor.execute(
                f"DELETE FROM afd WHERE afd_id IN ({format_strings})",
                tuple(afd_ids)
            )

        # ----------------------------------
        # DELETE MULTIPLE CATEGORY / SUBCATEGORY
        # ----------------------------------
        if qc_afd_ids:
            format_strings = ",".join(["%s"] * len(qc_afd_ids))

            # Fetch records first
            cursor.execute(
                f"""
                SELECT qc_afd_id, afd_category_id 
                FROM qc_afd 
                WHERE qc_afd_id IN ({format_strings})
                """,
                tuple(qc_afd_ids)
            )

            records = cursor.fetchall()

            category_ids = []
            subcategory_ids = []

            for record in records:
                if record["afd_category_id"] == 0 or record["afd_category_id"] is None:
                    category_ids.append(record["qc_afd_id"])
                else:
                    subcategory_ids.append(record["qc_afd_id"])

            # Delete subcategories of categories
            if category_ids:
                format_cat = ",".join(["%s"] * len(category_ids))
                cursor.execute(
                    f"DELETE FROM qc_afd WHERE afd_category_id IN ({format_cat})",
                    tuple(category_ids)
                )

            # Delete selected categories + subcategories
            cursor.execute(
                f"DELETE FROM qc_afd WHERE qc_afd_id IN ({format_strings})",
                tuple(qc_afd_ids)
            )

        conn.commit()
        return api_response(200, "Records deleted successfully")

    except Exception as e:
        conn.rollback()
        return api_response(500, f"Delete failed: {str(e)}")


@qc_afd_bp.route("/list", methods=["POST"])
def list_qc_afd():
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    try:
        # Get request data for filtering
        data = request.get_json() or {}
        project_category_id = data.get("project_category_id")

        # -------------------------
        # Fetch Masters
        # -------------------------
        if project_category_id:
            cursor.execute("""
                SELECT a.afd_id, a.afd_name 
                FROM afd a
                JOIN project_category pc ON a.afd_id = pc.afd_id
                WHERE a.is_active=1 AND pc.project_category_id=%s
            """, (project_category_id,))
        else:
            cursor.execute("SELECT afd_id, afd_name FROM afd WHERE is_active=1")
        masters = cursor.fetchall()

        # -------------------------
        # Fetch All Categories/Subcategories
        # -------------------------
        if project_category_id:
            cursor.execute("""
                SELECT qc_afd_id, afd_id, afd_name, afd_points, afd_category_id
                FROM qc_afd
                WHERE afd_id IN (
                    SELECT a.afd_id 
                    FROM afd a
                    JOIN project_category pc ON a.afd_id = pc.afd_id
                    WHERE pc.project_category_id=%s
                )
            """, (project_category_id,))
        else:
            cursor.execute("""
                SELECT qc_afd_id, afd_id, afd_name, afd_points, afd_category_id
                FROM qc_afd
            """)
        qc_rows = cursor.fetchall()

        # -------------------------
        # Build Hierarchy
        # -------------------------
        result = []

        for master in masters:
            master_dict = {
                "afd_id": master["afd_id"],
                "afd_name": master["afd_name"],
                "categories": []
            }

            # Get categories (parent rows)
            categories = [
                row for row in qc_rows
                if row["afd_id"] == master["afd_id"]
                and (row["afd_category_id"] == 0 or row["afd_category_id"] is None)
            ]

            for cat in categories:
                category_dict = {
                    "qc_afd_id": cat["qc_afd_id"],
                    "afd_name": cat["afd_name"],
                    "afd_points": cat["afd_points"],
                    "subcategories": []
                }

                # Get subcategories
                subs = [
                    row for row in qc_rows
                    if row["afd_category_id"] == cat["qc_afd_id"]
                ]

                for sub in subs:
                    category_dict["subcategories"].append({
                        "qc_afd_id": sub["qc_afd_id"],
                        "afd_name": sub["afd_name"],
                        "afd_points": sub["afd_points"]
                    })

                master_dict["categories"].append(category_dict)

            result.append(master_dict)

        return api_response(200, "QC AFD list fetched successfully", result)

    except Exception as e:
        return api_response(500, f"Fetch failed: {str(e)}")

    finally:
        cursor.close()
        conn.close()

# ---------------------------------------------------------
# LIST GROUPED PROPER HIERARCHY
# ---------------------------------------------------------
@qc_afd_bp.route("/list_by_category", methods=["POST"])
def list_qc_afd_by_category():

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    try:
        cursor.execute("""
            SELECT *
            FROM qc_afd
            ORDER BY afd_category_id ASC, qc_afd_id ASC
        """)
        rows = cursor.fetchall()

        categories = {}
        for row in rows:
            if row["afd_category_id"] == 0:
                categories[row["qc_afd_id"]] = {
                    "category": row,
                    "subcategories": []
                }

        for row in rows:
            parent_id = row["afd_category_id"]
            if parent_id != 0 and parent_id in categories:
                categories[parent_id]["subcategories"].append(row)

        return api_response(200, "Records fetched successfully", {
            "total_categories": len(categories),
            "data": list(categories.values())
        })

    except Exception as e:
        return api_response(500, f"List failed: {str(e)}")
    finally:
        cursor.close()
        conn.close()