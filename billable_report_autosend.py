# daily_tracker_report_cron_v2.py

from dotenv import load_dotenv
from pathlib import Path
import os
import mysql.connector
import smtplib
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import logging

# -------------------------------
# CONFIG
# -------------------------------
load_dotenv(dotenv_path=Path(__file__).resolve().parent / ".env")

RECIPIENTS = ["ummehabiba.siddiquie@transformsolution.net"]
CC_RECIPIENTS = []

LOG_FILE = Path(__file__).resolve().parent / "daily_tracker_report.log"
logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)

# -------------------------------
# DB CONNECTION
# -------------------------------
def get_db_connection():
    return mysql.connector.connect(
        host=os.getenv("DB_HOST"),
        port=int(os.getenv("DB_PORT", 3306)),
        user=os.getenv("DB_USERNAME"),
        password=os.getenv("DB_PASSWORD"),
        database=os.getenv("DB_DATABASE", "tfs_hrms"),
    )

# -------------------------------
# FETCH DATA
# -------------------------------
def fetch_data():
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    try:
        # today = datetime.now().date()
        # report_date = today - timedelta(days=1)
        
        # TEST DATE
        report_date = datetime.strptime("2026-03-03", "%Y-%m-%d").date()

        logging.info(f"Fetching data for report date: {report_date}")

        cursor.execute("""
            SELECT u.user_id, u.user_name, t.team_id, t.team_name,
                   COALESCE(umt.monthly_target,0) AS monthly_target,
                   COALESCE(umt.extra_assigned_hours,0) AS extra_assigned_hours,
                   COALESCE(umt.working_days,0) AS working_days
            FROM tfs_user u
            JOIN user_role r ON u.role_id = r.role_id
            LEFT JOIN team t ON u.team_id = t.team_id
            LEFT JOIN user_monthly_tracker umt
              ON umt.user_id = u.user_id
             AND umt.is_active=1
             AND umt.month_year = DATE_FORMAT(%s, '%%b%%Y')
            WHERE u.is_delete != 0
              AND u.is_active=1
              AND r.role_name='Agent'
            ORDER BY t.team_name, u.user_name
        """, (report_date,))

        all_users = cursor.fetchall()
        
        # logging.info(f"Users fetched: {len(all_users)}")
        print(f"Users fetched: {len(all_users)}")

        if not all_users:
            print(f"No users found for report date: {report_date}") 
            return report_date, []
            

        user_ids = [u['user_id'] for u in all_users]
        in_ph = ",".join(["%s"] * len(user_ids))
        print(f"User IDs for IN clause: {user_ids}")    

        # -------------------------
        # YESTERDAY WORKED HOURS
        # -------------------------
        cursor.execute(f"""
            SELECT user_id,
                SUM(production / NULLIF(tenure_target,0)) AS worked_hours
            FROM task_work_tracker
            WHERE DATE(date_time)=%s
            AND user_id IN ({in_ph})
            AND is_active=1
            GROUP BY user_id
        """, [report_date] + user_ids)

        tracker_daily = {
            r['user_id']: {
                "worked_hours": float(r["worked_hours"] or 0),
                "date": r["work_date"]
            }
            for r in cursor.fetchall()
        }

        # -------------------------
        # MTD HOURS
        # -------------------------
        month_start = report_date.replace(day=1)

        cursor.execute(f"""
            SELECT user_id,
                SUM(production / NULLIF(tenure_target,0)) AS mtd_hours
            FROM task_work_tracker
            WHERE DATE(date_time) BETWEEN %s AND %s
            AND user_id IN ({in_ph})
            AND is_active=1
            GROUP BY user_id
        """, [month_start, report_date] + user_ids)

        tracker_mtd = {
            r['user_id']: float(r['mtd_hours'] or 0)
            for r in cursor.fetchall()
        }

        # -------------------------
        # LAST QC SCORE
        # -------------------------
        cursor.execute(f"""
            SELECT t1.user_id, t1.qc_score, t1.assigned_hours, t1.date
            FROM temp_qc t1
            JOIN (
                SELECT user_id, MAX(date) last_qc_date
                FROM temp_qc
                WHERE qc_score IS NOT NULL
                AND date < %s
                AND user_id IN ({in_ph})
                GROUP BY user_id
            ) t2
            ON t1.user_id=t2.user_id AND t1.date=t2.last_qc_date
        """, [report_date] + user_ids)

        last_qc = {r['user_id']: r for r in cursor.fetchall()}

        # -------------------------
        # COMBINE DATA
        # -------------------------
        for u in all_users:
            print(f"Processing user: {u['user_name']} (ID: {u['user_id']})")

            uid = u["user_id"]

            worked_data = tracker_daily.get(uid, {})
            worked_hours = worked_data.get("worked_hours", 0)

            mtd_hours = tracker_mtd.get(uid, 0)

            qc_data = last_qc.get(uid, {})

            u["daily_worked_hours"] = worked_hours
            u["mtd_hours"] = mtd_hours

            u["qc_score"] = qc_data.get("qc_score")
            u["assigned_hours"] = float(qc_data.get("assigned_hours") or 0)

            # -------------------------
            # DATE HEADERS
            # -------------------------
            worked_date = worked_data.get("date")
            
            if worked_date:
                if isinstance(worked_date, str):
                    worked_date = datetime.strptime(worked_date, "%Y-%m-%d")
                u["worked_hours_date"] = worked_date.strftime("%Y-%m-%d")
            else:
                u["worked_hours_date"] = None

            # u["worked_hours_date"] = worked_date.strftime("%Y-%m-%d") if worked_date else None

            # Assigned hours must use SAME date as worked hours
            u["assigned_hours_date"] = u["worked_hours_date"]

            qc_date = qc_data.get("date")
            if qc_date:
                if isinstance(qc_date, str):
                    qc_date = datetime.strptime(qc_date, "%Y-%m-%d")
                u["qc_date"] = qc_date.strftime("%Y-%m-%d")
            else:
                u["qc_date"] = None
            # u["qc_date"] = qc_date.strftime("%Y-%m-%d") if qc_date else None

            # -------------------------
            # CALCULATIONS
            # -------------------------
            monthly_target = float(u.get("monthly_target") or 0)
            extra_assigned = float(u.get("extra_assigned_hours") or 0)
            working_days = int(u.get("working_days") or 0)

            monthly_goal = monthly_target + extra_assigned
            pending_goal = max(0, monthly_goal - mtd_hours)

            remaining_days = max(0, working_days - 1)

            daily_required_hours = (
                pending_goal / remaining_days if remaining_days else 0
            )

            u["monthly_goal"] = monthly_goal
            u["pending_goal"] = pending_goal
            u["daily_required_hours"] = daily_required_hours
            print(f"User: {u['user_name']}, Worked Hours: {worked_hours}, MTD Hours: {mtd_hours}, QC Score: {u['qc_score']}, Daily Required Hours: {daily_required_hours:.2f}")

        # IMPORTANT
        return report_date, all_users

    except Exception as e:
        print(f"Error fetching data: {str(e)}")
    finally:
        cursor.close()
        conn.close()


# -------------------------------
# GENERATE HTML
# -------------------------------
# -------------------------------
# GENERATE HTML WITH DATES IN HEADER
# -------------------------------
def generate_html(report_date, data_rows):
    day_str = report_date.strftime("%d")
    month_year = report_date.strftime("%B %Y")
    html = f"<p style='font-family:Arial;'>Dear All,<br><br>Tracker Report of <b>{day_str} {month_year}</b></p>"

    # Determine the latest dates for each column
    # Determine the latest dates for each column
    worked_date_str = report_date.strftime("%Y-%m-%d")
    assigned_date_str = worked_date_str

    latest_qc_date = max(
        [u.get("qc_date") for u in data_rows if u.get("qc_date")],
        default=None
    )

    assigned_header = f"Assigned Hours ({assigned_date_str})"
    worked_header = f"Worked Hours ({worked_date_str})"
    qc_header = f"QC Score ({latest_qc_date})" if latest_qc_date else "QC Score"

    html += f"""
    <table width="100%" border="1" cellpadding="4" cellspacing="0"
       style="border-collapse: collapse; font-family:Arial; font-size: 12px; margin-bottom:10px;">
        <tr style="background-color:#d9e1f2; font-weight:bold;">
            <th>Team</th>
            <th>Team Member</th>
            <th>{assigned_header}</th>
            <th>{worked_header}</th>
            <th>{qc_header}</th>
            <th>Daily Required Hours</th>
            <th>MTD Hours</th>
            <th>Monthly Goal</th>
            <th>Pending Goal</th>
        </tr>
    """

    for u in data_rows:
        html += f"""
        <tr>
            <td>{u.get('team_name') or ''}</td>
            <td>{u['user_name']}</td>
            <td style="text-align:right;">{u.get('assigned_hours',0):.2f}</td>
            <td style="text-align:right;">{u.get('daily_worked_hours',0):.2f}</td>
            <td style="text-align:right;">{'' if u.get('qc_score') is None else u.get('qc_score')}</td>
            <td style="text-align:right;">{u.get('daily_required_hours',0):.2f}</td>
            <td style="text-align:right;">{u.get('mtd_hours',0):.2f}</td>
            <td style="text-align:right;">{u.get('monthly_goal',0):.2f}</td>
            <td style="text-align:right;">{u.get('pending_goal',0):.2f}</td>
        </tr>
        """

    html += "</table>"
    return html
# -------------------------------
# SEND EMAIL
# -------------------------------
def send_email(subject, html_body):
    host = os.getenv("SMTP_HOST")
    port = int(os.getenv("SMTP_PORT", "587"))
    user = os.getenv("SMTP_USER")
    password = os.getenv("SMTP_PASS")

    msg = MIMEMultipart("alternative")
    msg["From"] = user
    msg["To"] = ", ".join(RECIPIENTS)
    msg["Cc"] = ", ".join(CC_RECIPIENTS)
    msg["Subject"] = subject
    msg.attach(MIMEText(html_body, "html"))

    all_recipients = RECIPIENTS + CC_RECIPIENTS
    with smtplib.SMTP(host, port) as server:
        server.starttls()
        server.login(user, password)
        server.sendmail(user, all_recipients, msg.as_string())

# -------------------------------
# MAIN
# -------------------------------
if __name__ == "__main__":
    try:
        report_date, data_rows = fetch_data()
        logging.info(f"Rows returned: {len(data_rows)}")
        print(f"Rows returned: {len(data_rows)}")
        if not data_rows:
            logging.info(f"No data found for {report_date}, email not sent.")
            exit(0)

        html_body = generate_html(report_date, data_rows)
        subject = f"{report_date.strftime('%d %B %Y')} Daily Tracker Report"
        send_email(subject, html_body)
        logging.info(f"[SUCCESS] Daily tracker report sent for {report_date}")

    except Exception as e:
        logging.exception(f"[ERROR] Failed to generate/send report: {str(e)}")