from django.conf import settings
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework import serializers
from rest_framework.exceptions import ValidationError
import math

from core.permissions import get_company_for_user

from . import services
from .models import ForecastRun


class ForecastParams(serializers.Serializer):
    as_of = serializers.DateField(required=False, allow_null=True)
    horizon = serializers.IntegerField(required=False, min_value=30, max_value=180, default=settings.FORECAST_DEFAULT_HORIZON)
    min_balance = serializers.FloatField(required=False, allow_null=True)

    def validate_min_balance(self, value):
        if value is not None and (not math.isfinite(value) or abs(value) > 1e15):
            raise serializers.ValidationError("Укажите конечную сумму в пределах 10^15")
        return value


def _params(request):
    q = request.query_params if request.method == "GET" else request.data
    params = ForecastParams(data={k:v for k,v in q.items() if k in ('as_of','horizon','min_balance') and v != ''})
    params.is_valid(raise_exception=True)
    p = params.validated_data
    return p.get('as_of'), p['horizon'], p.get('min_balance')


class ForecastView(APIView):
    """GET → базовый прогноз + оценка риска (сохраняется как снапшот)."""

    def get(self, request, company_pk):
        company = get_company_for_user(request.user, company_pk)
        as_of, horizon, mb = _params(request)
        return Response(services.run(company, as_of, horizon, None, mb, save=True))


class ScenarioView(APIView):
    """POST {"scenario": {...}} → базовый и негативный прогноз рядом."""

    def post(self, request, company_pk):
        company = get_company_for_user(request.user, company_pk)
        as_of, horizon, mb = _params(request)
        return Response(services.compare_scenario(company, request.data.get("scenario") or {}, as_of, horizon, mb))


class PlanFactView(APIView):
    def get(self, request, company_pk):
        company = get_company_for_user(request.user, company_pk)
        run_id = request.query_params.get("run_id")
        try:
            run_id = int(run_id) if run_id else None
        except (TypeError, ValueError) as exc:
            raise ValidationError({"run_id": "Ожидается целый идентификатор"}) from exc
        return Response(services.plan_fact(company, run_id))


class RunsView(APIView):
    def get(self, request, company_pk):
        company = get_company_for_user(request.user, company_pk)
        runs = ForecastRun.objects.filter(company=company, is_baseline=True).values("id", "as_of", "horizon_days", "model_name", "created_at", "risk")[:20]
        return Response([{**r, "as_of": str(r["as_of"]), "created_at": r["created_at"].isoformat(), "risk": {k: r["risk"].get(k) for k in ("level", "probability", "gap_date", "deficit_p50", "summary")}} for r in runs])


class BacktestView(APIView):
    def get(self, request):
        from .evaluation import report_for_company
        company_id = serializers.IntegerField(min_value=1).run_validation(request.query_params.get('company_id'))
        company = get_company_for_user(request.user, company_id)
        return Response(report_for_company(company) or {"detail": "Для этой компании ещё нет проверки текущей версии прогноза"})

    def post(self, request):
        from cashflow_core.backtest import BacktestConfig
        from .evaluation import evaluate, report_for_company
        import pandas as pd
        company_id = serializers.IntegerField(min_value=1).run_validation(request.data.get('company_id'))
        company = get_company_for_user(request.user, company_id)
        first, last = company.operations.order_by('operation_date').first(), company.operations.order_by('-operation_date').first()
        if first is None:
            raise ValidationError({'detail': 'Сначала загрузите выписку'})
        start = pd.Timestamp(first.operation_date) + pd.Timedelta(days=120)
        end = pd.Timestamp(last.operation_date) - pd.Timedelta(days=30)
        if start > end:
            raise ValidationError({'detail': 'Для проверки нужны минимум 120 дней истории и ещё 30 дней известного факта'})
        cfg = BacktestConfig(test_start=str(start.date()), test_end=str(end.date()), cutoffs_every_days=max(14, (end-start).days // 23 + 1))
        model = services.get_flow_model()
        until = getattr(model, 'train_info', {}).get('train_until')
        if until and start < pd.Timestamp(until) <= end:
            cfg.test_start = str(pd.Timestamp(until).date())
        try:
            evaluate([company], cfg)
        except ValueError as exc:
            raise ValidationError({'detail': str(exc)}) from exc
        return Response(report_for_company(company), status=201)
