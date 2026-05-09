# Procfile for Render deployment
# ============================================================================
# This file tells Render how to start the Flask app using Gunicorn

web: gunicorn app:app --workers 2 --threads 2 --worker-class gthread --timeout 60
