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

# -------------------------------
# HELPERS
# -------------------------------
def is_team_agent(u):
    return u["user_name"].strip().lower() == (u["team_name"] or "").strip().lower()

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
        report_month = report_date.strftime("%b%Y").upper()

        logging.info(f"Fetching data for {report_date}")

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
            AND r.role_name='Agent'
            AND t.team_name IN ('A','B')
            ORDER BY t.team_name, u.user_name
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
        # DAILY HOURS (Presence)
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
            r["user_id"]: float(r["worked_hours"] or 0)
            for r in cursor.fetchall()
        }

        # -------------------------
        # FILTER USERS (CORE FIX)
        # -------------------------
        active_user_ids = set(daily_map.keys())

        users = [
            u for u in users
            if (u["user_id"] in active_user_ids or is_team_agent(u))
        ]

        if not users:
            return report_date, []

        # rebuild ids after filtering
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
        # QC DATA
        # -------------------------
        cursor.execute(
            """
            SELECT MAX(DATE(date)) AS latest_qc_date
            FROM temp_qc
            WHERE qc_score IS NOT NULL AND DATE(date) < %s
            """,
            (report_date,)
        )

        latest_qc_date = cursor.fetchone()["latest_qc_date"]
        qc_map = {}
        avg_qc_map = {}

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

            cursor.execute(
                f"""
                SELECT user_id, AVG(qc_score) AS avg_qc
                FROM temp_qc
                WHERE DATE(date) BETWEEN %s AND %s
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
        # ASSIGNED HOURS
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

            if is_team_agent(u):
                assigned = 0
            else:
                assigned = assigned_map.get(uid, 0)

            monthly_goal = float(u["monthly_target"]) + float(u["extra_assigned_hours"])
            pending = max(0, monthly_goal - mtd)

            days_worked = days_worked_map.get(uid, 0)
            remaining_days = max(0, float(u["working_days"]) - days_worked)
            daily_required = pending / remaining_days if remaining_days else 0

            qc_data = qc_map.get(uid, {})

            u.update({
                "daily_worked_hours": worked,
                "mtd_hours": mtd,
                "assigned_hours": assigned,
                "qc_score": qc_data.get("qc_score"),
                "qc_date": qc_data.get("qc_date"),
                "avg_qc_score": avg_qc_map.get(uid),
                "monthly_goal": monthly_goal,
                "pending_goal": pending,
                "daily_required_hours": daily_required,
            })

        return report_date, users

    finally:
        cursor.close()
        conn.close()

# -------------------------------
# HTML GENERATION
# -------------------------------
def generate_html(report_date, data_rows):

    html = """
    <table border="1" cellpadding="3" cellspacing="0"
    style="border-collapse:collapse;font-family:Arial;font-size:11px">
    """

    teams = defaultdict(list)
    for u in data_rows:
        teams[u["team_name"]].append(u)

    grand_assigned = 0

    for team, members in teams.items():

        team_assigned = 0

        for u in members:

            assigned = u["assigned_hours"]

            assigned_display = "" if is_team_agent(u) else f"{assigned:.2f}"

            html += f"""
            <tr>
            <td>{u['user_name']}</td>
            <td>{assigned_display}</td>
            </tr>
            """

            if not is_team_agent(u):
                team_assigned += assigned
                grand_assigned += assigned

        html += f"""
        <tr><td><b>Team {team} Total</b></td><td>{team_assigned:.2f}</td></tr>
        """

    html += f"""
    <tr><td><b>Grand Total</b></td><td>{grand_assigned:.2f}</td></tr>
    """

    html += "</table>"

    return html