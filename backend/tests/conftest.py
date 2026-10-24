"""Тесты ядра не требуют БД; тесты API используют SQLite, чтобы запускаться без Postgres."""
import os

os.environ.setdefault("DATABASE_URL", "sqlite:///test_db.sqlite3")
os.environ.setdefault("DJANGO_SECRET_KEY", "test")
