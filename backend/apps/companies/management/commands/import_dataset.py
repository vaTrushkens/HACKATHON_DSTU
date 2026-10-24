"""Импорт датасета кейса: companies.csv + statements/*.csv.

    python manage.py import_dataset --path ../cashflow_dataset_participants [--only C001,C037] [--demo-user demo]
"""
from __future__ import annotations

import logging
from pathlib import Path

import pandas as pd
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand

from apps.accounts.models import CompanyMembership
from apps.companies.models import Company
from apps.operations import services as ops_services
from apps.planning import services as planning_services
from cashflow_core import ingest

log = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Импортирует синтетические выписки кейса в базу"

    def add_arguments(self, parser):
        parser.add_argument("--path", default=settings.DATASET_DIR)
        parser.add_argument("--only", default="", help="Список company_id через запятую")
        parser.add_argument("--demo-user", default="demo", help="Пользователь, которому выдать доступ ко всем компаниям")
        parser.add_argument("--demo-password", default="demo12345")
        parser.add_argument("--skip-patterns", action="store_true")

    def handle(self, *args, **opts):
        base = Path(opts["path"])
        comp = pd.read_csv(base / "companies.csv", sep=";", encoding="utf-8-sig", dtype=str)
        only = {c.strip() for c in opts["only"].split(",") if c.strip()}
        categorizer = ops_services.get_categorizer()

        User = get_user_model()
        user, created = User.objects.get_or_create(username=opts["demo_user"], defaults={"is_staff": True})
        if created:
            user.set_password(opts["demo_password"])
            user.save()
            self.stdout.write(f"Создан пользователь {opts['demo_user']} / {opts['demo_password']}")

        for row in comp.itertuples():
            if only and row.company_id not in only:
                continue
            company, _ = Company.objects.update_or_create(
                code=row.company_id,
                defaults={
                    "name": row.company_name,
                    "inn": row.company_inn,
                    "legal_form": row.legal_form,
                    "industry": row.industry,
                },
            )
            CompanyMembership.objects.get_or_create(user=user, company=company)
            path = base / "statements" / f"{row.company_id}.csv"
            if not path.exists():
                self.stderr.write(f"{row.company_id}: нет файла выписки")
                continue
            df = ingest.read_statement_csv(path)
            report = ops_services.import_operations(company, df, categorizer=categorizer)
            if not opts["skip_patterns"]:
                planning_services.sync_recurring_patterns(company)
            self.stdout.write(f"{row.company_id}: {report.rows_total} строк, дублей {report.rows_duplicates}, пропусков {report.gap_days}")
        self.stdout.write(self.style.SUCCESS("Импорт завершён"))
