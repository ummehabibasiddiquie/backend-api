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
        report_date = datetime.strptime("2026-02-27", "%Y-%m-%d").date()
        report_month = report_date.strftime('%b%Y').upper() 

        logging.info(f"Fetching data for report date: {report_date}")

        cursor.execute(
            """
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
             AND umt.month_year = %s
            WHERE u.is_delete != 0
              AND u.is_active=1
              AND r.role_name='Agent'
            ORDER BY t.team_name, u.user_name
        """,
            (report_month,),
        )
        print()

        all_users = cursor.fetchall()

        print(f"Users fetched: {len(all_users)}")

        if not all_users:
            return report_date, []

        user_ids = [u["user_id"] for u in all_users]
        in_ph = ",".join(["%s"] * len(user_ids))

        # -------------------------
        # DAILY WORKED HOURS
        # -------------------------

        cursor.execute(
            f"""
            SELECT user_id,
                SUM(production / NULLIF(tenure_target,0)) AS worked_hours
            FROM task_work_tracker
            WHERE DATE(date_time)=%s
            AND user_id IN ({in_ph})
            AND is_active=1
            GROUP BY user_id
        """,
            [report_date] + user_ids,
        )

        tracker_daily = {
            r["user_id"]: float(r["worked_hours"] or 0) for r in cursor.fetchall()
        }
        print(tracker_daily)

        # -------------------------
        # MTD HOURS
        # -------------------------

        month_start = report_date.replace(day=1)

        cursor.execute(
            f"""
            SELECT user_id,
                SUM(production / NULLIF(tenure_target,0)) AS mtd_hours
            FROM task_work_tracker
            WHERE DATE(date_time) BETWEEN %s AND %s
            AND user_id IN ({in_ph})
            AND is_active=1
            GROUP BY user_id
        """,
            [month_start, report_date] + user_ids,
        )

        tracker_mtd = {
            r["user_id"]: float(r["mtd_hours"] or 0) for r in cursor.fetchall()
        }

        # -------------------------
        # LAST QC SCORE
        # -------------------------

        cursor.execute(
            f"""
            SELECT t1.user_id, t1.qc_score, t1.assigned_hours, DATE(t1.date) AS qc_date
            FROM temp_qc t1
            JOIN (
                SELECT user_id, MAX(DATE(date)) AS last_qc_date
                FROM temp_qc
                WHERE qc_score IS NOT NULL
                AND DATE(date) <= %s
                AND user_id IN ({in_ph})
                GROUP BY user_id
            ) t2
            ON t1.user_id = t2.user_id AND DATE(t1.date) = t2.last_qc_date
            """,
            [report_date] + user_ids,
        )

        last_qc = {r["user_id"]: r for r in cursor.fetchall()}

        # -------------------------
        # CALCULATIONS
        # -------------------------

        day_of_month = report_date.day

        for u in all_users:

            uid = u["user_id"]

            worked_hours = tracker_daily.get(uid, 0)
            mtd_hours = tracker_mtd.get(uid, 0)

            qc_data = last_qc.get(uid, {})

            u["daily_worked_hours"] = worked_hours
            u["mtd_hours"] = mtd_hours
            u["qc_score"] = qc_data.get("qc_score")
            u["assigned_hours"] = float(qc_data.get("assigned_hours") or 0)

            qc_date = qc_data.get("qc_date")
            if qc_date:
                if isinstance(qc_date, datetime):
                    u["qc_date"] = qc_date.strftime("%Y-%m-%d")
                else:
                    u["qc_date"] = str(qc_date)
            else:
                u["qc_date"] = None

            monthly_target = float(u.get("monthly_target") or 0)
            extra_assigned = float(u.get("extra_assigned_hours") or 0)
            working_days = float(u.get("working_days") or 0)

            # Monthly Goal
            monthly_goal = monthly_target + extra_assigned

            # Pending Goal
            pending_goal = max(0, monthly_goal - mtd_hours)
            
            # Count days already worked in the month
            # days_worked_so_far = len([1 for t in tracker_mtd if t <= report_date])  # you can also count task_work_tracker entries
            
            # Count of days worked in the month per user
            cursor.execute(
                f"""
                SELECT user_id, COUNT(DISTINCT DATE(date_time)) AS days_worked
                FROM task_work_tracker
                WHERE DATE(date_time) BETWEEN %s AND %s
                AND user_id IN ({in_ph})
                AND is_active=1
                GROUP BY user_id
                """,
                [month_start, report_date] + user_ids,
            )
            days_worked_map = {r["user_id"]: int(r["days_worked"]) for r in cursor.fetchall()}

            days_worked_so_far = days_worked_map.get(uid, 0)

            # Remaining Days
            remaining_days = max(0, working_days - days_worked_so_far)

            # Daily Required Hours
            daily_required_hours = (
                pending_goal / remaining_days if remaining_days else 0
            )

            u["monthly_goal"] = monthly_goal
            u["pending_goal"] = pending_goal
            u["daily_required_hours"] = daily_required_hours

        return report_date, all_users

    except Exception as e:
        print(f"Error fetching data: {str(e)}")

    finally:
        cursor.close()
        conn.close()


# -------------------------------
# GENERATE HTML
# -------------------------------

def generate_html(report_date, data_rows):

    day_str = report_date.strftime("%d")
    month_year = report_date.strftime("%B %Y")

    html = f"<p style='font-family:Arial;'>Dear All,<br><br>Tracker Report of <b>{day_str} {month_year}</b></p>"

    # Worked and Assigned date = report date
    worked_date = report_date.strftime("%Y-%m-%d")
    assigned_date = worked_date

    # # Find latest QC date from data
    # latest_qc_date = max(qc_dates) if qc_dates else None
    # latest_qc_date_str = latest_qc_date.strftime("%Y-%m-%d") if latest_qc_date else None

    # Find latest QC date from the fetched data
    qc_dates = [u.get("qc_date") for u in data_rows if u.get("qc_date")]
    latest_qc_date_str = max(qc_dates) if qc_dates else None
    
    qc_header = f"QC Score ({latest_qc_date_str})" if latest_qc_date_str else "QC Score"

    assigned_header = f"Assigned Hours ({assigned_date})"
    worked_header = f"Worked Hours ({worked_date})"
    # qc_header = f"QC Score ({latest_qc_date})" if latest_qc_date else "QC Score"

    html += f"""
    <table width="100%" border="1" cellpadding="4" cellspacing="0"
    style="border-collapse: collapse; font-family:Arial; font-size:12px;">
    <tr style="background:#d9e1f2;font-weight:bold;">
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
            <td align="right">{u.get('assigned_hours',0):.2f}</td>
            <td align="right">{u.get('daily_worked_hours',0):.2f}</td>
            <td align="right">{'' if u.get('qc_score') is None else u.get('qc_score')}</td>
            <td align="right">{u.get('daily_required_hours',0):.2f}</td>
            <td align="right">{u.get('mtd_hours',0):.2f}</td>
            <td align="right">{u.get('monthly_goal',0):.2f}</td>
            <td align="right">{u.get('pending_goal',0):.2f}</td>
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

        if not data_rows:
            logging.info("No data found")
            exit(0)

        html_body = generate_html(report_date, data_rows)

        subject = f"{report_date.strftime('%d %B %Y')} Daily Tracker Report"

        send_email(subject, html_body)

        logging.info("Daily tracker report sent successfully")

    except Exception as e:

        logging.exception(f"Report failed: {str(e)}")