"""Reproducible evaluation shared by the CLI and authenticated company API."""
from dataclasses import asdict
import hashlib
import time
import pandas as pd
from apps.operations.services import ops_frame
from cashflow_core.backtest import BacktestRow, evaluate_company, summarize
from cashflow_core.forecast import get_model
from cashflow_core.forecast.engine import ENGINE_VERSION
from .models import BacktestResult
from .services import get_calibrator, get_flow_model


def fingerprint(frame):
    return hashlib.sha256(pd.util.hash_pandas_object(frame, index=False).values.tobytes()).hexdigest()


def evaluate(companies, cfg, model_names=("naive", "seasonal_naive", "hgb"), name="control_dataset", progress=None, calibrator="auto"):
    """``calibrator``: "auto" — применить сохранённую калибровку, если она есть; None — сырые вероятности."""
    started = time.monotonic()
    models, warnings = {}, []
    if calibrator == "auto":
        calibrator = get_calibrator()
    if calibrator is not None:
        fitted_until = calibrator.info.get("test_end")
        if fitted_until and pd.Timestamp(fitted_until) >= pd.Timestamp(cfg.test_start):
            warnings.append(f"Калибровка обучена на периоде до {fitted_until}, который пересекается с проверяемым: метрики разрывов оптимистичны")
    for key in model_names:
        if key == "hgb":
            model = get_flow_model()
            until = getattr(model, "train_info", {}).get("train_until")
            if model.name != 'hgb' or not until or pd.Timestamp(until) > pd.Timestamp(cfg.test_start):
                warnings.append("HGB пропущена: нет совместимой модели, обученной до начала тестового периода")
                continue
        else:
            model = get_model(key)
        models[key] = model
    if not models:
        raise ValueError("Нет моделей, допустимых для выбранного периода. Переобучите HGB до начала теста или включите базовые модели.")
    rows, sources = [], []
    for i, company in enumerate(companies, 1):
        frame = ops_frame(company)
        sources.append({"id": company.id, "code": company.code, "dataset_id": str(company.dataset_id or ''), "rows": len(frame), "sha256": fingerprint(frame)})
        result = evaluate_company(frame, {"company_id": company.code, "industry": company.industry, "legal_form": company.legal_form}, models, cfg, calibrator=calibrator)
        rows.extend(result)
        if not result:
            warnings.append(f"{company.code}: недостаточно истории и факта для полного горизонта")
        if progress:
            progress(i, len(companies), len(rows), time.monotonic() - started)
    summary = summarize(rows)
    summary["horizon"] = cfg.horizon
    return BacktestResult.objects.create(name=name[:64], config={**asdict(cfg), "engine_version": ENGINE_VERSION,
        "sources": sources, "warnings": warnings, "duration_seconds": round(time.monotonic() - started, 2),
        "categorization": "rules_and_counterparty_on_history_only", "models": {k: getattr(m, 'train_info', {}) for k,m in models.items()},
        "calibration": calibrator.info if calibrator is not None else None},
        summary=summary, rows=[asdict(r) for r in rows])


def report_for_company(company):
    for rec in BacktestResult.objects.all().iterator():
        if rec.config.get('engine_version') != ENGINE_VERSION:
            continue
        source = next((s for s in rec.config.get('sources', []) if s['id'] == company.id), None)
        if source is None:
            continue
        rows = [r for r in rec.rows if r['company_id'] == company.code]
        summary = summarize([BacktestRow(**r) for r in rows])
        summary['horizon'] = rec.config['horizon']
        config = {**rec.config, 'sources': [source]}
        config['warnings'] = [w for w in config.get('warnings', []) if ':' not in w or w.startswith(('HGB', company.code + ':'))]
        return {'id': rec.id, 'name': rec.name, 'created_at': rec.created_at.isoformat(), 'config': config,
                'summary': summary, 'categorizer_metrics': {}, 'rows_sample': rows[:200],
                'stale': source['sha256'] != fingerprint(ops_frame(company))}
    return None
