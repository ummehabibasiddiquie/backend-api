import smtplib
import os
from dotenv import load_dotenv

load_dotenv()

try:
    server = smtplib.SMTP(os.getenv("SMTP_HOST"), int(os.getenv("SMTP_PORT")))
    server.starttls()
    server.login(os.getenv("SMTP_USER"), os.getenv("SMTP_PASS"))
    print("✅ LOGIN SUCCESS")
    server.quit()
except Exception as e:
    print("❌ ERROR:", e)