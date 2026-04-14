from apscheduler.schedulers.background import BackgroundScheduler
from datetime import datetime
from config import get_db_connection
from calendar import monthrange

def auto_create_rosters_job():
    from routes.roster_routes import create_roster  # reuse API logic if needed

    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)

        today = datetime.now()

        # calculate next month
        if today.month == 12:
            year = today.year + 1
            month = 1
        else:
            year = today.year
            month = today.month + 1

        month_year = datetime(year, month, 1).strftime("%b%Y").upper()

        print(f"[SCHEDULER] Creating roster for {month_year}")

        total_days = monthrange(year, month)[1]

        # get users
        cursor.execute("SELECT user_id FROM tfs_user WHERE is_active=1")
        users = cursor.fetchall()

        for user in users:
            user_id = user['user_id']

            # check already exists
            cursor.execute("""
                SELECT roster_id FROM rosters
                WHERE user_id=%s AND month_year=%s
            """, (user_id, month_year))

            if cursor.fetchone():
                continue

            # create roster
            cursor.execute("""
                INSERT INTO rosters (user_id, month_year, total_days)
                VALUES (%s,%s,%s)
            """, (user_id, month_year, total_days))

            roster_id = cursor.lastrowid

            # create days
            for day in range(1, total_days + 1):
                date_obj = datetime(year, month, day)
                weekday = date_obj.weekday()

                if weekday in [5,6]:
                    day_type = 'weekoff'
                else:
                    day_type = 'working'

                # holiday check
                cursor.execute("""
                    SELECT 1 FROM holidays WHERE holiday_date=%s
                """, (date_obj.date(),))
                if cursor.fetchone():
                    day_type = 'holiday'

                cursor.execute("""
                    INSERT INTO roster_days
                    (roster_id,user_id,date,day_type)
                    VALUES (%s,%s,%s,%s)
                """, (roster_id,user_id,date_obj.date(),day_type))

        conn.commit()
        cursor.close()
        conn.close()

        print(f"[SCHEDULER] Roster created successfully for {month_year}")

    except Exception as e:
        print(f"[SCHEDULER ERROR]: {e}")


def start_scheduler():
    scheduler = BackgroundScheduler()

    # 🔥 MAIN JOB
    scheduler.add_job(
        auto_create_rosters_job,
        'cron',
        day='last',
        hour=23,
        minute=55
    )

    scheduler.start()
    print("✅ Scheduler Started")