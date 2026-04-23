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
    # "mohsin.pathan@transformsolution.net",
    # "dharmesh.jotania@transformsolution.net",
    # "venkateshwaran.iyer@transformsolution.net",
    # "yahya.irani@transformsolution.net",
    # "amit.mandviwala@transformsolution.net",
    # "sriman.narayan@transformsolution.net",
    # "shirin.gafoor@transformsolution.net",
    # "avinash.dwivedi@transformsolution.net",
    # "jimil.kinariwala@transformsolution.net",
    # "manas.pradhan@transformsolution.net"
]

CC_RECIPIENTS = [
    # "ashfaq@transformsolution.com",
    # "seema@transformsolution.com"
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
        # report_date = datetime.strptime("2026-04-20", "%Y-%m-%d").date()
        
        report_month = report_date.strftime("%b%Y").upper()

        logging.info(f"Fetching data for report date {report_date}")

        # -------------------------
        # USERS
        # -------------------------

        # Calculate month start and end for deactivated_at logic
        month_start = report_date.replace(day=1)
        month_end = (month_start.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(seconds=1)
        
        cursor.execute(
            """
            SELECT u.user_id, u.user_name, t.team_name,
                umt.user_monthly_tracker_id,
                COALESCE(umt.monthly_target,0) AS monthly_target,
                COALESCE(umt.extra_assigned_hours,0) AS extra_assigned_hours,
                CAST(umt.working_days AS DECIMAL(10,2)) AS working_days,
                u.is_active,
                u.deactivated_at,
                CASE 
                    WHEN u.is_active = 1 THEN 'Active'
                    WHEN u.is_active = 0 AND u.deactivated_at IS NOT NULL THEN 'Exited'
                    ELSE 'Inactive'
                END AS exit_status
            FROM tfs_user u
            JOIN user_role r ON u.role_id = r.role_id
            LEFT JOIN team t ON u.team_id = t.team_id
            LEFT JOIN user_monthly_tracker umt
                ON umt.user_id = u.user_id
                AND umt.is_active=1
                AND umt.month_year=%s
            WHERE u.is_delete = 1
            AND r.role_name='Agent'
            AND t.team_name IN ('A','B')
            AND (
                u.is_active = 1
                OR (
                    u.is_active = 0
                    AND u.deactivated_at IS NOT NULL
                    AND u.deactivated_at BETWEEN %s AND %s
                )
            )
            ORDER BY
                t.team_name,
                CASE 
                    WHEN u.user_name = t.team_name THEN 0
                    ELSE 1
                END,
                u.user_name
            """,
            (report_month, month_start, month_end),
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
        
        # -------------------------
        # QC SCORES (REPORT DATE)
        # -------------------------

        qc_map = {}
        
        # Get QC scores for report date
        cursor.execute(
            f"""
            SELECT 
                dwc.user_id,
                qr.qc_score,
                %s AS qc_date
            FROM (
                SELECT DISTINCT user_id
                FROM tfs_user
                WHERE user_id IN ({in_ph})
            ) dwc
            LEFT JOIN (
                SELECT
                    agent_id,
                    ROUND(AVG(qc_score), 2) AS qc_score
                FROM qc_records
                WHERE DATE(date_of_file_submission) = %s
                AND agent_id IN ({in_ph})
                GROUP BY agent_id
            ) qr
                ON qr.agent_id = dwc.user_id
            """,
            [report_date] + user_ids + [report_date] + user_ids,
        )

        qc_map = {r["user_id"]: r for r in cursor.fetchall()}

        # Also get average QC scores up to report date
        avg_qc_map = {}

        cursor.execute(
            f"""
                SELECT 
                    dwc.user_id,
                    AVG(qr.qc_score) AS avg_qc
                FROM (
                    SELECT DISTINCT user_id
                    FROM tfs_user
                    WHERE user_id IN ({in_ph})
                ) dwc
                LEFT JOIN (
                    SELECT
                        agent_id,
                        qc_score
                    FROM qc_records
                    WHERE qc_score IS NOT NULL
                    AND DATE(date_of_file_submission) BETWEEN %s AND %s
                    AND agent_id IN ({in_ph})
                ) qr
                    ON qr.agent_id = dwc.user_id
                WHERE qr.qc_score IS NOT NULL
                GROUP BY dwc.user_id
            """,
            user_ids + [month_start, report_date] + user_ids,
        )

        avg_qc_map = {
            r["user_id"]: float(r["avg_qc"] or 0)
            for r in cursor.fetchall()
        }
                
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
            SELECT 
                twt.user_id,
                COUNT(DISTINCT DATE(twt.date_time)) AS days_worked
            FROM task_work_tracker twt
            WHERE DATE(twt.date_time) BETWEEN %s AND %s
            AND twt.user_id IN ({in_ph})
            AND twt.is_active=1
            GROUP BY twt.user_id
            """,
            [month_start, report_date] + user_ids,
        )

        days_worked_map = {
            r["user_id"]: float(r["days_worked"]) for r in cursor.fetchall()
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

            qc_date = qc_data.get("qc_date")
            if qc_date and isinstance(qc_date, datetime):
                qc_date = qc_date.strftime("%Y-%m-%d")

            monthly_target = float(u["monthly_target"])
            extra = float(u["extra_assigned_hours"])
            # Match tracker API: COALESCE(CAST(umt.working_days AS SIGNED), 0)
            working_days = float(u["working_days"]) if u["working_days"] is not None else 0

            monthly_goal = monthly_target + extra
            pending = max(0, monthly_goal - mtd)

            days_worked = days_worked_map.get(uid, 0)
            # Match tracker API: GREATEST(working_days - worked_days, 0)
            remaining_days = max(0, working_days - days_worked)

            daily_required = None
            if u.get("user_monthly_tracker_id") is not None and remaining_days > 0:
                daily_required = pending / remaining_days

            avg_qc = avg_qc_map.get(uid)

            assigned = 0 if (is_team_agent(u) or worked == 0) else assigned_map.get(uid, 0)

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

        # DEBUG: Show all users and their calculations
        print(f"DEBUG - All users calculations:")
        for u in users:
            uid = u["user_id"]
            days_worked = days_worked_map.get(uid, 0)
            working_days = float(u["working_days"]) if u["working_days"] is not None else 0
            remaining_days = max(0, working_days - days_worked)
            mtd = u.get("mtd_hours", 0)
            pending = u.get("pending_goal", 0)
            
            print(f"  {u['user_name']} (ID: {uid}):")
            print(f"    working_days: {working_days}, days_worked: {days_worked}, remaining_days: {remaining_days}")
            print(f"    monthly_target: {u['monthly_target']}, mtd: {mtd:.2f}, pending: {pending:.2f}")
            print(f"    user_monthly_tracker_id: {u.get('user_monthly_tracker_id')}")
            print(f"    daily_required_hours: {u.get('daily_required_hours')}")

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
    
    # QC date is now the report date
    qc_date_str = report_date.strftime("%d %b")
    
    html = f"""
    <p><b>Delivered billable hours on {day_str} {month_year}</b></p>

    <table border="1" cellpadding="3" cellspacing="0"
    style="border-collapse:collapse;font-family:Arial;font-size:11px;width:auto">

    <tr style="background:#FFD966;font-weight:bold">
        <th rowspan="2">Team Member</th>
        <th rowspan="2">Exit Status</th>
        <th colspan="4">Daily Report</th>
        <th colspan="4">MTD Report</th>
    </tr>

    <tr style="background:#FFE699;font-weight:bold">
        <th >Assigned <br>{assigned_date}</th>
        <th>Worked <br>{worked_date}</th>
        <th>Quality <br>{qc_date_str}</th>
        <th>Daily Required <br> Hours</th>
        <th>Delivered-MTD <br> till {worked_date}</th>
        <th>Monthly Goal</th>
        <th>Pending Goal</th>
        <th>Avg QC till <br>{qc_date_str}</th>
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
            <td align="center">{u.get('exit_status', '')}</td>
            <td align="right">{"" if is_team_agent(u) else f"{assigned:.2f}"}</td>
            <td align="right">{worked:.2f}</td>
            <td align="right">{f"{u['qc_score']:.2f}" if u.get('qc_score') is not None else ""}</td>
            <td align="right">{f"{required:.2f}" if required is not None else ""}</td>
            <td align="right">{mtd:.2f}</td>
            <td align="right">{goal:.2f}</td>
            <td align="right">{pending:.2f}</td>
            <td align="right">{f"{u['avg_qc_score']:.2f}" if u.get('avg_qc_score') is not None else ""}</td>
            </tr>
            """

            if not is_team_agent(u):
                team_assigned += assigned
            team_worked += worked
            team_required += required if required is not None else 0
            team_mtd += mtd
            team_goal += goal
            team_pending += pending
            
            if not is_team_agent(u):
                grand_assigned += assigned
            grand_worked += worked
            grand_required += required if required is not None else 0
            grand_mtd += mtd
            grand_goal += goal
            grand_pending += pending

        html += f"""
        <tr style="font-weight:bold;background:#C9DAF8">
        <td>Team {team} Total</td>
        <td></td>
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
        <td></td>
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

    # FOR LOCALHOST TESTING - Print instead of sending email
    print("=" * 80)
    print(f"REPORT DATE: {report_date.strftime('%dth %B %Y')}")
    print(f"SUBJECT: Delivered billable hours on {report_date.strftime('%dth %B %Y')}")
    print(f"TO: {', '.join(RECIPIENTS)}")
    print(f"CC: {', '.join(CC_RECIPIENTS)}")
    print("=" * 80)
    print(html_body)
    print("=" * 80)
    logging.info("Report printed to console (localhost mode)")

    # Uncomment below to actually send email
    # host = os.getenv("SMTP_HOST")
    # port = int(os.getenv("SMTP_PORT", 587))
    # user = os.getenv("SMTP_USER")
    # password = os.getenv("SMTP_PASS")

    # msg = MIMEMultipart("alternative")

    # msg["From"] = user
    # msg["To"] = ", ".join(RECIPIENTS)
    # msg["Cc"] = ", ".join(CC_RECIPIENTS) 
    # msg["Subject"] = f"Delivered billable hours on {report_date.strftime('%dth %B %Y')}"

    # msg.attach(MIMEText(html_body, "html"))

    # all_recipients = RECIPIENTS + CC_RECIPIENTS
    
    # with smtplib.SMTP(host, port) as server:
    #     server.starttls()
    #     server.login(user, password)
    #     server.sendmail(user, all_recipients, msg.as_string())


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