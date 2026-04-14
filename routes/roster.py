from flask import Blueprint, request, jsonify
from config import get_db_connection
import json
from utils.response import api_response

roster_bp = Blueprint("roster", __name__)

# =========================================
# 1. GET ROSTER (CALENDAR) - ROLE BASED
# =========================================
@roster_bp.route("/get", methods=["POST"])
def get_roster():
    data = request.json
    logged_in_user_id = data["logged_in_user_id"]
    month_year = data["month_year"]
    target_user_id = data.get("user_id")  # Optional: for viewing specific user
    planned_filter = data.get("planned")  # Optional: "planned", "unplanned", or None for both

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    try:
        # Get logged in user's role
        cursor.execute("""
            SELECT r.role_name 
            FROM tfs_user u
            JOIN user_role r ON r.role_id = u.role_id
            WHERE u.user_id = %s AND u.is_active = 1
        """, (logged_in_user_id,))
        role_row = cursor.fetchone()

        if not role_row:
            return jsonify({"status": 404, "message": "User not found"})

        role = role_row["role_name"].lower()

        # =====================================================
        # BUILD USER LIST BASED ON ROLE
        # =====================================================
        user_ids = []

        if role == "agent":
            # Agent sees only their own roster
            user_ids = [logged_in_user_id]

        elif role in ["assistant manager", "manager", "project manager", "qa"]:
            # Manager sees all users under them (handle JSON arrays)
            logged_id_str = str(logged_in_user_id)
            logged_id_int = int(logged_in_user_id)

            query = """
                SELECT u.user_id 
                FROM tfs_user u
                WHERE u.is_active = 1
                AND (
                    u.asst_manager_id = %s 
                    OR u.asst_manager_id = %s
                    OR u.project_manager_id = %s 
                    OR u.project_manager_id = %s
                    OR u.qa_id = %s 
                    OR u.qa_id = %s
                    OR (JSON_VALID(u.asst_manager_id) AND (
                        JSON_CONTAINS(u.asst_manager_id, JSON_ARRAY(%s))
                        OR JSON_CONTAINS(u.asst_manager_id, JSON_ARRAY(CAST(%s AS UNSIGNED)))
                    ))
                    OR (JSON_VALID(u.project_manager_id) AND (
                        JSON_CONTAINS(u.project_manager_id, JSON_ARRAY(%s))
                        OR JSON_CONTAINS(u.project_manager_id, JSON_ARRAY(CAST(%s AS UNSIGNED)))
                    ))
                    OR (JSON_VALID(u.qa_id) AND (
                        JSON_CONTAINS(u.qa_id, JSON_ARRAY(%s))
                        OR JSON_CONTAINS(u.qa_id, JSON_ARRAY(CAST(%s AS UNSIGNED)))
                    ))
                )
            """
            params = [
                logged_id_str, logged_id_int,  # asst_manager_id
                logged_id_str, logged_id_int,  # project_manager_id
                logged_id_str, logged_id_int,  # qa_id
                logged_id_str, logged_id_str,  # asst_manager JSON
                logged_id_str, logged_id_str,  # project_manager JSON
                logged_id_str, logged_id_str   # qa JSON
            ]

            cursor.execute(query, params)
            users = cursor.fetchall()
            user_ids = [u["user_id"] for u in users]

            # Also include manager's own roster
            if logged_in_user_id not in user_ids:
                user_ids.append(logged_in_user_id)

        else:
            # Admin/Super Admin - can see all or specific user
            if target_user_id:
                user_ids = [target_user_id]
            else:
                # Get all users
                cursor.execute("SELECT user_id FROM tfs_user WHERE is_active = 1")
                users = cursor.fetchall()
                user_ids = [u["user_id"] for u in users]

        # If specific user_id requested, filter to that
        if target_user_id and role != "agent":
            if target_user_id in user_ids:
                user_ids = [target_user_id]
            else:
                return jsonify({"status": 403, "message": "You cannot view this user's roster"})

        # =====================================================
        # GET ROSTERS FOR ALL USERS
        # =====================================================
        if not user_ids:
            return jsonify({"status": 200, "data": [], "message": "No users found under you"})

        placeholders = ",".join(["%s"] * len(user_ids))
        query = f"""
            SELECT r.*, u.user_name, u.user_email, u.team_id, t.team_name
            FROM rosters r
            JOIN tfs_user u ON u.user_id = r.user_id
            LEFT JOIN team t ON t.team_id = u.team_id
            WHERE r.user_id IN ({placeholders})
            AND r.month_year = %s
        """
        params = user_ids + [month_year]

        cursor.execute(query, params)
        rosters = cursor.fetchall()

        if not rosters:
            return jsonify({"status": 404, "message": "No rosters found for this month"})

        # =====================================================
        # GET DAYS FOR EACH ROSTER
        # =====================================================
        result = []
        for roster in rosters:
            # Build WHERE clause for planned/unplanned filter
            where_clause = "WHERE rd.roster_id=%s"
            query_params = [roster["roster_id"]]
            
            if planned_filter:
                if planned_filter == "planned":
                    where_clause += " AND rd.is_leave = 1 AND rd.is_planned_leave = 1"
                elif planned_filter == "unplanned":
                    where_clause += " AND rd.is_leave = 1 AND rd.is_planned_leave = 0"
                # If planned_filter is "none" or other, show only non-leave days
                elif planned_filter == "none":
                    where_clause += " AND rd.is_leave = 0"
            
            cursor.execute(f"""
                SELECT 
                    COALESCE(
                        -- Use draft data if there's a pending draft for logged-in user
                        CASE 
                            WHEN d.draft_id IS NOT NULL AND d.status = 'pending' AND d.edited_by = %s THEN
                                CASE 
                                    WHEN JSON_EXTRACT(d.changes_json, '$.day_type') IS NOT NULL THEN JSON_UNQUOTE(JSON_EXTRACT(d.changes_json, '$.day_type'))
                                    ELSE rd.day_type
                                END
                            ELSE rd.day_type
                        END,
                        rd.day_type
                    ) as day_type,
                    COALESCE(
                        CASE 
                            WHEN d.draft_id IS NOT NULL AND d.status = 'pending' AND d.edited_by = %s THEN
                                CASE 
                                    WHEN JSON_EXTRACT(d.changes_json, '$.is_leave') IS NOT NULL THEN JSON_EXTRACT(d.changes_json, '$.is_leave')
                                    ELSE rd.is_leave
                                END
                            ELSE rd.is_leave
                        END,
                        rd.is_leave
                    ) as is_leave,
                    COALESCE(
                        CASE 
                            WHEN d.draft_id IS NOT NULL AND d.status = 'pending' AND d.edited_by = %s THEN
                                CASE 
                                    WHEN JSON_EXTRACT(d.changes_json, '$.leave_type_id') IS NOT NULL THEN JSON_EXTRACT(d.changes_json, '$.leave_type_id')
                                    ELSE rd.leave_type_id
                                END
                            ELSE rd.leave_type_id
                        END,
                        rd.leave_type_id
                    ) as leave_type_id,
                    COALESCE(
                        CASE 
                            WHEN d.draft_id IS NOT NULL AND d.status = 'pending' AND d.edited_by = %s THEN
                                CASE 
                                    WHEN JSON_EXTRACT(d.changes_json, '$.is_target_leave') IS NOT NULL THEN JSON_EXTRACT(d.changes_json, '$.is_target_leave')
                                    ELSE rd.is_target_leave
                                END
                            ELSE rd.is_target_leave
                        END,
                        rd.is_target_leave
                    ) as is_target_leave,
                    COALESCE(
                        CASE 
                            WHEN d.draft_id IS NOT NULL AND d.status = 'pending' AND d.edited_by = %s THEN
                                CASE 
                                    WHEN JSON_EXTRACT(d.changes_json, '$.is_planned_leave') IS NOT NULL THEN JSON_EXTRACT(d.changes_json, '$.is_planned_leave')
                                    ELSE rd.is_planned_leave
                                END
                            ELSE rd.is_planned_leave
                        END,
                        rd.is_planned_leave
                    ) as is_planned_leave,
                    COALESCE(
                        CASE 
                            WHEN d.draft_id IS NOT NULL AND d.status = 'pending' AND d.edited_by = %s THEN
                                CASE 
                                    WHEN JSON_EXTRACT(d.changes_json, '$.shift') IS NOT NULL THEN JSON_UNQUOTE(JSON_EXTRACT(d.changes_json, '$.shift'))
                                    ELSE rd.shift
                                END
                            ELSE rd.shift
                        END,
                        rd.shift
                    ) as shift,
                    rd.roster_day_id,
                    rd.roster_id,
                    rd.user_id,
                    rd.date,
                    rd.created_at,
                    rd.updated_at,
                    lt.leave_type_name
                FROM roster_days rd
                LEFT JOIN roster_day_drafts d ON d.roster_day_id = rd.roster_day_id AND d.status = 'pending'
                LEFT JOIN leave_types lt ON lt.leave_type_id = COALESCE(
                    CASE 
                        WHEN d.draft_id IS NOT NULL AND d.status = 'pending' AND d.edited_by = %s THEN
                            CASE 
                                WHEN JSON_EXTRACT(d.changes_json, '$.leave_type_id') IS NOT NULL THEN JSON_EXTRACT(d.changes_json, '$.leave_type_id')
                                ELSE rd.leave_type_id
                            END
                        ELSE rd.leave_type_id
                    END,
                    rd.leave_type_id
                )
                {where_clause}
                ORDER BY rd.date
            """, tuple([logged_in_user_id] * 7 + query_params))

            days = cursor.fetchall()

            calendar = []
            for d in days:
                if d.get("is_leave") == 1:
                    color = "red"
                elif d["day_type"] == "weekoff":
                    color = "grey"
                elif d["day_type"] == "holiday":
                    color = "blue"
                elif d["day_type"] == "wfh":
                    color = "orange"
                elif d["day_type"] == "half_day":
                    color = "purple"
                else:
                    color = "green"

                # Check if this data is from pending draft (only for days that have pending changes)
                is_pending = False
                # Check if there's a pending draft for this day and user
                cursor.execute("""
                    SELECT COUNT(*) as pending_count
                    FROM roster_day_drafts
                    WHERE roster_day_id = %s AND status = 'pending' AND edited_by = %s
                """, (d["roster_day_id"], logged_in_user_id))
                pending_count = cursor.fetchone()["pending_count"]
                is_pending = pending_count > 0

                calendar_item = {
                    "date": str(d["date"]),
                    "status": d["day_type"],
                    "shift": d["shift"],
                    "color": color,
                    "editable": True,
                    "is_planned": "yes" if d.get("is_planned_leave") == 1 else "no",
                    "pending_status": "pending" if is_pending else "approved"
                }
                
                # Add leave type information only for leave days
                print(f"DEBUG: Checking leave condition - is_leave: {d.get('is_leave')}, day_type: {d.get('day_type')}")
                if d.get("is_leave") == 1 or d.get("day_type") == "leave":
                    print(f"DEBUG: Leave day found - date: {d['date']}, leave_type_id: {d.get('leave_type_id')}, leave_type_name: {d.get('leave_type_name')}")
                    calendar_item["leave_type_id"] = d.get("leave_type_id")
                    calendar_item["leave_type_name"] = d.get("leave_type_name")
                    print(f"DEBUG: Added to calendar_item - leave_type_id: {calendar_item.get('leave_type_id')}, leave_type_name: {calendar_item.get('leave_type_name')}")
                else:
                    print(f"DEBUG: Not a leave day - date: {d['date']}, is_leave: {d.get('is_leave')}, day_type: {d.get('day_type')}")

                calendar.append(calendar_item)

            result.append({
                "user_id": roster["user_id"],
                "user_name": roster["user_name"],
                "user_email": roster["user_email"],
                "team": {
                    "team_id": roster["team_id"],
                    "team_name": roster["team_name"]
                },
                "summary": {
                    "working_days": roster["working_days"],
                    "weekoffs": roster["weekoff_days"],
                    "holidays": roster["holiday_days"],
                    "target": roster["final_target"]
                },
                "calendar": calendar
            })

        # If agent, return single object, else return array
        if role == "agent" and len(result) == 1:
            return jsonify({
                "status": 200,
                "role": role,
                "data": result[0]
            })
        else:
            return jsonify({
                "status": 200,
                "role": role,
                "count": len(result),
                "data": result
            })

    finally:
        cursor.close()
        conn.close()


@roster_bp.route("/update", methods=["POST"])
def update_day():
    print("=== UPDATE API CALLED ===")

    data = request.json

    updates = data.get("updates", [])
    edited_by = data.get("logged_in_user_id")
    print("updates", updates)
    print("edited_by", edited_by)
    print(f"Number of updates: {len(updates)}")

    if not updates:
        return jsonify({"status": 400, "message": "No data"})

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    try:
        drafts_created = 0

        for update in updates:

            user_id = update.get("user_id")
            date = update.get("date")
            update_type = update.get("type")
            print(f"\n=== Processing Update ===")
            print("user_id", user_id)
            print("date", date)
            print("type", update_type)

            if not user_id or not date:
                print("Skipping: Missing user_id or date")
                continue

            cursor.execute("""
                SELECT * FROM roster_days
                WHERE user_id=%s AND date=%s
            """, (user_id, date))

            day = cursor.fetchone()
            if not day:
                print(f"Skipping: No roster_days record found for user {user_id} on {date}")
                continue
            else:
                print(f"Found roster_days record: roster_day_id={day['roster_day_id']}")

            # ----------------------
            # BUILD CHANGES JSON
            # ----------------------
            changes = {}

            if update.get("type") == "leave":
                leave_type_id = update.get("leave_type_id")
                # Handle string "None" values
                if leave_type_id == "None" or leave_type_id == "null" or leave_type_id is None:
                    leave_type_id = None
                changes["leave_type_id"] = leave_type_id
                changes["is_leave"] = 1
                # Store standardized day_type as "leave"
                changes["day_type"] = "leave"
                
                # Set is_target_leave based on leave_type's affects_target flag
                if leave_type_id is not None:
                    cursor.execute("""
                        SELECT affects_target FROM leave_types 
                        WHERE leave_type_id = %s
                    """, (leave_type_id,))
                    leave_type = cursor.fetchone()
                    if leave_type:
                        changes["is_target_leave"] = leave_type["affects_target"]
                
                if "planned" in update:
                    # Handle both 0/1 and yes/no values
                    planned_value = update["planned"]
                    if planned_value in ["yes", "Yes", "YES"]:
                        changes["is_planned_leave"] = 1
                    elif planned_value in ["no", "No", "NO"]:
                        changes["is_planned_leave"] = 0
                    else:
                        # Handle numeric values (0, 1) or any other values
                        try:
                            changes["is_planned_leave"] = 1 if int(planned_value) > 0 else 0
                        except (ValueError, TypeError):
                            changes["is_planned_leave"] = 0

            elif update.get("type") == "weekoff":
                changes["day_type"] = "weekoff"
                changes["is_leave"] = 0

            elif update.get("type") == "working" or update.get("type") == "work":
                changes["day_type"] = "working"
                changes["is_leave"] = 0

            elif update.get("type") == "wfh":
                changes["day_type"] = "wfh"
                changes["is_leave"] = 0

            elif update.get("type") == "half_day":
                changes["day_type"] = "half_day"
                changes["is_leave"] = 0

            elif update.get("type") == "shift" or ("shift" in update and not update.get("type")):
                changes["shift"] = update.get("shift")

            else:
                continue

            # Debug: Print changes to see what's being created
            print(f"Update type: {update.get('type')}, Changes: {changes}")

            # Only create draft if there are actual changes
            if not changes:
                print(f"No changes to create for {update.get('type')} - skipping")
                continue

            # ----------------------
            # GET OLD DATA SNAPSHOT
            # ----------------------
            old_data = {}
            for key in changes.keys():
                old_data[key] = day.get(key)

            # Debug: Print old data
            print(f"Old data: {old_data}")

            # ----------------------
            # INSERT SINGLE DRAFT ROW
            # ----------------------
            cursor.execute("""
                INSERT INTO roster_day_drafts
                (roster_day_id, roster_id, user_id, date,
                 changes_json, old_json, edited_by, status)
                VALUES (%s,%s,%s,%s,%s,%s,%s,'draft')
            """, (
                day["roster_day_id"],
                day["roster_id"],
                user_id,
                date,
                json.dumps(changes),
                json.dumps(old_data),
                edited_by
            ))
            
            drafts_created += 1
            print(f"Draft created successfully! Total drafts: {drafts_created}")

        conn.commit()
        print(f"\n=== SUMMARY ===")
        print(f"Total updates processed: {len(updates)}")
        print(f"Total drafts created: {drafts_created}")

        return jsonify({
            "status": 200,
            "message": f"{drafts_created} draft(s) created successfully"
        })

    except Exception as e:
        conn.rollback()
        return jsonify({"status": 500, "message": str(e)})

    finally:
        cursor.close()
        conn.close()

@roster_bp.route("/submit", methods=["POST"])
def submit():

    data = request.json
    edited_by = data.get("edited_by")

    conn = get_db_connection()
    cursor = conn.cursor()

    try:

        cursor.execute("""
            UPDATE roster_day_drafts
            SET status='pending'
            WHERE edited_by=%s AND status='draft'
        """, (edited_by,))

        conn.commit()

        return jsonify({
            "status": 200,
            "message": "Submitted for approval"
        })

    except Exception as e:
        conn.rollback()
        return jsonify({"status": 500, "message": str(e)})

    finally:
        cursor.close()
        conn.close()

@roster_bp.route("/approve", methods=["POST"])
def approve():

    data = request.json
    draft_id = data.get("draft_id")

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    try:

        cursor.execute("""
            SELECT * FROM roster_day_drafts
            WHERE draft_id=%s AND status='pending'
        """, (draft_id,))

        draft = cursor.fetchone()

        if not draft:
            return jsonify({"status": 404, "message": "Draft not found"})

        changes = json.loads(draft["changes_json"])

        if not changes:
            return jsonify({"status": 400, "message": "No changes to apply"})

        # Build SET query dynamically
        set_clause = ", ".join([f"{k}=%s" for k in changes.keys()])
        values = list(changes.values())

        query = f"""
            UPDATE roster_days
            SET {set_clause}
            WHERE roster_day_id = %s
        """

        print(f"DEBUG: About to update roster_days with query: {query}")
        print(f"DEBUG: Values: {values}")
        print(f"DEBUG: roster_day_id: {draft['roster_day_id']}")

        cursor.execute(query, values + [draft["roster_day_id"]])

        print(f"DEBUG: roster_days updated successfully")

        # Mark approved
        cursor.execute("""
            UPDATE roster_day_drafts
            SET status='approved'
            WHERE roster_day_id=%s
        """, (draft["roster_day_id"],))

        print(f"DEBUG: Draft marked as approved")

        # Check draft approval date and apply early month planned leave logic
        approval_date = draft["created_at"]
        approval_day = approval_date.day
        
        print(f"DEBUG: Draft created on {approval_date}, day = {approval_day}")
        
        # Check if this is a target-affecting leave change
        changes = json.loads(draft["changes_json"])
        old_data = json.loads(draft["old_json"])
        
        print(f"DEBUG: Changes: {changes}")
        print(f"DEBUG: Old data: {old_data}")
        
        is_target_leave_change = (
            changes.get("is_leave") == 1 and 
            changes.get("is_target_leave") == 1 and
            old_data.get("is_leave") == 0
        )
        
        print(f"DEBUG: Is target leave change: {is_target_leave_change}")
        
        target_affects = False
        working_days_change = 0
        target_leaves_change = 0
        
        if is_target_leave_change:
            # This is a new target-affecting leave
            working_days_change = -1
            target_leaves_change = 1
            
            # Check approval date rules
            if approval_day > 5:
                # After 5th - always affects target
                target_affects = True
                print(f"DEBUG: After 5th approval - target affected")
            elif approval_day <= 5:
                # Before 5th - check planned leaves count
                print(f"DEBUG: Before 5th approval - checking planned leaves")
                
                # Get current planned target-affecting leaves count
                cursor.execute("""
                    SELECT COUNT(*) as current_planned_target_leaves
                    FROM roster_days
                    WHERE user_id=%s AND roster_id=%s
                    AND is_leave=1 AND is_target_leave=1 AND is_planned_leave=1
                """, (draft["user_id"], draft["roster_id"]))
                current_planned_leaves = cursor.fetchone()["current_planned_target_leaves"]
                new_planned_leaves = current_planned_leaves + 1
                
                if new_planned_leaves >= 3:
                    # 3 or more planned leaves - affects target
                    target_affects = True
                    print(f"DEBUG: {new_planned_leaves} planned leaves (>=3) - target affected")
                else:
                    # Less than 3 planned leaves - no target impact
                    target_affects = False
                    print(f"DEBUG: {new_planned_leaves} planned leaves (<3) - no target impact")
        
        # NOTE: Don't update rosters here - let recalculate_target handle all roster updates
        # This prevents double-counting and duplicate updates

        # Call recalculate_target for any additional calculations
        print(f"DEBUG: About to call recalculate_target for user_id={draft['user_id']}, roster_id={draft['roster_id']}")
        from utils.target_utils import recalculate_target
        recalculate_target(
            draft["user_id"], draft["roster_id"], conn)
        print(f"DEBUG: recalculate_target completed")

        conn.commit()

        return jsonify({
            "status": 200,
            "message": "Approved & Applied"
        })

    except Exception as e:
        conn.rollback()
        return jsonify({"status": 500, "message": str(e)})

    finally:
        cursor.close()
        conn.close()

@roster_bp.route("/reject", methods=["POST"])
def reject():

    data = request.json
    draft_id = data.get("draft_id")

    conn = get_db_connection()
    cursor = conn.cursor()

    try:

        cursor.execute("""
            UPDATE roster_day_drafts
            SET status='rejected'
            WHERE draft_id=%s AND status='pending'
        """, (draft_id,))

        conn.commit()

        return jsonify({
            "status": 200,
            "message": "Rejected successfully"
        })

    except Exception as e:
        conn.rollback()
        return jsonify({"status": 500, "message": str(e)})

    finally:
        cursor.close()
        conn.close()

@roster_bp.route("/get_leave_history", methods=["POST"])
def get_leave_history():
    import json
    from routes.dashboard import get_subordinate_user_ids
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    try:
        data = request.json
        logged_in_user_id = data.get("logged_in_user_id")
        month_year = data.get("month_year")  # Optional filter
        status_filter = data.get("status")  # Optional: "approved", "rejected", or None for both

        if not logged_in_user_id:
            return jsonify({"success": False, "message": "logged_in_user_id is required"}), 400

        # Get leave types for reference
        cursor.execute("SELECT leave_type_id, leave_type_name FROM leave_types")
        leave_types = {row["leave_type_id"]: row["leave_type_name"] for row in cursor.fetchall()}

        # Get role of user
        cursor.execute("""
            SELECT r.role_name
            FROM tfs_user u
            JOIN user_role r ON u.role_id = r.role_id
            WHERE u.user_id = %s
        """, (logged_in_user_id,))

        user = cursor.fetchone()
        if not user:
            return jsonify({"success": False, "message": "User not found"}), 404

        role_name = user["role_name"].lower()

        # Build WHERE conditions based on role
        where_conditions = ["d.status IN ('approved', 'rejected', 'pending')"]
        params = []

        # Add status filter if specified
        if status_filter and status_filter in ["approved", "rejected", "pending"]:
            where_conditions.append("d.status = %s")
            params.append(status_filter)

        # Add month filter if specified
        if month_year:
            where_conditions.append("DATE_FORMAT(d.date, '%b%Y') = %s")
            params.append(month_year)

        # Add role-based user filtering using standard pattern from dashboard API
        subordinate_ids = get_subordinate_user_ids(cursor, role_name, logged_in_user_id)
        
        if subordinate_ids is None:
            # Admin/Super Admin - can see all data, no additional filtering needed
            pass
        else:
            # All other roles - can only see their team members' data
            if subordinate_ids:
                placeholders = ",".join(["%s"] * len(subordinate_ids))
                where_conditions.append(f"d.user_id IN ({placeholders})")
                params.extend(subordinate_ids)
            else:
                # No subordinate users found, return empty result
                return jsonify({
                    "success": True,
                    "data": [],
                    "count": 0
                })

        # Main query to get approved/rejected leave history
        query = f"""
            SELECT 
                d.draft_id,
                d.user_id,
                d.date,
                d.changes_json,
                d.old_json,
                d.status,
                d.created_at,
                d.edited_by,
                u.user_name,
                u.team_id,
                t.team_name,
                editor.user_name as edited_by_name
            FROM roster_day_drafts d
            LEFT JOIN tfs_user u ON d.user_id = u.user_id
            LEFT JOIN team t ON t.team_id = u.team_id
            LEFT JOIN tfs_user editor ON d.edited_by = editor.user_id
            WHERE {' AND '.join(where_conditions)}
            ORDER BY d.created_at DESC
        """
        cursor.execute(query, tuple(params))
        drafts = cursor.fetchall()

        # Process the results to format leave information
        result = []
        for draft in drafts:
            changes = json.loads(draft["changes_json"] or "{}")
            old_data = json.loads(draft["old_json"] or "{}")
            
            # Include all pending requests (not just leave changes)
            if draft["status"].lower() == "pending":
                # For pending requests, show all types of changes
                request_info = {
                    "draft_id": draft["draft_id"],
                    "user_id": draft["user_id"],
                    "user_name": draft["user_name"],
                    "team": {
                        "team_id": draft["team_id"],
                        "team_name": draft["team_name"]
                    },
                    "date": draft["date"],
                    "status": draft["status"].lower(),
                    # "status_display": draft["status_display"],
                    "requested_by": draft["edited_by_name"],
                    "requested_at": draft["created_at"].strftime("%Y-%m-%d %H:%M:%S"),
                    "changes": {
                        "from": {
                            "day_type": old_data.get("day_type"),
                            "is_leave": old_data.get("is_leave"),
                            "leave_type_id": old_data.get("leave_type_id")
                        },
                        "to": {
                            "day_type": changes.get("day_type"),
                            "is_leave": changes.get("is_leave"),
                            "leave_type_id": changes.get("leave_type_id")
                        }
                    }
                }
                
                # Add leave-specific info for leave requests
                if changes.get("is_leave") == 1:
                    leave_type_id = changes.get("leave_type_id")
                    request_info["leave_type"] = leave_types.get(leave_type_id, "Unknown") if leave_type_id else "Unknown"
                    request_info["leave_type_id"] = changes.get("leave_type_id")
                    request_info["is_planned"] = "yes" if changes.get("is_planned_leave") == 1 else "no"
                else:
                    # For non-leave requests, add action description
                    day_type = changes.get("day_type")
                    if day_type == "wfh":
                        request_info["action"] = "mark as wfh"
                    elif day_type == "half_day":
                        request_info["action"] = "mark as half day"
                    elif day_type == "weekoff":
                        request_info["action"] = "mark as weekoff"
                    elif day_type == "working":
                        request_info["action"] = "mark as working day"
                    else:
                        request_info["action"] = "mark as working day"  # default fallback
                
                result.append(request_info)
            elif draft["status"].lower() in ["approved", "rejected"] and changes.get("is_leave") == 1:
                # For approved/rejected leave requests only (historical)
                leave_info = {
                    "draft_id": draft["draft_id"],
                    "user_id": draft["user_id"],
                    "user_name": draft["user_name"],
                    "team": {
                        "team_id": draft["team_id"],
                        "team_name": draft["team_name"]
                    },
                    "date": draft["date"],
                    "leave_type": leave_types.get(changes.get("leave_type_id"), "Unknown") if changes.get("leave_type_id") else "Unknown",
                    "leave_type_id": changes.get("leave_type_id"),
                    "is_planned": "yes" if changes.get("is_planned_leave") == 1 else "no",
                    "status": draft["status"].lower(),
                    # "status_display": draft["status_display"],
                    "requested_by": draft["edited_by_name"],
                    "requested_at": draft["created_at"].strftime("%Y-%m-%d %H:%M:%S"),
                    "changes": {
                        "from": {
                            "day_type": old_data.get("day_type"),
                            "is_leave": old_data.get("is_leave"),
                            "leave_type_id": old_data.get("leave_type_id")
                        },
                        "to": {
                            "day_type": changes.get("day_type"),
                            "is_leave": changes.get("is_leave"),
                            "leave_type_id": changes.get("leave_type_id")
                        }
                    }
                }
                result.append(leave_info)

        return jsonify({
            "success": True,
            "data": result,
            "count": len(result)
        })
        return jsonify({"success": False, "message": str(e)}), 500
    finally:
        cursor.close()
        conn.close()


@roster_bp.route("/get_pending_drafts", methods=["POST"])
def get_pending_drafts():
    import json
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    # Get leave types for reference
    cursor.execute("SELECT leave_type_id, leave_type_name FROM leave_types")
    leave_types = {row["leave_type_id"]: row["leave_type_name"] for row in cursor.fetchall()}

    try:
        data = request.json
        logged_in_user_id = data.get("logged_in_user_id")

        # Get role of user
        cursor.execute("""
            SELECT r.role_name
            FROM tfs_user u
            JOIN user_role r ON u.role_id = r.role_id
            WHERE u.user_id = %s
        """, (logged_in_user_id,))

        user = cursor.fetchone()

        if not user or user["role_name"].lower() != "super admin":
            return jsonify({"success": False, "message": "Unauthorized"}), 403

        # Group pending drafts by user and date
        cursor.execute("""
            SELECT 
                d.draft_id,
                d.user_id,
                d.date,
                d.changes_json,
                d.old_json,
                d.created_at,
                u.user_name,
                u.team_id,
                t.team_name,
                editor.user_name as edited_by_name
            FROM roster_day_drafts d
            LEFT JOIN tfs_user u ON d.user_id = u.user_id
            LEFT JOIN team t ON t.team_id = u.team_id
            LEFT JOIN tfs_user editor ON d.edited_by = editor.user_id
            WHERE d.status = 'pending'
        """)

        rows = cursor.fetchall()

        result = []
        for r in rows:
            key = (r["user_id"], r["date"])
            if key not in result:
                item = {
                    "user_id": r["user_id"],
                    "user_name": r["user_name"],
                    "team": {
                        "team_id": r["team_id"],
                        "team_name": r["team_name"]
                    },
                    "date": str(r["date"]),
                    "requested_by": r["edited_by_name"],
                    "requested_at": r["created_at"],
                    "request": {},
                    "draft_ids": []
                }
            
            changes = json.loads(r["changes_json"]) if r["changes_json"] else {}
            
            # Build user-friendly request info with comprehensive details
            req = item["request"]
            
            # Include all pending request types with comprehensive information
            if changes.get("is_leave") == 1:
                # Leave request
                req["leave"] = "yes"
                leave_type_id = changes.get("leave_type_id")
                req["leave_type"] = leave_types.get(leave_type_id, "Unknown") if leave_type_id else "Unknown"
                req["leave_type_id"] = changes.get("leave_type_id")
                req["is_planned"] = "yes" if changes.get("is_planned_leave") == 1 else "no"
                req["action"] = "request leave"
            elif changes.get("is_leave") == 0:
                # Non-leave requests (WFH, half day, etc.)
                req["leave"] = "no"
                day_type = changes.get("day_type")
                if day_type == "wfh":
                    req["action"] = "mark as wfh"
                elif day_type == "half_day":
                    req["action"] = "mark as half day"
                elif day_type == "weekoff":
                    req["action"] = "mark as weekoff"
                elif day_type == "working":
                    req["action"] = "mark as working day"
                else:
                    req["action"] = "mark as working day"  # default fallback
            
            if changes.get("shift"):
                req["shift"] = changes["shift"]
            
            item["draft_ids"].append(r["draft_id"])
            item["draft_id"] = item["draft_ids"][0]  # Use first draft_id for approve/reject
            del item["draft_ids"]  # Remove internal field
            result.append(item)

        try:
            response = jsonify({
                "success": True,
                "count": len(result),
                "pending_requests": result
            }), 200
            return response
        except Exception as e:
            return jsonify({"success": False, "message": str(e)}), 500

    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500

    finally:
        cursor.close()
        conn.close()

@roster_bp.route("/get_rosters", methods=["POST"])
def get_rosters():
    """
    Get comprehensive roster data with all flags and target calculation information
    Returns working_days, target_leaves, base_target, final_target for users
    """
    data = request.json
    logged_in_user_id = data.get("logged_in_user_id")
    month_year = data.get("month_year")  # Optional filter
    target_user_id = data.get("user_id")  # Optional: for specific user

    if not logged_in_user_id:
        return jsonify({"success": False, "message": "logged_in_user_id is required"}), 400

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    try:
        # Get user role for access control
        cursor.execute("""
            SELECT r.role_name 
            FROM tfs_user u
            JOIN user_role r ON u.role_id = r.role_id
            WHERE u.user_id = %s AND u.is_active = 1
        """, (logged_in_user_id,))
        user = cursor.fetchone()

        if not user:
            return jsonify({"success": False, "message": "User not found"}), 404

        role_name = user["role_name"].lower()

        # Build WHERE conditions based on role
        where_conditions = []
        params = []

        # Add month filter if specified
        if month_year:
            where_conditions.append("DATE_FORMAT(r.month_year, '%b%Y') = %s")
            params.append(month_year)

        # Add role-based user filtering
        if role_name == "agent":
            where_conditions.append("r.user_id = %s")
            params.append(logged_in_user_id)
        elif role_name in ["assistant manager", "manager", "project manager", "qa"]:
            # Managers can see their team members
            logged_id_str = str(logged_in_user_id)
            where_conditions.append("""
                (
                    JSON_CONTAINS(u.asst_manager_id, %s) OR
                    JSON_CONTAINS(u.project_manager_id, %s) OR
                    JSON_CONTAINS(u.qa_id, %s) OR
                    u.asst_manager_id = %s OR
                    u.project_manager_id = %s OR
                    u.qa_id = %s OR
                    u.user_id = %s
                )
            """)
            params.extend([logged_id_str, logged_id_str, logged_id_str, 
                         logged_id_str, logged_id_str, logged_in_user_id])
        elif role_name in ["admin", "super admin"]:
            # Admin/Super Admin - can see all or specific user
            if target_user_id:
                where_conditions.append("r.user_id = %s")
                params.append(target_user_id)
            # No additional filtering needed for admin
        else:
            return jsonify({"success": False, "message": "Unauthorized role"}), 403

        # Main query to get comprehensive roster data
        query = f"""
            SELECT 
                r.roster_id,
                r.user_id,
                u.user_name,
                u.team_id,
                t.team_name,
                r.month_year,
                r.working_days,
                r.target_leaves,
                r.base_target,
                r.final_target,
                r.extra_assigned_hours,
                -- Target calculation flags
                CASE 
                    WHEN r.target_leaves > 0 THEN 'Target affected'
                    ELSE 'No target impact'
                END as target_status,
                -- Early month planned leave flag
                CASE 
                    WHEN EXISTS (
                        SELECT 1 FROM roster_day_drafts d
                        WHERE d.user_id = r.user_id 
                        AND d.roster_id = r.roster_id
                        AND d.status = 'approved'
                        AND DAY(d.created_at) <= 5
                        AND JSON_EXTRACT(d.changes_json, '$.is_planned_leave') = 1
                        AND (
                            SELECT COUNT(*) FROM roster_days rd
                            WHERE rd.user_id = d.user_id 
                            AND rd.roster_id = d.roster_id
                            AND rd.is_leave = 1 
                            AND rd.is_target_leave = 1 
                            AND rd.is_planned_leave = 1
                        ) >= 3
                    ) THEN 'Early month planned leaves'
                    ELSE 'Normal rules apply'
                END as early_month_status,
                -- Last target calculation
                (SELECT CONCAT(
                    'Last calculated: ', 
                    IFNULL(MAX(d.created_at), 'Never')
                ) FROM roster_day_drafts d
                WHERE d.user_id = r.user_id 
                AND d.roster_id = r.roster_id
                AND d.status = 'approved'
                AND JSON_EXTRACT(d.changes_json, '$.is_target_leave') = 1
                ) as last_target_calculation
            FROM rosters r
            LEFT JOIN tfs_user u ON r.user_id = u.user_id
            LEFT JOIN team t ON u.team_id = t.team_id
            WHERE {' AND '.join(where_conditions)}
            ORDER BY u.user_name, r.month_year DESC
        """

        cursor.execute(query, tuple(params))
        rosters = cursor.fetchall()

        # Format response with all flags and information
        result = []
        for roster in rosters:
            result_item = {
                "roster_id": roster["roster_id"],
                "user_id": roster["user_id"],
                "user_name": roster["user_name"],
                "team": {
                    "team_id": roster["team_id"],
                    "team_name": roster["team_name"]
                },
                "month_year": roster["month_year"],
                "working_days": roster["working_days"],
                "target_leaves": roster["target_leaves"],
                "base_target": roster["base_target"],
                "final_target": roster["final_target"],
                "extra_assigned_hours": roster["extra_assigned_hours"],
                "flags": {
                    "target_affected": roster["target_leaves"] > 0,
                    "target_status": roster["target_status"],
                    "early_month_planned": roster["early_month_status"] == "Early month planned leaves"
                },
                "last_target_calculation": roster["last_target_calculation"]
            }
            result.append(result_item)

        return jsonify({
            "success": True,
            "data": result,
            "count": len(result)
        })

    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500

    finally:
        cursor.close()
        conn.close()

@roster_bp.route("/auto-create", methods=["POST"])
def trigger_roster():
    from utils.roster_utils import auto_create_rosters

    data = request.json or {}
    month_year = data.get("month_year")
    user_id = data.get("logged_in_user_id")

    result = auto_create_rosters(month_year, user_id)

    return jsonify(result)