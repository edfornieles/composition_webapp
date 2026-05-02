[Unit]
Description=Gunicorn for Django (%APP_SERVICE_NAME%)
After=network.target

[Service]
Type=simple
User=%APP_USER%
Group=www-data
WorkingDirectory=%APP_DIR%
Environment=PYTHONUNBUFFERED=1
EnvironmentFile=%ENV_FILE%
ExecStart=%VENV_PATH%/bin/gunicorn --workers %GUNICORN_WORKERS% --timeout %GUNICORN_TIMEOUT% --bind %GUNICORN_BIND% %DJANGO_WSGI_MODULE%
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target

