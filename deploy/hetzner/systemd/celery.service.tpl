[Unit]
Description=Celery worker for Django (%APP_SERVICE_NAME%)
After=network.target redis-server.service
Requires=redis-server.service

[Service]
Type=simple
User=%APP_USER%
Group=www-data
WorkingDirectory=%APP_DIR%
Environment=PYTHONUNBUFFERED=1
EnvironmentFile=%ENV_FILE%
ExecStart=%VENV_PATH%/bin/celery -A celery_app worker --loglevel=INFO --concurrency=%CELERY_CONCURRENCY%
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target

