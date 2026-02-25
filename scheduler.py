import requests
import os
from apscheduler.schedulers.background import BackgroundScheduler

def assign_daily_hours_job():
    """
    Job to call the /qc/assign-daily-hours endpoint.
    """
    try:
        # Use an environment variable for the base URL, with a default for local development
        base_url = os.getenv("API_BASE_URL", "http://127.0.0.1:5000")
        url = f"{base_url}/qc/assign-daily-hours"
        response = requests.post(url)
        if response.status_code == 200:
            print(f"Successfully triggered daily hour assignment: {response.json().get('message')}")
        else:
            print(f"Failed to trigger daily hour assignment. Status: {response.status_code}, Response: {response.text}")
    except Exception as e:
        print(f"An error occurred during the scheduled job: {e}"
              
              )

def start_scheduler():
    """
    Initializes and starts the scheduler.
    """
    scheduler = BackgroundScheduler(daemon=True)
    # Schedule the job to run every day at 8:00 AM
    scheduler.add_job(assign_daily_hours_job, 'cron', hour=8, minute=0)
    scheduler.start()
    print("Scheduler started. Daily hours assignment job is scheduled for 8:00 AM.")
