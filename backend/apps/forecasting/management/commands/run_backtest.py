"""Rolling-origin бэктест моделей прогноза.

    python manage.py run_backtest --models naive,seasonal_naive,hgb --test-start 2026-04-01 --test-end 2026-08-31

Чтобы hgb не «видел будущее», обучите её командой train_models --train-until <test-start>.
"""
from __future__ import annotations

import time

from django.core.management.base import BaseCommand

from apps.companies.models import Company
from apps.forecasting import services as fc_services
from apps.forecasting.models import BacktestResult
from apps.operations import services as ops_services
from cashflow_core.backtest import BacktestConfig, evaluate_company, summarize
from cashflow_core.categorizer.ml import MLCategorizer
from cashflow_core.forecast import get_model


class Command(BaseCommand):
    help = "Запускает бэктест и сохраняет результаты для экрана «Качество модели»"

    def add_arguments(self, parser):
        parser.add_argument("--models", default="naive,seasonal_naive,hgb")
        parser.add_argument("--test-start", default="2026-04-01")
        parser.add_argument("--test-end", default="2026-08-31")
        parser.add_argument("--horizon", type=int, default=30)
        parser.add_argument("--every", type=int, default=14)
        parser.add_argument("--paths", type=int, default=300)
        parser.add_argument("--limit", type=int, default=0, help="Ограничить число компаний (для быстрого прогона)")
        parser.add_argument("--name", default="default")

    def handle(self, *args, **opts):
        cfg = BacktestConfig(horizon=opts["horizon"], cutoffs_every_days=opts["every"], test_start=opts["test_start"], test_end=opts["test_end"], n_paths=opts["paths"])
        models = {}
        for name in opts["models"].split(","):
            name = name.strip()
            models[name] = get_model(name, str(fc_services.FLOW_MODEL_PATH)) if name == "hgb" else get_model(name)
        companies = list(Company.objects.all())
        if opts["limit"]:
            companies = companies[: opts["limit"]]
        rows = []
        t = time.time()
        for i, c in enumerate(companies, 1):
            ops = ops_services.ops_frame(c)
            meta = {"company_id": c.code, "industry": c.industry, "legal_form": c.legal_form}
            rows += evaluate_company(ops, meta, models, cfg)
            if i % 10 == 0:
                self.stdout.write(f"  {i}/{len(companies)} компаний, {len(rows)} прогнозов, {time.time() - t:.0f}s")
        summary = summarize(rows)
        summary["horizon"] = cfg.horizon
        cat_metrics = {}
        if ops_services.CATEGORIZER_PATH.exists():
            cat_metrics = MLCategorizer.load(str(ops_services.CATEGORIZER_PATH)).metrics
        BacktestResult.objects.create(name=opts["name"], config=cfg.__dict__, summary=summary, rows=[r.__dict__ for r in rows], categorizer_metrics=cat_metrics)
        self.stdout.write(self.style.SUCCESS(f"Готово: {len(rows)} прогнозов за {time.time() - t:.0f}s"))
        for m in summary["models"]:
            self.stdout.write(f"  {m['model']:16s} MAE/оборот={m['mae_norm_median']:.3f} MASE={m['mase_median']:.3f} cover80={m['coverage80']:.2f} gapP={m['gap_precision']} gapR={m['gap_recall']} lead={m['lead_days_mean']}")
