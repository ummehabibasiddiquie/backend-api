# send_tracker_report_standalone.py
from dotenv import load_dotenv
from pathlib import Path
import os
import mysql.connector
import smtplib
from datetime import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

# Load environment variables from .env in the same folder
load_dotenv(dotenv_path=Path(__file__).resolve().parent / ".env")

# -------------------------------
# CONFIG
# -------------------------------
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
# DATABASE CONNECTION
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
# QUERY DATA
# -------------------------------
def get_daily_tracker_report_till_now():
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    try:        
        now = datetime.now()
        today_start = datetime.combine(now.date(), datetime.strptime("09:00:00", "%H:%M:%S").time())
        
        start_str = f"{today_start.month}/{today_start.day}/{today_start.year} {today_start.strftime('%H:%M')}"
        end_str = f"{now.month}/{now.day}/{now.year} {now.strftime('%H:%M')}"
        
        # today_start = "2026-03-02 09:00:00"
        # now = "2026-03-02 10:53:39"
        
        print(now, today_start)

        query = """
            SELECT 
                t.date_time,
                u.user_name,
                p.task_name,
                t.production,
                t.tenure_target,
                t.billable_hours
            FROM task_work_tracker t
            JOIN tfs_user u ON t.user_id = u.user_id
            JOIN task p ON t.task_id = p.task_id
            WHERE t.date_time >= %s
              AND t.date_time <= %s
              AND t.is_active != 0
              AND u.is_delete != 0
            ORDER BY u.user_name, t.date_time;
        """
        cursor.execute(query, (today_start, now))
        
        print(query)
        return cursor.fetchall(), start_str, end_str
    finally:
        cursor.close()
        conn.close()

# -------------------------------
# GENERATE HTML
# -------------------------------
from collections import defaultdict

def generate_html_report(data, start_str, end_str):

    html = f"""
    <p style="font-family: Arial; font-size: 13px;">
        Dear All,<br><br>
        Tracker report from <b>{start_str}</b> till <b>{end_str}</b>.
    </p>
    """

    if not data:
        html += "<p style='font-family:Arial; font-size:13px;'>No records found.</p>"
        return html

    # Group by agent
    grouped = defaultdict(list)
    for row in data:
        grouped[row["user_name"]].append(row)

    html += """
    <table style="
        border-collapse: collapse;
        width: 80%;
        font-family: Arial, sans-serif;
        font-size: 12px;
    ">
        <tr style="background-color: #2f6f8f; color: white;">
            <th style="padding:4px; border:1px solid #ccc;">Created</th>
            <th style="padding:4px; border:1px solid #ccc;">Name</th>
            <th style="padding:4px; border:1px solid #ccc;">Task Name</th>
            <th style="padding:4px; border:1px solid #ccc;">Production</th>
            <th style="padding:4px; border:1px solid #ccc;">Target</th>
            <th style="padding:4px; border:1px solid #ccc;">Billable Hours</th>
        </tr>
    """

    row_index = 0

    for user, rows in grouped.items():

        total_prod = 0
        total_target = 0
        total_bill = 0

        for row in rows:

            dt = row['date_time']
            formatted_date = dt.strftime("%d/%m/%Y %H:%M")

            row_color = "#e6f2f8" if row_index % 2 == 0 else "#ffffff"

            html += f"""
            <tr style="background-color: {row_color};">
                <td style="padding:4px; border:1px solid #ccc;">{formatted_date}</td>
                <td style="padding:4px; border:1px solid #ccc;">{row['user_name']}</td>
                <td style="padding:4px; border:1px solid #ccc;">{row['task_name']}</td>
                <td style="padding:4px; border:1px solid #ccc; text-align:center;">
                    {float(row['production']):.2f}
                </td>
                <td style="padding:4px; border:1px solid #ccc; text-align:center;">
                    {float(row['tenure_target']):.2f}
                </td>
                <td style="padding:4px; border:1px solid #ccc; text-align:center;">
                    {float(row['billable_hours']):.2f}
                </td>
            </tr>
            """

            total_prod += float(row['production'])
            total_target += float(row['tenure_target'])
            total_bill += float(row['billable_hours'])

            row_index += 1

        # Agent Total Row (same theme, slightly darker)
        html += f"""
        <tr style="background-color:#cfe7f3; font-weight:bold;">
            <td colspan="3" style="padding:4px; border:1px solid #ccc;">
                {user} Total
            </td>
            <td style="padding:4px; border:1px solid #ccc; text-align:center;">
                {total_prod:.2f}
            </td>
            <td style="padding:4px; border:1px solid #ccc; text-align:center;">
                {total_target:.2f}
            </td>
            <td style="padding:4px; border:1px solid #ccc; text-align:center;">
                {total_bill:.2f}
            </td>
        </tr>
        """

    html += "</table>"

    return html

# -------------------------------
# SEND EMAIL
# -------------------------------
def send_email(to_emails, subject, html_body):
    host = os.getenv("SMTP_HOST")
    port = int(os.getenv("SMTP_PORT", "587"))
    user = os.getenv("SMTP_USER")
    password = os.getenv("SMTP_PASS")
    from_name = os.getenv("SMTP_FROM_NAME", "No-Reply")
    print(host, port, user, password)
    
    print("EMAIL:", os.getenv("SMTP_USER"))
    print("PASSWORD LENGTH:", len(os.getenv("SMTP_PASS") or ""))

    if isinstance(to_emails, str):
        recipients = [to_emails]
    else:
        recipients = to_emails

    msg = MIMEMultipart("alternative")
    msg["From"] = f"{from_name} <{user}>"
    msg["To"] = ", ".join(recipients)
    msg["Cc"] = ", ".join(CC_RECIPIENTS) 
    msg["Subject"] = subject
    msg.attach(MIMEText(html_body, "html"))
    
    all_recipients = RECIPIENTS + CC_RECIPIENTS

    print(f"Connecting to SMTP: {host}:{port}")
    with smtplib.SMTP(host, port) as server:
        server.ehlo()
        server.starttls()
        server.ehlo()
        server.login(user, password)
        server.sendmail(user, all_recipients, msg.as_string())
    print("✅ Email sent successfully")

# -------------------------------
# MAIN
# -------------------------------
if __name__ == "__main__":
    try:
        print("[CRON STARTED]", datetime.now())
        data, start_str, end_str = get_daily_tracker_report_till_now()
        html_body = generate_html_report(data, start_str, end_str)
        subject = f"Daily Tracker Report - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        send_email(RECIPIENTS, subject, html_body)
        print(f"[CRON SUCCESS] Sent {len(data)} records")
    except Exception as e:
        print("[CRON ERROR]", str(e))
        