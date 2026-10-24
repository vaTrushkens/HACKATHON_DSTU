"""Настройки Django. Секреты и подключение к БД — только через переменные окружения (.env)."""
from __future__ import annotations

import os
from datetime import timedelta
from pathlib import Path

import dj_database_url
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
PROJECT_DIR = BASE_DIR.parent
load_dotenv(PROJECT_DIR / ".env")
load_dotenv(BASE_DIR / ".env")

SECRET_KEY = os.getenv("DJANGO_SECRET_KEY", "dev-only-insecure-key-change-me")
DEBUG = os.getenv("DJANGO_DEBUG", "1") == "1"
ALLOWED_HOSTS = os.getenv("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1,backend").split(",")

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "corsheaders",
    "django_filters",
    "apps.accounts",
    "apps.companies",
    "apps.operations",
    "apps.planning",
    "apps.forecasting",
    "apps.assistant",
    "apps.tgbot",
]

MIDDLEWARE = [
    "corsheaders.middleware.CorsMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"
TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {"context_processors": ["django.template.context_processors.request", "django.contrib.auth.context_processors.auth", "django.contrib.messages.context_processors.messages"]},
    }
]
WSGI_APPLICATION = "config.wsgi.application"

# PostgreSQL — основная БД. DATABASE_URL=postgres://user:pass@host:5432/db
# Для локальных тестов без Postgres можно указать DATABASE_URL=sqlite:///db.sqlite3
DATABASES = {
    "default": dj_database_url.config(
        default=os.getenv("DATABASE_URL", "postgres://cashflow:cashflow@localhost:5432/cashflow"),
        conn_max_age=600,
    )
}

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator", "OPTIONS": {"min_length": 8}},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
]

LANGUAGE_CODE = "ru-ru"
TIME_ZONE = "Europe/Moscow"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": ("rest_framework_simplejwt.authentication.JWTAuthentication",),
    "DEFAULT_PERMISSION_CLASSES": ("rest_framework.permissions.IsAuthenticated",),
    "DEFAULT_FILTER_BACKENDS": ("django_filters.rest_framework.DjangoFilterBackend",),
    "DEFAULT_PAGINATION_CLASS": "core.pagination.DefaultPagination",
    "PAGE_SIZE": 50,
    "DEFAULT_RENDERER_CLASSES": ("rest_framework.renderers.JSONRenderer",) + (("rest_framework.renderers.BrowsableAPIRenderer",) if DEBUG else ()),
}

SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(hours=12),
    "REFRESH_TOKEN_LIFETIME": timedelta(days=7),
    "AUTH_HEADER_TYPES": ("Bearer",),
}

CORS_ALLOWED_ORIGINS = os.getenv("CORS_ALLOWED_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173").split(",")

# ---- настройки сервиса ----
MODELS_DIR = Path(os.getenv("MODELS_DIR", BASE_DIR / "models_store"))
MODELS_DIR.mkdir(parents=True, exist_ok=True)
FORECAST_DEFAULT_HORIZON = int(os.getenv("FORECAST_DEFAULT_HORIZON", "60"))
FORECAST_N_PATHS = int(os.getenv("FORECAST_N_PATHS", "1000"))
DATASET_DIR = os.getenv("DATASET_DIR", str(PROJECT_DIR.parent / "cashflow_dataset_participants"))

# ---- LLM-ассистент: локальная модель через Ollama (чат на сайте и Telegram-бот) ----
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "ollama")
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "http://localhost:11434")
LLM_MODEL = os.getenv("LLM_MODEL", "gemma4:e4b")
LLM_TIMEOUT = int(os.getenv("LLM_TIMEOUT", "180"))  # CPU-инференс: ответ с инструментами может занять десятки секунд
LLM_NUM_CTX = int(os.getenv("LLM_NUM_CTX", "8192"))
LLM_MODE = os.getenv("LLM_MODE", "router")  # router — выбор инструмента структурированным JSON (быстро, для компактных моделей); native — нативный function calling (модели от ~8B)
LLM_THINK = os.getenv("LLM_THINK", "1") == "1"  # размышления в режиме native: надёжнее вызывает инструменты, но ~2× дольше

# ---- Telegram-бот: уведомления о рисках/платежах и ответы ассистента ----
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_NOTIFY_INTERVAL_MIN = int(os.getenv("TELEGRAM_NOTIFY_INTERVAL_MIN", "30"))
TELEGRAM_REMIND_DAYS = int(os.getenv("TELEGRAM_REMIND_DAYS", "3"))

# ---- логирование: чувствительные данные (ИНН, суммы, контрагенты, назначения) маскируются ----
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "filters": {"mask_sensitive": {"()": "core.logging_filters.SensitiveDataFilter"}},
    "formatters": {"std": {"format": "%(asctime)s %(levelname)s %(name)s: %(message)s"}},
    "handlers": {"console": {"class": "logging.StreamHandler", "formatter": "std", "filters": ["mask_sensitive"]}},
    "root": {"handlers": ["console"], "level": os.getenv("LOG_LEVEL", "INFO")},
    "loggers": {"django.db.backends": {"level": "WARNING"}, "httpx": {"level": "WARNING"}},  # SQL с данными операций и URL Telegram API с токеном в логи не пишем
}
