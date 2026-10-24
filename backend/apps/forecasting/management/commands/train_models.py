"""Обучение моделей на всех компаниях из БД.

    python manage.py train_models [--train-until 2026-03-31] [--skip-categorizer] [--recategorize]

* категоризатор: TF-IDF + логистическая регрессия на уверенно размеченных правилами строках;
* модель потоков: глобальный HistGradientBoosting на панели «срез × горизонт».
"""
from __future__ import annotations

import time

import pandas as pd
from django.core.management.base import BaseCommand, CommandError

from apps.companies.models import Company
from apps.forecasting import services as fc_services
from apps.operations import services as ops_services
from cashflow_core.categorizer import Categorizer
from cashflow_core.categorizer.pipeline import apply_rules
from cashflow_core.forecast.models import HGBFlowModel, build_training_panel
from cashflow_core.forecast.series import daily_flows


class Command(BaseCommand):
    help = "Обучает категоризатор и модель прогноза потоков"

    def add_arguments(self, parser):
        parser.add_argument("--train-until", default=None, help="Обучать только на данных до даты (для честного бэктеста)")
        parser.add_argument("--skip-categorizer", action="store_true")
        parser.add_argument("--skip-flow", action="store_true")
        parser.add_argument("--recategorize", action="store_true", help="Пересчитать категории всех операций новой моделью")

    def handle(self, *args, **opts):
        companies = list(Company.objects.all())
        frames = {c.code: ops_services.ops_frame(c) for c in companies}
        until = pd.Timestamp(opts['train_until']) if opts['train_until'] else max((f.operation_date.max() for f in frames.values() if not f.empty), default=None)
        if until is None or pd.isna(until):
            raise CommandError('Нет данных для обучения')
        frames = {k: f[f.operation_date <= until].copy() for k, f in frames.items()}
        if not any(len(f) for f in frames.values()):
            raise CommandError('До указанной даты нет операций')
        all_ops = pd.concat([f for f in frames.values() if not f.empty], ignore_index=True)
        self.stdout.write(f"Операций в базе: {len(all_ops)}, компаний: {len(companies)}")

        if not opts["skip_categorizer"]:
            t = time.time()
            cat = Categorizer().fit(all_ops)
            cat.ml.save(str(ops_services.CATEGORIZER_PATH))
            ops_services.reset_categorizer_cache()
            self.stdout.write(f"Категоризатор обучен за {time.time() - t:.0f}s: {cat.metrics()}")
            if opts["recategorize"]:
                n = sum(ops_services.recategorize(c, cat) for c in companies)
                self.stdout.write(f"Пересчитано категорий: {n}")

        if not opts["skip_flow"]:
            t = time.time()
            daily = {}
            meta = {}
            for c in companies:
                f = frames[c.code]
                if f.empty:
                    continue
                # Stored labels may have been inferred using later operations.
                f = f.copy()
                f['category'] = apply_rules(f).category
                end = min(f.operation_date.max(), until) if until is not None else f.operation_date.max()
                d = daily_flows(f[f.operation_date <= end], end)
                if not d.empty:
                    daily[c.code] = d
                    meta[c.code] = {"industry": c.industry, "legal_form": c.legal_form}
            panel = build_training_panel(daily, meta, cutoffs_every=14, horizon=45, train_until=until)
            self.stdout.write(f"Обучающая панель: {len(panel)} строк")
            if panel.empty:
                raise CommandError('Недостаточно истории для обучающей панели')
            model = HGBFlowModel().fit(panel)
            model.train_info["train_until"] = str(until.date()) if until is not None else None
            model.save(str(fc_services.FLOW_MODEL_PATH))
            fc_services.reset_model_cache()
            self.stdout.write(self.style.SUCCESS(f"Модель потоков обучена за {time.time() - t:.0f}s → {fc_services.FLOW_MODEL_PATH}"))
