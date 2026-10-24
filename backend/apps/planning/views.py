import xml.etree.ElementTree as ET

import pandas as pd
from django.conf import settings
from django.db.models import Count
from rest_framework import mixins, status, viewsets
from rest_framework.response import Response
from rest_framework.views import APIView

from core.permissions import get_company_for_user

from . import importers, services
from .connectors import CONFIG_FIELDS, ConnectorError, connector, ping
from .integrations import ADAPTERS, RegistryAdapter
from .models import Contract, IntegrationConfig, PlannedEvent, RecurringPattern
from .serializers import ContractSerializer, PlannedEventSerializer, RecurringPatternSerializer


def _as_of(request, company) -> pd.Timestamp:
    return pd.Timestamp(request.query_params.get("as_of") or company.as_of or pd.Timestamp.today()).normalize()


class RecurringViewSet(mixins.ListModelMixin, mixins.UpdateModelMixin, viewsets.GenericViewSet):
    serializer_class = RecurringPatternSerializer
    pagination_class = None
    http_method_names = ["get", "patch", "post", "head", "options"]

    def get_queryset(self):
        company = get_company_for_user(self.request.user, self.kwargs["company_pk"])
        return RecurringPattern.objects.filter(company=company).order_by("-active", "-amount")

    def create(self, request, company_pk=None):
        """POST → пересчитать регулярные платежи по выписке."""
        company = get_company_for_user(request.user, company_pk)
        n = services.sync_recurring_patterns(company)
        return Response({"patterns": n})


class PlannedEventViewSet(viewsets.ModelViewSet):
    serializer_class = PlannedEventSerializer
    pagination_class = None

    def get_queryset(self):
        company = get_company_for_user(self.request.user, self.kwargs["company_pk"])
        qs = PlannedEvent.objects.filter(company=company)
        st = self.request.query_params.get("status")
        if st:
            qs = qs.filter(status__in=st.split(","))
        return qs

    def perform_create(self, serializer):
        company = get_company_for_user(self.request.user, self.kwargs["company_pk"])
        serializer.save(company=company, created_by=self.request.user)

    def destroy(self, request, *args, **kwargs):
        obj = self.get_object()
        obj.status = "cancelled"
        obj.save(update_fields=["status", "updated_at"])
        return Response(status=status.HTTP_204_NO_CONTENT)


class ContractViewSet(viewsets.ModelViewSet):
    """Договоры: создание сразу разворачивает график платежей в календарь."""

    serializer_class = ContractSerializer
    pagination_class = None

    def get_queryset(self):
        company = get_company_for_user(self.request.user, self.kwargs["company_pk"])
        return Contract.objects.filter(company=company, active=True)

    def perform_create(self, serializer):
        company = get_company_for_user(self.request.user, self.kwargs["company_pk"])
        contract = serializer.save(company=company, created_by=self.request.user)
        services.generate_contract_events(contract)

    def perform_update(self, serializer):
        services.generate_contract_events(serializer.save())

    def destroy(self, request, *args, **kwargs):
        obj = self.get_object()
        obj.active = False
        obj.save(update_fields=["active"])
        PlannedEvent.objects.filter(company=obj.company, external_ref=obj.ref, status__in=["expected", "overdue"]).update(status="cancelled")
        return Response(status=status.HTTP_204_NO_CONTENT)


class TaxCalendarView(APIView):
    def get(self, request, company_pk):
        company = get_company_for_user(request.user, company_pk)
        horizon = int(request.query_params.get("horizon", settings.FORECAST_DEFAULT_HORIZON))
        return Response(services.tax_calendar_for(company, _as_of(request, company), horizon))


class CounterpartiesView(APIView):
    def get(self, request, company_pk):
        company = get_company_for_user(request.user, company_pk)
        return Response(services.counterparties_overview(company, _as_of(request, company)))


def _masked(cfg: IntegrationConfig | None) -> dict:
    """Значения ключей наружу не отдаём — только признак, что они заданы, и последние 4 символа."""
    if not cfg:
        return {}
    return {k: (f"…{str(v)[-4:]}" if k != "base_url" else v) if v else "" for k, v in cfg.config.items()}


class IntegrationsView(APIView):
    def get(self, request, company_pk):
        company = get_company_for_user(request.user, company_pk)
        counts = dict(PlannedEvent.objects.filter(company=company).exclude(status="cancelled").values_list("source").annotate(n=Count("id")))
        configs = {c.kind: c for c in IntegrationConfig.objects.filter(company=company)}
        out = []
        for key, meta in ADAPTERS.items():
            item = {"source": key, **meta, "events": counts.get(key, 0)}
            ck = meta.get("config")
            if ck:
                cfg = configs.get(ck)
                client = connector(company, ck)
                item.update({"configured": client.configured, "mode": "api" if client.configured else ("derived" if key == "bank" else "mock"), "fields": [{"name": n, "label": l} for n, l in CONFIG_FIELDS[ck]], "values": _masked(cfg), "last_sync": cfg.last_sync.isoformat() if cfg and cfg.last_sync else None, "last_status": cfg.last_status if cfg else "", "last_error": cfg.last_error if cfg else ""})
            else:
                item["mode"] = "file" if "import_invoices" in meta["actions"] or "import_ofd" in meta["actions"] else "active"
            out.append(item)
        return Response(out)

    def post(self, request, company_pk):
        """Синхронизация источника: {"source": "bank" | "registry"}; {"action": "test"} — проверка ключей."""
        company = get_company_for_user(request.user, company_pk)
        source = request.data.get("source")
        as_of = _as_of(request, company)
        if request.data.get("action") == "test":
            kind = {"registry": "dadata"}.get(source, source)
            if kind not in CONFIG_FIELDS:
                return Response({"detail": "У этого источника нет ключей"}, status=400)
            return Response({"source": source, **ping(connector(company, kind))})
        try:
            if source == "bank":
                return Response({"source": source, **services.sync_bank(company, as_of, int(request.data.get("horizon", settings.FORECAST_DEFAULT_HORIZON)))})
            if source == "registry":
                return Response({"source": source, "mode": "api" if connector(company, "dadata").configured else "mock", "updated_invoices": services.apply_registry_to_invoices(company)})
        except ConnectorError as e:
            return Response({"detail": str(e)}, status=502)
        return Response({"detail": "Неизвестный источник"}, status=400)


class IntegrationConfigView(APIView):
    """Ключи внешнего API по компании: PUT {"config": {...}} сохраняет, DELETE удаляет."""

    def put(self, request, company_pk, kind):
        company = get_company_for_user(request.user, company_pk)
        if kind not in CONFIG_FIELDS:
            return Response({"detail": "Неизвестный коннектор"}, status=400)
        allowed = {n for n, _ in CONFIG_FIELDS[kind]}
        incoming = {k: str(v).strip() for k, v in (request.data.get("config") or {}).items() if k in allowed}
        cfg, _ = IntegrationConfig.objects.get_or_create(company=company, kind=kind)
        # пустое или замаскированное значение не затирает сохранённый ключ
        cfg.config = {**cfg.config, **{k: v for k, v in incoming.items() if v and not v.startswith("…")}}
        cfg.enabled = True
        cfg.save()
        return Response({"kind": kind, "configured": connector(company, kind).configured, "values": _masked(cfg)})

    def delete(self, request, company_pk, kind):
        company = get_company_for_user(request.user, company_pk)
        IntegrationConfig.objects.filter(company=company, kind=kind).delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class ImportInvoicesView(APIView):
    """Счета из 1С (CommerceML, CSV/XLSX) или ЭДО (УПД XML): multipart file, direction=auto|in|out, deferral_days."""

    def post(self, request, company_pk):
        company = get_company_for_user(request.user, company_pk)
        f = request.FILES.get("file")
        if not f:
            return Response({"detail": "Нет файла"}, status=400)
        try:
            report = importers.import_invoices(company, f.name, f.read(), direction=request.data.get("direction", "auto"), deferral_days=int(request.data.get("deferral_days", 14) or 14), user=request.user)
        except (ValueError, ET.ParseError) as e:
            return Response({"detail": f"Файл не разобран: {e}"}, status=400)
        return Response(report)


class ImportOfdView(APIView):
    """Отчёт ОФД (CSV/XLSX): multipart file, fee_pct — комиссия эквайринга в процентах."""

    def post(self, request, company_pk):
        company = get_company_for_user(request.user, company_pk)
        f = request.FILES.get("file")
        if not f:
            return Response({"detail": "Нет файла"}, status=400)
        try:
            report = importers.import_ofd(company, f.name, f.read(), fee_pct=float(request.data.get("fee_pct", 0) or 0), user=request.user)
        except ValueError as e:
            return Response({"detail": f"Файл не разобран: {e}"}, status=400)
        return Response(report)


class CounterpartyCheckView(APIView):
    def get(self, request):
        inn = request.query_params.get("inn", "")
        name = request.query_params.get("name", "")
        company_pk = request.query_params.get("company")
        if company_pk:
            return Response(services.check_counterparty(get_company_for_user(request.user, company_pk), inn, name))
        return Response(RegistryAdapter().check(inn, name))


class PlanFactStatusView(APIView):
    def post(self, request, company_pk):
        company = get_company_for_user(request.user, company_pk)
        return Response(services.refresh_event_statuses(company))
