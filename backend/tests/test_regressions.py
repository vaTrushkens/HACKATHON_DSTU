"""Regression cases from the end-to-end specification audit."""
import io
import json
from dataclasses import replace
from unittest.mock import patch
import numpy as np
import pandas as pd
import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from apps.companies.models import Company
from apps.operations.services import import_operations
from apps.planning.models import PlannedEvent
from apps.planning.services import planned_events_frame
from cashflow_core import ingest
from cashflow_core.categorizer import Categorizer
from cashflow_core.categorizer.ml import MLCategorizer
from cashflow_core.forecast import ForecastInputs, NaiveMeanModel, Scenario, run_forecast
from cashflow_core.forecast.models import HGBFlowModel, make_feature_rows
from cashflow_core.forecast.series import daily_flows, actual_daily
from cashflow_core.planfact import match_events
from cashflow_core.recurring import detect_recurring
from cashflow_core.risk import assess
from cashflow_core.backtest import BacktestConfig, evaluate_company, summarize
from .test_api import client_and_company
from .test_core import _synthetic_ops


def categorized(months=14):
    raw = _synthetic_ops(months=months)
    return pd.concat([raw, Categorizer().transform(raw)], axis=1)


def test_missing_balance_preserves_end_of_day_anchors():
    raw = _synthetic_ops().iloc[:3].copy()
    raw['operation_date'] = pd.to_datetime(['2026-01-12','2026-01-12','2026-01-13'])
    raw['direction'] = ['Поступление','Списание','Поступление']
    raw['amount'] = [100.,50.,200.]
    raw['balance_eod'] = [1000., np.nan,1200.]
    result, _ = ingest.clean_operations(raw)
    assert result.balance_eod.tolist() == [1000.,1000.,1200.]


def test_identical_content_with_different_ids_is_not_a_duplicate():
    raw = _synthetic_ops().iloc[[0]].copy()
    other = raw.assign(operation_id='different')
    result, report = ingest.clean_operations(pd.concat([raw,other]))
    assert len(result) == 2 and report.rows_duplicates == 0


def test_incremental_document_numbers_and_idless_files(client_and_company):
    _, _, company = client_and_company
    raw = _synthetic_ops().iloc[[0]].copy()
    assert import_operations(company, raw).rows_new == 1
    assert import_operations(company, raw.assign(operation_date=raw.operation_date + pd.Timedelta(days=1))).rows_new == 1
    no_ids = raw.drop(columns='operation_id').assign(operation_date=raw.operation_date + pd.Timedelta(days=2))
    no_ids = ingest.to_standard_frame(no_ids)
    assert import_operations(company, no_ids).rows_new == 1
    assert import_operations(company, no_ids).rows_new == 0


def test_incremental_missing_balance_uses_existing_history(client_and_company):
    _, _, company = client_and_company
    raw = _synthetic_ops().iloc[[0]].copy()
    raw['balance_eod'] = 1000.
    import_operations(company, raw)
    new = raw.assign(operation_id='next', operation_date=raw.operation_date+pd.Timedelta(days=1), amount=200., balance_eod=np.nan)
    import_operations(company,new)
    assert float(company.operations.get(operation_id='next').balance_eod) == 1200.


def test_company_creation_grants_access_and_unique_codes(client_and_company):
    client, stranger, _ = client_and_company
    first = client.post('/api/companies/', {'name':'First'}, format='json')
    second = client.post('/api/companies/', {'name':'Second'}, format='json')
    assert first.status_code == second.status_code == 201
    assert first.data['code'] and first.data['code'] != second.data['code']
    assert client.get(f"/api/companies/{first.data['id']}/").status_code == 200
    assert stranger.get(f"/api/companies/{first.data['id']}/").status_code == 403


def test_mixed_upload_rejected_and_new_dataset_isolated(client_and_company):
    client, stranger, company = client_and_company
    raw = _synthetic_ops().iloc[:2].copy()
    raw['company_id'] = ['controlA','controlB']
    content = raw.to_csv(index=False).encode('utf-8')
    response = client.post(f'/api/companies/{company.id}/upload/', {'file':SimpleUploadedFile('data.csv',content)}, format='multipart')
    assert response.status_code == 400 and company.operations.count() == 0
    response = client.post('/api/companies/import-dataset/', {'file':SimpleUploadedFile('data.csv',content)}, format='multipart')
    assert response.status_code == 201, response.data
    assert len(response.data['companies']) == 2
    codes = []
    for item in response.data['companies']:
        c = Company.objects.get(id=item['company']['id'])
        assert c.dataset_id and c.operations.count() == 1
        codes.append(c.external_code)
        assert stranger.get(f'/api/companies/{c.id}/').status_code == 403
    assert set(codes) == {'controlA','controlB'}
    assert company.operations.count() == 0


@pytest.mark.parametrize('params', [{'horizon':'abc'},{'horizon':0},{'horizon':200},{'as_of':'invalid'},{'min_balance':'NaN'}])
def test_invalid_forecast_parameters_are_400(client_and_company, params):
    client, _, company = client_and_company
    assert client.get(f'/api/companies/{company.id}/forecast/', params).status_code == 400


def test_invalid_contract_category_and_scenario_are_400(client_and_company):
    client, _, company = client_and_company
    root = f'/api/companies/{company.id}'
    contract = {'counterparty_name':'X','direction':'Списание','total_amount':100000,'start_date':'2026-07-01','end_date':'2026-07-31','schedule_type':'prepay_final','prepayment_pct':150}
    assert client.post(root+'/contracts/', contract, format='json').status_code == 400
    import_operations(company, _synthetic_ops().iloc[[0]])
    op = company.operations.first()
    assert client.patch(root+f'/operations/{op.id}/', {'category':'SALARY'}, format='json').status_code == 400
    for sc in [{'extra_expenses':[4]}, {'revenue_change_pct':-101}, {'delay_client_days':1.5}, {'extra_incomes':[{'date':'2026-07-01','amount':1,'category':'SALARY'}]}]:
        assert client.post(root+'/scenario/', {'scenario':sc}, format='json').status_code == 400


def test_future_fact_and_stored_matched_status_do_not_hide_obligation(client_and_company):
    _, _, company = client_and_company
    raw = _synthetic_ops().iloc[[0]].copy()
    raw['operation_date'] = pd.Timestamp('2026-07-12')
    raw['amount'] = 1000.
    import_operations(company, raw)
    e = PlannedEvent.objects.create(company=company, date='2026-07-10', amount=1000, direction='Поступление', category='REV_ACQUIRING', counterparty_name=raw.iloc[0].counterparty_name, counterparty_inn=raw.iloc[0].counterparty_inn, status='matched')
    frame = planned_events_frame(company, pd.Timestamp('2026-07-01'), pd.Timestamp('2026-07-31'))
    assert len(frame) == 1 and frame.iloc[0].amount == 1000
    assert e.pk


def test_overdue_expense_is_in_forecast_and_income_is_uncertain(client_and_company):
    _, _, company = client_and_company
    for direction,cat in [('Списание','RENT'),('Поступление','REV_CLIENTS')]:
        PlannedEvent.objects.create(company=company,date='2026-06-20',amount=1000,direction=direction,category=cat,status='overdue')
    frame = planned_events_frame(company, pd.Timestamp('2026-06-30'), pd.Timestamp('2026-07-30'))
    expense = frame[frame.direction == 'Списание'].iloc[0]
    income = frame[frame.direction == 'Поступление'].iloc[0]
    assert expense.date == pd.Timestamp('2026-07-01') and expense.probability == 1
    assert income.probability <= .5 and income.timing_std_days >= 5


def test_invoice_replaces_one_recurring_payment_and_cache_is_reused():
    ops = categorized()
    as_of = pd.Timestamp('2026-06-30')
    model = NaiveMeanModel()
    inp = ForecastInputs(ops,as_of,60,patterns=detect_recurring(ops,as_of),include_taxes=False)
    base = run_forecast(inp,model,n_paths=100)
    invoice = base.events[base.events.category == 'RENT'].iloc[[0]].copy()
    invoice['source'] = 'invoice:1c'
    invoice['pattern_key'] = 'invoice:one'
    result = run_forecast(replace(inp,planned_events=invoice),model,n_paths=100)
    assert result.events[result.events.category == 'RENT'].amount.sum() == base.events[base.events.category == 'RENT'].amount.sum()
    assert result.calculation_log[3]['reused_statistical_layer'] is True
    np.testing.assert_allclose(result.paths, base.paths)
    json.dumps(result.to_dict(), allow_nan=False)


def test_recurring_customer_is_removed_from_statistical_layer():
    from cashflow_core.calendar_ru import shift_to_business_day
    rows = []
    for i, month in enumerate(pd.period_range('2025-07','2026-06',freq='M')):
        date = shift_to_business_day(month.start_time + pd.Timedelta(days=9),'forward')
        rows.append({'operation_id':str(i),'operation_date':date,'amount':100000.,'direction':'Поступление','category':'REV_CLIENTS','counterparty_inn':'1234567890','counterparty_name':'Client','payment_purpose':'Оплата услуг','balance_eod':100000.*(i+1)})
    ops = pd.DataFrame(rows); as_of = pd.Timestamp('2026-06-30')
    inp = ForecastInputs(ops,as_of,30,patterns=detect_recurring(ops,as_of),include_taxes=False)
    result = run_forecast(inp,NaiveMeanModel(),n_paths=100)
    assert result.components['det_in_total'] == 100000
    assert result.components['stoch_in_total'] == 0
    changed = run_forecast(replace(inp, scenario=Scenario(revenue_change_pct=-20)),NaiveMeanModel(),n_paths=100)
    assert changed.components['det_in_total'] == 80000


def test_calendar_horizon_feature_matches_training_and_serving():
    daily = daily_flows(categorized(),pd.Timestamp('2026-07-03'))
    calendar = make_feature_rows(daily,pd.date_range('2026-07-04','2026-07-10'),{})
    workdays = make_feature_rows(daily,pd.bdate_range('2026-07-06','2026-07-10'),{})
    assert calendar.loc[calendar.date == pd.Timestamp('2026-07-06'),'h'].iloc[0] == workdays.iloc[0].h == 3


def test_high_integral_risk_has_no_false_safe_summary():
    result = run_forecast(ForecastInputs(categorized(),pd.Timestamp('2026-06-30'),30),NaiveMeanModel(),n_paths=100)
    paths = np.full((100,4),100.)
    for i in range(100): paths[i,i//25] = -100.
    daily = result.daily.iloc[:4].copy()
    daily['p10'],daily['p50'],daily['p90'] = np.percentile(paths,[10,50,90],axis=0)
    daily['mean'] = paths.mean(axis=0); daily['prob_below'] = (paths<0).mean(axis=0)
    risk = assess(replace(result,paths=paths,daily=daily,events=result.events.iloc[:0],opening_balance=100))
    assert risk.probability == 1 and risk.level == 'high' and risk.gap_date is None
    assert 'Денег хватает' not in risk.summary and risk.deficit_p50 == 100


def test_backtest_rejects_future_model_and_handles_no_cutoffs():
    ops = categorized()
    cfg = BacktestConfig()
    assert evaluate_company(ops,{}, {'naive':NaiveMeanModel()},cfg,cutoffs=[]) == []
    model = HGBFlowModel(train_info={'feature_version':2,'train_until':'2026-09-01'})
    with pytest.raises(ValueError): evaluate_company(ops,{}, {'hgb':model},cfg,cutoffs=[pd.Timestamp('2026-06-01')])


def test_metrics_zero_f1_absolute_errors_and_json():
    rows = evaluate_company(categorized(),{'company_id':'test'},{'naive':NaiveMeanModel()},BacktestConfig(n_paths=30),cutoffs=[pd.Timestamp('2026-06-01')])
    assert rows
    row = replace(rows[0], actual_gap=True,predicted_gap=False,gap_prob=0.)
    summary = summarize([row]); metrics = summary['models'][0]
    assert metrics['gap_f1'] == 0 and metrics['gap_accuracy'] == 0 and metrics['brier_score'] == 1
    assert metrics['mae_balance'] > 0 and metrics['mse_balance'] > 0
    json.dumps(summary,allow_nan=False)


def test_quality_api_scopes_and_staleness(client_and_company):
    from apps.forecasting.evaluation import evaluate
    client, stranger, company = client_and_company
    import_operations(company, _synthetic_ops())
    evaluate([company],BacktestConfig(test_start='2026-06-01',test_end='2026-06-01',n_paths=20),['naive'])
    response = client.get('/api/backtest/',{'company_id':company.id})
    assert response.status_code == 200 and response.data['summary']['n_companies'] == 1
    assert response.data['stale'] is False
    assert stranger.get('/api/backtest/',{'company_id':company.id}).status_code == 403
    company.operations.filter(pk=company.operations.first().pk).update(amount=1)
    assert client.get('/api/backtest/',{'company_id':company.id}).data['stale'] is True


def test_rare_category_does_not_crash_training():
    raw = _synthetic_ops().iloc[:60].copy(); raw['direction'] = 'Списание'
    cat = MLCategorizer().fit(raw,pd.Series(['SALARY']*59+['RENT']),pd.Series([.95]*60))
    assert cat is not None


def test_weekend_actual_keeps_original_date():
    ops = categorized().iloc[:2].copy()
    ops['operation_date'] = pd.to_datetime(['2026-07-03','2026-07-04'])
    actual = actual_daily(ops,pd.Timestamp('2026-07-05'))
    assert actual.loc['2026-07-04','inflow'] == ops.iloc[1].amount
    assert actual.loc['2026-07-05','inflow'] == 0


def test_industry_profile_uses_only_peers_available_at_cutoff(client_and_company):
    from apps.forecasting.services import build_inputs
    _, _, company = client_and_company
    peer = Company.objects.create(code='PEER',name='Peer',industry=company.industry)
    import_operations(peer,_synthetic_ops(months=8))
    import_operations(company,_synthetic_ops(months=1))
    as_of = pd.Timestamp('2025-06-30')
    assert build_inputs(company,as_of,30,Scenario(),0).industry_profile is None
    # Long peer history exists by December; the short company starts then.
    company.operations.all().delete()
    raw = _synthetic_ops(months=1)
    raw['operation_date'] += pd.Timedelta(days=183)
    import_operations(company,raw)
    inp = build_inputs(company,pd.Timestamp('2025-12-31'),30,Scenario(),0)
    assert inp.industry_profile['peers'] == 1
    result = run_forecast(inp,NaiveMeanModel(),n_paths=20)
    assert result.model_name == 'industry_profile'
    json.dumps(result.to_dict(),allow_nan=False)


def test_backtest_api_runs_for_selected_company(client_and_company):
    client, _, company = client_and_company
    import_operations(company,_synthetic_ops(months=6))
    with patch('apps.forecasting.services.get_flow_model', return_value=NaiveMeanModel()):
        with patch('apps.forecasting.evaluation.get_flow_model', return_value=NaiveMeanModel()):
            response = client.post('/api/backtest/',{'company_id':company.id},format='json')
    assert response.status_code == 201, response.data
    assert response.data['summary']['n_forecasts'] > 0
    assert response.data['config']['sources'][0]['id'] == company.id
    assert response.data['config']['warnings']


def test_plan_fact_json_has_no_nan_for_unobserved_future(client_and_company):
    from apps.forecasting.services import run, plan_fact
    _, _, company = client_and_company
    import_operations(company,_synthetic_ops(months=6))
    fc = run(company,as_of='2025-11-15',horizon=60,model=NaiveMeanModel())
    result = plan_fact(company,fc['run_id'])
    json.dumps(result,allow_nan=False)
