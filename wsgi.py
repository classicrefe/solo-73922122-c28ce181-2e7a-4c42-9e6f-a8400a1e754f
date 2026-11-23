"""gunicorn 入口。"""
from app import create_app

app = create_app()
