"""Тесты ядра не требуют БД; тесты API идут на SQLite с фиксированным SECRET_KEY
(см. RUNNING_TESTS в config/settings.py), поэтому локальный .env на них не влияет.
TEST_DATABASE_URL переключает тестовую БД, например на PostgreSQL."""
