import pandas as pd
from django.conf import settings
from django.db.models import Count
from rest_framework import mixins, status, viewsets
from rest_framework.response import Response
from rest_framework.views import APIView

from core.permissions import get_company_for_user

from . import services
from .integrations import ADAPTERS, RegistryAdapter
from .models import Contract, PlannedEvent, RecurringPattern
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


class IntegrationsView(APIView):
    def get(self, request, company_pk):
        company = get_company_for_user(request.user, company_pk)
        counts = dict(PlannedEvent.objects.filter(company=company).exclude(status="cancelled").values_list("source").annotate(n=Count("id")))
        return Response([{"source": k, **v, "events": counts.get(k, 0)} for k, v in ADAPTERS.items()])

    def post(self, request, company_pk):
        """Синхронизация источника: {"source": "bank" | "registry"}."""
        company = get_company_for_user(request.user, company_pk)
        source = request.data.get("source")
        as_of = _as_of(request, company)
        if source == "bank":
            n = services.sync_bank(company, as_of, int(request.data.get("horizon", settings.FORECAST_DEFAULT_HORIZON)))
            return Response({"source": source, "events": n})
        if source == "registry":
            n = services.apply_registry_to_invoices(company)
            return Response({"source": source, "updated_invoices": n})
        return Response({"detail": "Неизвестный источник"}, status=400)


class CounterpartyCheckView(APIView):
    def get(self, request):
        inn = request.query_params.get("inn", "")
        return Response(RegistryAdapter().check(inn, request.query_params.get("name", "")))


class PlanFactStatusView(APIView):
    def post(self, request, company_pk):
        company = get_company_for_user(request.user, company_pk)
        return Response(services.refresh_event_statuses(company))
