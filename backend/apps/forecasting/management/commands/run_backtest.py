"""Rolling-origin бэктест моделей прогноза.

    python manage.py run_backtest --models naive,seasonal_naive,hgb --test-start 2026-04-01 --test-end 2026-08-31

Чтобы hgb не «видел будущее», обучите её командой train_models --train-until <test-start>.
"""
from __future__ import annotations

from django.core.management.base import CommandError
from core.commands import LoggedCommand
from apps.forecasting.evaluation import evaluate

from apps.companies.models import Company
from cashflow_core.backtest import BacktestConfig


class Command(LoggedCommand):
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
        parser.add_argument("--company", help="Код компании в базе")
        parser.add_argument("--dataset", help="UUID импортированного набора")
        parser.add_argument("--actual-mode", choices=['actual', 'without_financing'], default='actual')
        parser.add_argument("--fit-calibration", action="store_true", help="По результатам этого прогона обучить калибровку вероятности и сохранить в models_store (запускайте на периоде ДО тестового)")
        parser.add_argument("--no-calibration", action="store_true", help="Не применять сохранённую калибровку (сырые вероятности Монте-Карло)")

    def handle(self, *args, **opts):
        companies = Company.objects.order_by('code')
        if opts['company']:
            companies = companies.filter(code=opts['company'])
        if opts['dataset']:
            companies = companies.filter(dataset_id=opts['dataset'])
        if opts["limit"]:
            companies = companies[: opts["limit"]]
        companies = list(companies)
        if not companies:
            raise CommandError('Не найдены компании')
        try:
            cfg = BacktestConfig(horizon=opts['horizon'], cutoffs_every_days=opts['every'], test_start=opts['test_start'], test_end=opts['test_end'], n_paths=opts['paths'], actual_mode=opts['actual_mode'])
            rec = evaluate(companies, cfg, [s.strip() for s in opts['models'].split(',')], opts['name'],
                progress=lambda i,n,r,t: self.stdout.write(f'{i}/{n} компаний; {r} прогнозов; {t:.0f} с'),
                calibrator=None if (opts['fit_calibration'] or opts['no_calibration']) else "auto")
        except (ValueError, KeyError) as exc:
            raise CommandError(str(exc)) from exc
        for warning in rec.config['warnings']:
            self.stdout.write(self.style.WARNING(warning))
        for m in rec.summary['models']:
            self.stdout.write(f"{m['model']}: MAE={m['mae_balance']}, MSE={m['mse_balance']}, Accuracy={m['gap_accuracy']}, Precision={m['gap_precision']}, Recall={m['gap_recall']}, F1={m['gap_f1']}, Brier={m['brier_score']}; {m['confusion']}")
            for key, title in (("warning", "предупреждение (P ≥ порога)"), ("early", "ранний сигнал (худшие 10 %)")):
                w = m["early_warning"][key]
                self.stdout.write(f"    {title}: сигналов {w['signals']}, верных {w['precision']}; разрывов обнаружено {w['detected']} из {w['episodes']}; упреждение медиана {w['lead_median']} дн., ≥7 дн. у {w['lead_ge7_share']}, ≥14 дн. у {w['lead_ge14_share']}")
        self.stdout.write(self.style.SUCCESS(f"Сохранён бэктест #{rec.id}: {rec.summary['n_forecasts']} прогнозов"))
        if opts['fit_calibration']:
            self._fit_calibration(rec, cfg)

    def _fit_calibration(self, rec, cfg):
        """Калибровка по модели, которую использует сервис (hgb, если обучена, иначе seasonal_naive)."""
        from apps.forecasting import services as fc_services
        from cashflow_core.calibration import ProbabilityCalibrator
        live = fc_services.get_flow_model().name
        rows = [r for r in rec.rows if r['model'] == live]
        if not rows:
            raise CommandError(f"В прогоне нет результатов модели «{live}», которой пользуется сервис")
        try:
            cal = ProbabilityCalibrator.fit([r['gap_prob'] for r in rows], [r['actual_gap'] for r in rows],
                {"flow_model": live, "test_start": cfg.test_start, "test_end": cfg.test_end, "backtest_id": rec.id})
        except ValueError as exc:
            raise CommandError(str(exc)) from exc
        cal.save(str(fc_services.CALIBRATION_PATH))
        fc_services.reset_model_cache()
        self.stdout.write(self.style.SUCCESS(f"Калибровка сохранена → {fc_services.CALIBRATION_PATH}: Brier {cal.info['brier_raw']} → {cal.info['brier_calibrated']} на {cal.info['samples']} срезах"))
