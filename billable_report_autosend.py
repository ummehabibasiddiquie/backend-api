# daily_tracker_report_cron_v3.py

from dotenv import load_dotenv
from pathlib import Path
import os
import mysql.connector
import smtplib
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import logging
from collections import defaultdict


# -------------------------------
# CONFIG
# -------------------------------
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


LOG_FILE = Path(__file__).resolve().parent / "daily_tracker_report.log"

logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)

def is_team_agent(u):
    return (u["user_name"] or "").strip().lower() == (u["team_name"] or "").strip().lower()

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
        # report_date = datetime.strptime("2026-03-17", "%Y-%m-%d").date()
        
        report_month = report_date.strftime("%b%Y").upper()

        logging.info(f"Fetching data for report date {report_date}")

        # -------------------------
        # USERS
        # -------------------------

        cursor.execute(
            """
            SELECT u.user_id, u.user_name, t.team_name,
                COALESCE(umt.monthly_target,0) AS monthly_target,
                COALESCE(umt.extra_assigned_hours,0) AS extra_assigned_hours,
                COALESCE(umt.working_days,0) AS working_days
            FROM tfs_user u
            JOIN user_role r ON u.role_id = r.role_id
            LEFT JOIN team t ON u.team_id = t.team_id
            LEFT JOIN user_monthly_tracker umt
                ON umt.user_id = u.user_id
                AND umt.is_active=1
                AND umt.month_year=%s
            WHERE u.is_delete != 0
            AND u.is_active=1
            AND u.is_delete != 0
            AND r.role_name='Agent'
            AND t.team_name IN ('A','B')
            ORDER BY
                t.team_name,
                CASE 
                    WHEN u.user_name = t.team_name THEN 0
                    ELSE 1
                END,
                u.user_name
            """,
            (report_month,),
        )

        users = cursor.fetchall()

        if not users:
            return report_date, []

        user_ids = [u["user_id"] for u in users]
        in_ph = ",".join(["%s"] * len(user_ids))

        month_start = report_date.replace(day=1)

        # -------------------------
        # DAILY HOURS
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

        daily_map = {
            r["user_id"]: float(r["worked_hours"] or 0) for r in cursor.fetchall()
        }
        
        # Remove absent agents but keep team agents
        active_user_ids = set(daily_map.keys())

        users = [
            u for u in users
            if (u["user_id"] in active_user_ids or is_team_agent(u))
        ]

        if not users:
            return report_date, []

        # rebuild user_ids AFTER filtering
        user_ids = [u["user_id"] for u in users]
        in_ph = ",".join(["%s"] * len(user_ids))

        # -------------------------
        # MTD HOURS
        # -------------------------

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

        mtd_map = {r["user_id"]: float(r["mtd_hours"] or 0) for r in cursor.fetchall()}

        # -------------------------
        # DAYS WORKED
        # -------------------------

        cursor.execute(
            f"""
            SELECT user_id,
            COUNT(DISTINCT DATE(date_time)) AS days_worked
            FROM task_work_tracker
            WHERE DATE(date_time) BETWEEN %s AND %s
            AND user_id IN ({in_ph})
            AND is_active=1
            GROUP BY user_id
            """,
            [month_start, report_date] + user_ids,
        )

        days_worked_map = {
            r["user_id"]: int(r["days_worked"]) for r in cursor.fetchall()
        }

        # -------------------------
        # LATEST QC DATE (GLOBAL)
        # -------------------------

        cursor.execute(
            """
            SELECT MAX(DATE(date)) AS latest_qc_date
            FROM temp_qc
            WHERE qc_score IS NOT NULL AND DATE(date) < %s
            """,
            (report_date,)
        )
        # print(report_date)

        row = cursor.fetchone()
        latest_qc_date = row["latest_qc_date"]
        # print(f"Latest QC date: {latest_qc_date}")

        qc_map = {}

        if latest_qc_date:

            cursor.execute(
                f"""
                SELECT user_id, qc_score, DATE(date) AS qc_date
                FROM temp_qc
                WHERE DATE(date) = %s
                AND user_id IN ({in_ph})
                """,
                [latest_qc_date] + user_ids,
            )

            qc_map = {r["user_id"]: r for r in cursor.fetchall()}
            # print(f"QC Map: {qc_map}")
        
        # -------------------------
        # AVG QC SCORE (MONTH TILL LATEST QC DATE)
        # -------------------------

        avg_qc_map = {}

        if latest_qc_date:

            cursor.execute(
                f"""
                    SELECT user_id, AVG(qc_score) AS avg_qc
                    FROM temp_qc
                    WHERE qc_score IS NOT NULL
                    AND DATE(date) BETWEEN %s AND %s
                    AND user_id IN ({in_ph})
                    GROUP BY user_id
                """,
                [month_start, latest_qc_date] + user_ids,
            )

            avg_qc_map = {
                r["user_id"]: float(r["avg_qc"] or 0)
                for r in cursor.fetchall()
            }
                
        # -------------------------
        # ASSIGNED HOURS (REPORT DATE)
        # -------------------------

        cursor.execute(
            f"""
            SELECT user_id, assigned_hours
            FROM temp_qc
            WHERE DATE(date) = %s
            AND user_id IN ({in_ph})
            """,
            [report_date] + user_ids,
        )

        assigned_map = {
            r["user_id"]: float(r["assigned_hours"] or 0)
            for r in cursor.fetchall()
        }

        # -------------------------
        # CALCULATIONS
        # -------------------------

        for u in users:

            uid = u["user_id"]

            worked = daily_map.get(uid, 0)
            mtd = mtd_map.get(uid, 0)

            qc_data = qc_map.get(uid, {})

            # print(qc_data.get("qc_date"))
            qc_date = qc_data.get("qc_date")
            if qc_date and isinstance(qc_date, datetime):
                qc_date = qc_date.strftime("%Y-%m-%d")

            avg_qc = avg_qc_map.get(uid)
            # assigned = assigned_map.get(uid, 0)
            assigned = 0 if is_team_agent(u) else assigned_map.get(uid, 0)

            monthly_target = float(u["monthly_target"])
            extra = float(u["extra_assigned_hours"])
            working_days = float(u["working_days"])

            monthly_goal = monthly_target + extra
            pending = max(0, monthly_goal - mtd)

            days_worked = days_worked_map.get(uid, 0)
            remaining_days = max(0, working_days - days_worked)

            daily_required = pending / remaining_days if remaining_days else 0

            u.update(
                {
                    "daily_worked_hours": worked,
                    "mtd_hours": mtd,
                    "assigned_hours": assigned,
                    "qc_score": qc_data.get("qc_score"),
                    "qc_date": qc_date,
                    "avg_qc_score": avg_qc,
                    "monthly_goal": monthly_goal,
                    "pending_goal": pending,
                    "daily_required_hours": daily_required,
                }
            )

        return report_date, users

    finally:
        cursor.close()
        conn.close()


# -------------------------------
# HTML GENERATION
# -------------------------------
def generate_html(report_date, data_rows):

    day_str = report_date.strftime("%d %b")
    month_year = report_date.strftime("%B %Y")
    worked_date = report_date.strftime("%d %b")
    assigned_date = worked_date
    
    # Find latest QC date
    qc_dates = []
    for u in data_rows:
        qc_date = u.get("qc_date")
        if qc_date:
            if isinstance(qc_date, str):
                qc_date = datetime.strptime(qc_date, "%Y-%m-%d")
            qc_dates.append(qc_date)

    latest_qc_date = max(qc_dates) if qc_dates else None
    latest_qc_date_str = latest_qc_date.strftime("%d %b") if latest_qc_date else ""

    html = f"""
    <p><b>Delivered billable hours on {day_str} {month_year}</b></p>

    <table border="1" cellpadding="3" cellspacing="0"
    style="border-collapse:collapse;font-family:Arial;font-size:11px;width:auto">

    <tr style="background:#FFD966;font-weight:bold">
        <th rowspan="2">Team Member</th>
        <th colspan="4">Daily Report</th>
        <th colspan="4">MTD Report</th>
    </tr>

    <tr style="background:#FFE699;font-weight:bold">
        <th >Assigned <br>{assigned_date}</th>
        <th>Worked <br>{worked_date}</th>
        <th>Quality <br>{latest_qc_date_str}</th>
        <th>Daily Required <br> Hours</th>
        <th>Delivered-MTD <br> till {worked_date}</th>
        <th>Monthly Goal</th>
        <th>Pending Goal</th>
        <th>Avg QC till <br>{latest_qc_date_str}</th>
    </tr>
    """

    teams = defaultdict(list)

    for u in data_rows:
        team = u.get("team_name") or "No Team"
        teams[team].append(u)
        
    # GRAND TOTAL VARIABLES
    grand_assigned = 0
    grand_worked = 0
    grand_required = 0
    grand_mtd = 0
    grand_goal = 0
    grand_pending = 0

    for team, members in teams.items():

        # ensure team agent comes first
        members = sorted(
            members,
            key=lambda x: (
                0 if x["user_name"].strip().lower() == team.strip().lower() else 1,
                x["user_name"]
            )
        )
        
        # Initialize totals BEFORE loop
        team_assigned = 0
        team_worked = 0
        team_required = 0
        team_mtd = 0
        team_goal = 0
        team_pending = 0

        for u in members:

            assigned = u["assigned_hours"]
            worked = u["daily_worked_hours"]
            required = u["daily_required_hours"]
            mtd = u["mtd_hours"]
            goal = u["monthly_goal"]
            pending = u["pending_goal"]

            html += f"""
            <tr>
            <td>{u['user_name']}</td>
            <td align="right">{"" if is_team_agent(u) else f"{assigned:.2f}"}</td>
            <td align="right">{worked:.2f}</td>
            <td align="right">{f"{u['qc_score']:.2f}" if u.get('qc_score') is not None else ""}</td>
            <td align="right">{required:.2f}</td>
            <td align="right">{mtd:.2f}</td>
            <td align="right">{goal:.2f}</td>
            <td align="right">{pending:.2f}</td>
            <td align="right">{f"{u['avg_qc_score']:.2f}" if u.get('avg_qc_score') is not None else ""}</td>
            </tr>
            """

            if not is_team_agent(u):
                team_assigned += assigned
            team_worked += worked
            team_required += required
            team_mtd += mtd
            team_goal += goal
            team_pending += pending
            
            if not is_team_agent(u):
                grand_assigned += assigned
            grand_worked += worked
            grand_required += required
            grand_mtd += mtd
            grand_goal += goal
            grand_pending += pending

        html += f"""
        <tr style="font-weight:bold;background:#C9DAF8">
        <td>Team {team} Total</td>
        <td align="right">{team_assigned:.2f}</td>
        <td align="right">{team_worked:.2f}</td>
        <td></td>
        <td align="right">{team_required:.2f}</td>
        <td align="right">{team_mtd:.2f}</td>
        <td align="right">{team_goal:.2f}</td>
        <td align="right">{team_pending:.2f}</td>
        <td></td>
        </tr>
        """

    html += f"""
        <tr style="font-weight:bold;background:#A4C2F4">
        <td>Grand Total</td>
        <td align="right">{grand_assigned:.2f}</td>
        <td align="right">{grand_worked:.2f}</td>
        <td></td>
        <td align="right">{grand_required:.2f}</td>
        <td align="right">{grand_mtd:.2f}</td>
        <td align="right">{grand_goal:.2f}</td>
        <td align="right">{grand_pending:.2f}</td>
        <td></td>
        </tr>
        """

    html += "</table>"

    return html


# -------------------------------
# SEND EMAIL
# -------------------------------
def send_email(report_date, html_body):

    host = os.getenv("SMTP_HOST")
    port = int(os.getenv("SMTP_PORT", 587))
    user = os.getenv("SMTP_USER")
    password = os.getenv("SMTP_PASS")

    msg = MIMEMultipart("alternative")

    msg["From"] = user
    msg["To"] = ", ".join(RECIPIENTS)
    msg["Cc"] = ", ".join(CC_RECIPIENTS) 
    msg["Subject"] = f"Delivered billable hours on {report_date.strftime('%dth %B %Y')}"

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

        report_date, data = fetch_data()

        if not data:
            logging.info("No data found")
            exit()

        html = generate_html(report_date, data)

        send_email(report_date, html)

        logging.info("Report sent successfully")

    except Exception as e:
        print("Error:", str(e))

        logging.exception(f"Report failed: {str(e)}")