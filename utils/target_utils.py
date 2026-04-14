def recalculate_target(user_id, roster_id, conn):
    """
    Calculate target based on ONLY the current draft change
    This function works with the approve function and processes individual changes
    """
    cursor = conn.cursor(dictionary=True)

    # 1. Get current roster data
    cursor.execute("""
        SELECT working_days, base_target, extra_assigned_hours, target_leaves
        FROM rosters
        WHERE roster_id=%s AND user_id=%s
    """, (roster_id, user_id))
    roster_data = cursor.fetchone()
    current_working_days = roster_data["working_days"]
    current_base_target = roster_data["base_target"]
    extra_assigned_hours = roster_data["extra_assigned_hours"]
    current_target_leaves = roster_data["target_leaves"]

    # 2. Get most recent approved draft to understand the change
    cursor.execute("""
        SELECT changes_json, old_json, created_at
        FROM roster_day_drafts
        WHERE user_id=%s AND roster_id=%s
        AND status='approved'
        ORDER BY draft_id DESC
        LIMIT 1
    """, (user_id, roster_id))
    latest_draft = cursor.fetchone()

    if not latest_draft:
        # No recent change, nothing to recalculate
        return

    import json
    changes = json.loads(latest_draft["changes_json"])
    old_data = json.loads(latest_draft["old_json"])
    approval_date = latest_draft["created_at"]
    approval_day = approval_date.day

    # 3. Calculate changes based on what actually changed in THIS draft only
    working_days_change = 0
    target_leaves_change = 0
    target_affects = False

    # Check if this is a new target-affecting leave being added
    if (changes.get("is_leave") == 1 and 
        changes.get("is_target_leave") == 1 and
        old_data.get("is_leave") == 0):
        
        # New target-affecting leave added
        working_days_change = -1
        target_leaves_change = 1
        
        # Check if target affects based on approval date and planned rules
        if approval_day > 5:
            # After 5th - always affects target
            target_affects = True
        elif approval_day <= 5:
            # Before 5th - check planned leaves count from roster_days (only current month approved leaves)
            cursor.execute("""
                SELECT COUNT(*) as current_planned_target_leaves
                FROM roster_days rd
                JOIN rosters r ON rd.roster_id = r.roster_id
                WHERE rd.user_id=%s AND rd.roster_id=%s
                AND rd.is_leave=1 AND rd.is_target_leave=1 AND rd.is_planned_leave=1
                AND DATE_FORMAT(rd.date, '%Y-%m') = DATE_FORMAT(r.month_year, '%Y-%m')
            """, (user_id, roster_id))
            current_planned_leaves = cursor.fetchone()["current_planned_target_leaves"]
            new_planned_leaves = current_planned_leaves + 1
            
            if new_planned_leaves >= 3:
                # 3 or more planned leaves - affects target
                target_affects = True
            else:
                # Less than 3 planned leaves - no target impact
                target_affects = False

    # Check if this is a target-affecting leave being removed (becoming working day)
    elif (changes.get("is_leave") == 0 and 
              old_data.get("is_leave") == 1 and 
              old_data.get("is_target_leave") == 1):
        
        # Target-affecting leave removed
        working_days_change = 1
        target_leaves_change = -1
        
        # Removing target-affecting leave always affects target (reduces impact)
        target_affects = True

    # 4. Calculate new values based on THIS change only
    if working_days_change != 0 or target_leaves_change != 0:
        new_working_days = current_working_days + working_days_change
        new_target_leaves = current_target_leaves + target_leaves_change
        
        if target_affects:
            # Target-affecting change - recalculate base target
            new_base_target = new_working_days * 9
        else:
            # No target impact - keep current base target
            new_base_target = current_base_target
            
        new_final_target = new_base_target + extra_assigned_hours

        # 5. Update roster with new values
        cursor.execute("""
            UPDATE rosters
                SET working_days=%s,
                    target_leaves=%s,
                    base_target=%s,
                    final_target=%s
                WHERE roster_id=%s
            """, (new_working_days, new_target_leaves, new_base_target, new_final_target, roster_id))

        conn.commit()
    else:
        # Not a target-affecting leave change, no target recalculation needed
        pass