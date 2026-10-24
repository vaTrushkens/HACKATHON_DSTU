from django.conf import settings
from rest_framework.response import Response
from rest_framework.views import APIView

from core.permissions import get_company_for_user

from . import services
from .models import ForecastRun


def _params(request):
    q = request.query_params if request.method == "GET" else request.data
    horizon = int(q.get("horizon", settings.FORECAST_DEFAULT_HORIZON))
    horizon = max(30, min(horizon, 180))
    mb = q.get("min_balance")
    return q.get("as_of") or None, horizon, (float(mb) if mb not in (None, "") else None)


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
        return Response(services.plan_fact(company, int(run_id) if run_id else None))


class RunsView(APIView):
    def get(self, request, company_pk):
        company = get_company_for_user(request.user, company_pk)
        runs = ForecastRun.objects.filter(company=company, is_baseline=True).values("id", "as_of", "horizon_days", "model_name", "created_at", "risk")[:20]
        return Response([{**r, "as_of": str(r["as_of"]), "created_at": r["created_at"].isoformat(), "risk": {k: r["risk"].get(k) for k in ("level", "probability", "gap_date", "deficit_p50", "summary")}} for r in runs])


class BacktestView(APIView):
    def get(self, request):
        return Response(services.latest_backtest() or {"detail": "Бэктест ещё не запускался: python manage.py run_backtest"})
