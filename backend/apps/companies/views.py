import logging

import pandas as pd
from django.db import transaction
from rest_framework.exceptions import ValidationError
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.response import Response

from apps.operations import services as ops_services
from apps.planning import services as planning_services
from cashflow_core import ingest
from cashflow_core.forecast.series import balance_as_of, daily_flows
from cashflow_core.taxes import detect_regime
from core.permissions import companies_for_user, get_company_for_user

from .models import Company
from .serializers import CompanySerializer
from apps.accounts.models import CompanyMembership

log = logging.getLogger(__name__)


class CompanyViewSet(viewsets.ModelViewSet):
    serializer_class = CompanySerializer
    http_method_names = ["get", "patch", "post", "head", "options"]

    def get_queryset(self):
        return companies_for_user(self.request.user)

    def get_object(self):
        return get_company_for_user(self.request.user, self.kwargs["pk"])

    @transaction.atomic
    def perform_create(self, serializer):
        company = serializer.save()
        CompanyMembership.objects.create(company=company, user=self.request.user)

    @action(detail=False, methods=["post"], url_path="import-dataset", parser_classes=[MultiPartParser, FormParser])
    def import_dataset(self, request):
        from .dataset import import_dataset
        file = request.FILES.get("file")
        if not file:
            raise ValidationError({"detail": "Выберите CSV или ZIP с выписками"})
        try:
            dataset_id, rows = import_dataset(request.user, file.name, file.read())
        except (ValueError, OSError, KeyError) as exc:
            raise ValidationError({"detail": str(exc)}) from exc
        return Response({"dataset_id": str(dataset_id), "companies": [{"company": CompanySerializer(r["company"]).data, "report": r["report"]} for r in rows]}, status=201)

    @action(detail=True, methods=["get"])
    def summary(self, request, pk=None):
        """Сводка для шапки дашборда: остаток, режим, качество данных, структура потоков."""
        company = self.get_object()
        ops = ops_services.ops_frame(company)
        from apps.forecasting.services import resolve_as_of
        as_of = resolve_as_of(company, request.query_params.get("as_of")) if company.as_of or request.query_params.get("as_of") else (ops.operation_date.max() if len(ops) else None)
        if ops.empty or as_of is None:
            return Response({"company": CompanySerializer(company).data, "balance": None, "empty": True})
        hist = ops[ops.operation_date <= as_of]
        daily = daily_flows(hist, as_of)
        last90 = daily.tail(63)
        regime = detect_regime(hist, company.legal_form)
        return Response(
            {
                "company": CompanySerializer(company).data,
                "as_of": as_of.strftime("%Y-%m-%d"),
                "balance": balance_as_of(hist, as_of),
                "operations_total": int(len(hist)),
                "history_days": int((as_of - hist.operation_date.min()).days) if len(hist) else 0,
                "avg_daily_inflow": round(float(last90.inflow.mean()), 2) if len(last90) else 0,
                "avg_daily_outflow": round(float(last90.outflow.mean()), 2) if len(last90) else 0,
                "monthly_turnover": round(float(hist[hist.operation_date > as_of - pd.Timedelta(days=91)].amount.sum() / 3), 2),
                "tax_regime": regime,
                "ingest_report": ops_services.last_ingest_report(company),
            }
        )

    @action(detail=True, methods=["post"], parser_classes=[MultiPartParser, FormParser])
    def upload(self, request, pk=None):
        """Загрузка выписки (CSV кейса или 1С:Клиент-банк). Новые операции учитываются сразу."""
        company = self.get_object()
        f = request.FILES.get("file")
        if not f:
            return Response({"detail": "Файл не передан (поле file)"}, status=status.HTTP_400_BAD_REQUEST)
        try:
            df = ingest.load_any(f.name, f.read())
        except Exception as exc:  # noqa: BLE001 — сообщаем пользователю, не раскрывая содержимое
            log.warning("upload parse failed for company %s: %s", company.code, type(exc).__name__)
            return Response({"detail": f"Не удалось разобрать файл: {type(exc).__name__}"}, status=status.HTTP_400_BAD_REQUEST)
        try:
            report = ops_services.import_operations(company, df)
        except ValueError as exc:
            raise ValidationError({"detail": str(exc)}) from exc
        planning_services.sync_recurring_patterns(company)
        planning_services.refresh_event_statuses(company)
        return Response({"report": report.as_dict(), "company": CompanySerializer(company).data})

    @action(detail=True, methods=["get"])
    def history(self, request, pk=None):
        """Факт остатка по дням (для графика «что было на самом деле» и план-факта)."""
        company = self.get_object()
        ops = ops_services.ops_frame(company)
        if ops.empty:
            return Response([])
        date_from = request.query_params.get("from")
        date_to = request.query_params.get("to")
        from cashflow_core.forecast.series import actual_daily
        from rest_framework import serializers
        date_from = serializers.DateField().run_validation(date_from) if date_from else None
        date_to = serializers.DateField().run_validation(date_to) if date_to else None
        daily = actual_daily(ops, ops.operation_date.max())
        if date_from:
            daily = daily[daily.index >= pd.Timestamp(date_from)]
        if date_to:
            daily = daily[daily.index <= pd.Timestamp(date_to)]
        out = daily[["inflow", "outflow", "balance"]].reset_index()
        out["date"] = out["date"].dt.strftime("%Y-%m-%d")
        return Response(out.round(2).to_dict(orient="records"))
