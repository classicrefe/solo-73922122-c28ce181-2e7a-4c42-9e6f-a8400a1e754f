#!/bin/sh
set -e

mkdir -p "$(dirname "$WATERLEVEL_DB")"

# 建表（幂等），随后启动 gunicorn
python -c "
from app import create_app
from app.db import get_db, init_db
app = create_app()
with app.app_context():
    init_db(get_db())
"

exec gunicorn --bind 0.0.0.0:8000 --workers 2 --threads 4 --timeout 60 wsgi:app
