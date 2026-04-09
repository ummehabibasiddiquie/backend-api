# send_tracker_report_full_day.py

from dotenv import load_dotenv
from pathlib import Path
import os
import mysql.connector
import smtplib
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from collections import defaultdict

# Load .env
load_dotenv(dotenv_path=Path(__file__).resolve().parent / ".env")

RECIPIENTS = [
    "ummehabiba.siddiquie@transformsolution.net",
    "mohsin.pathan@transformsolution.net",
    "dharmesh.jotania@transformsolution.net",
    "venkateshwaran.iyer@transformsolution.net",
    "yahya.irani@transformsolution.net",
    "amit.mandviwala@transformsolution.net",
    "sriman.narayan@transformsolution.net",
    "shirin.gafoor@transformsolution.net",
    "avinash.dwivedi@transformsolution.net",
    "jimil.kinariwala@transformsolution.net",
    "manas.pradhan@transformsolution.net"
]

CC_RECIPIENTS = [
    "ashfaq@transformsolution.com",
    "seema@transformsolution.com"
]

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
        
        today = datetime.now().date()
        report_date = today - timedelta(days=1)

        # TEST DATE
        # report_date = datetime.strptime("2026-04-06", "%Y-%m-%d").date()
        
        print(f"Fetching data for report date: {report_date}")  

        # Get all active users
        cursor.execute("""
            SELECT distinct u.user_id, u.user_name
            FROM tfs_user u
            JOIN user_role r ON u.role_id = r.role_id
            WHERE u.is_delete != 0 AND u.is_active = 1
            AND r.role_name = 'Agent'
        """)
        all_users = cursor.fetchall()

        # Get tracker data
        cursor.execute("""
            SELECT 
                t.tracker_id,
                t.date_time,
                u.user_id,
                u.user_name,
                p.task_name,
                t.production,
                t.tenure_target,
                t.billable_hours
            FROM task_work_tracker t
            JOIN tfs_user u ON t.user_id = u.user_id
            JOIN task p ON t.task_id = p.task_id
            WHERE DATE(t.date_time) = %s
              AND u.is_delete != 0 AND t.is_active = 1
            ORDER BY u.user_name, t.date_time
        """, (report_date,))

        tracker_data = cursor.fetchall()

        return report_date, all_users, tracker_data

    finally:
        cursor.close()
        conn.close()

# -------------------------------
# GENERATE HTML
# -------------------------------
def generate_html(report_date, all_users, tracker_data):

    day_str = report_date.strftime("%d")
    month_year = report_date.strftime("%B %Y")

    # title = f"{day_str} {month_year} Production Report"

    html = f"""
    <p style="font-family:Arial;">Dear All,<br><br>
    Tracker Report of <b>{day_str} {month_year}</b></p>
    """

    # Identify users with entries
    users_with_entries = set([row["user_id"] for row in tracker_data])
    leave_users = [u for u in all_users if u["user_id"] not in users_with_entries]

    # ---------------- Leave Table ----------------
    # html += """
    # <table border="1" cellpadding="6" cellspacing="0"
    #    style="border-collapse: collapse; font-family:Arial; margin-bottom:30px;">
    #     <tr style="background-color:#f7941d; color:black; font-weight:bold;">
    #         <th>Name</th>
    #         <th>Status</th>
    #     </tr>
    # """

    # for user in leave_users:
    #     html += f"""
    #     <tr style="background-color:#a9d18e;">
    #         <td>{user['user_name']}</td>
    #         <td>Absent</td>
    #     </tr>
    #     """

    # html += "</table>"

    # ---------------- Production Table ----------------
    full_title = report_date.strftime("%d %B %Y") + " Production Report"
    html += f"""
    <table width="75%" border="1" cellpadding="4" cellspacing="0"
       style="border-collapse: collapse; font-family:Arial; font-size: 12px; margin-bottom:10px;">
        <tr>
            <th colspan="6" style="text-align:center; font-size:18px;">
                {full_title}
            </th>
        </tr>
        <tr style="background-color:#d9e1f2; font-weight:bold;">
            <th>Name</th>
            <th>Created</th>
            <th>Task Name</th>
            <th>Target</th>
            <th>Production</th>
            <th>Billable Hours</th>
        </tr>
    """

    grouped = defaultdict(list)
    for row in tracker_data:
        grouped[row["user_name"]].append(row)

    grand_target = 0
    grand_production = 0
    grand_billable = 0

    for user, rows in grouped.items():

        user_target = 0
        user_production = 0
        user_billable = 0

        first = True
        for row in rows:

            created = row["date_time"].strftime("%d/%m/%Y %H:%M")

            html += f"""
            <tr>
                <td>{user if first else ''}</td>
                <td>{created}</td>
                <td>{row['task_name']}</td>
                <td style="text-align:right;">{float(row['tenure_target']):.2f}</td>
                <td style="text-align:right;">{float(row['production']):.2f}</td>
                <td style="text-align:right;">{float(row['billable_hours']):.2f}</td>
            </tr>
            """

            first = False

            user_target += float(row["tenure_target"])
            user_production += float(row["production"])
            user_billable += float(row["billable_hours"])

        # User Total Row
        html += f"""
        <tr style="background-color:#ddebf7; font-weight:bold;">
            <td>{user} Total</td>
            <td></td>
            <td></td>
            <td style="text-align:right;">{user_target:.2f}</td>
            <td style="text-align:right;">{user_production:.2f}</td>
            <td style="text-align:right;">{user_billable:.2f}</td>
        </tr>
        """

        grand_target += user_target
        grand_production += user_production
        grand_billable += user_billable

    # Grand Total
    html += f"""
    <tr style="background-color:#bdd7ee; font-weight:bold;">
        <td>Grand Total</td>
        <td></td>
        <td></td>
        <td style="text-align:right;">{grand_target:.2f}</td>
        <td style="text-align:right;">{grand_production:.2f}</td>
        <td style="text-align:right;">{grand_billable:.2f}</td>
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
        report_date, all_users, tracker_data = fetch_data()

        html_body = generate_html(report_date, all_users, tracker_data)

        subject = f"{report_date.strftime('%d %B %Y')} Production Report"
        send_email(subject, html_body)

        print("[SUCCESS] Full day production report sent")

    except Exception as e:
        print("[ERROR]", str(e))