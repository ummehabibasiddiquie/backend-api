import os
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart


def send_email(to_email: str, subject: str, html_body: str):
    host = os.getenv("SMTP_HOST")
    port = int(os.getenv("SMTP_PORT", "587"))
    user = os.getenv("SMTP_USER")
    password = os.getenv("SMTP_PASS")
    from_name = os.getenv("SMTP_FROM_NAME", "No-Reply")

    if not all([host, user, password]):
        raise RuntimeError("SMTP configuration missing")

    msg = MIMEMultipart("alternative")
    msg["From"] = f"{from_name} <{user}>"
    msg["To"] = to_email
    msg["Subject"] = subject

    msg.attach(MIMEText(html_body, "html"))

    with smtplib.SMTP(host, port) as server:
        server.ehlo()
        server.starttls()
        server.login(user, password)
        server.sendmail(user, [to_email], msg.as_string())
