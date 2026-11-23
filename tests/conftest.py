import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import pytest

from app import create_app
from app.db import get_db, init_db


@pytest.fixture()
def app(tmp_path):
    db_path = tmp_path / "test.db"
    app = create_app({
        "DATABASE": str(db_path),
        "TESTING": True,
        "SECRET_KEY": "test",
    })
    with app.app_context():
        init_db(get_db())
    yield app


@pytest.fixture()
def db(app):
    with app.app_context():
        yield get_db()


@pytest.fixture()
def client(app):
    return app.test_client()
