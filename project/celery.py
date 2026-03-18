"""
project/celery.py
──────────────────
Celery application configuration for the Lykhan project.

Celery is the async task queue that decouples the webhook endpoint from
the LLM gate and MT5 execution. The webhook returns 200 immediately;
this worker picks up the task from Redis and runs the full pipeline.

Starting the worker (from the project root with .venv active):
    celery -A project worker --loglevel=info --concurrency=1

The --concurrency=1 flag is REQUIRED while using SQLite as the database.
SQLite uses file-level locking which breaks under concurrent writes from
multiple Celery worker processes. When you switch to PostgreSQL on AWS,
remove --concurrency=1 and set workers to your core count.

Starting the scheduler (for future periodic tasks like drawdown checks):
    celery -A project beat --loglevel=info

Monitoring with Flower (pip install flower):
    celery -A project flower
    Then open http://localhost:5555
"""

import os
from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "project.settings")

app = Celery("lykhan")

# Load config from Django settings, using the CELERY_ namespace prefix.
# Any setting prefixed with CELERY_ in settings.py is automatically applied.
app.config_from_object("django.conf:settings", namespace="CELERY")

# Autodiscover tasks.py modules in all INSTALLED_APPS.
app.autodiscover_tasks()


@app.task(bind=True, ignore_result=True)
def debug_task(self):
    """Utility task for verifying Celery is running. Call with: debug_task.delay()"""
    print(f"Request: {self.request!r}")