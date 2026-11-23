"""水位时长核算 Flask 应用包。"""
import os

from .db import init_app as init_db_app


def create_app(test_config=None):
    from flask import Flask

    app = Flask(__name__, instance_relative_config=False)
    app.config.from_mapping(
        DATABASE=os.environ.get("WATERLEVEL_DB", "data/waterlevel.db"),
        MAX_CONTENT_LENGTH=16 * 1024 * 1024,
        SECRET_KEY=os.environ.get("SECRET_KEY", "dev-secret-change-me"),
    )
    if test_config:
        app.config.update(test_config)

    init_db_app(app)

    from . import web
    app.register_blueprint(web.bp)

    @app.errorhandler(413)
    def too_large(_e):
        from flask import flash, redirect, url_for
        flash("文件超过 16MB 上限，请拆分后导入。", "error")
        return redirect(url_for("web.import_page"))

    return app
