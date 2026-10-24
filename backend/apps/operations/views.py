import pandas as pd
from django.db.models import Q
from django_filters import rest_framework as filters
from rest_framework import mixins, viewsets
from rest_framework.response import Response
from rest_framework.views import APIView

from cashflow_core.categories import CATEGORIES
from core.permissions import get_company_for_user

from . import services
from .models import Operation
from .serializers import OperationSerializer


class OperationFilter(filters.FilterSet):
    date_from = filters.DateFilter(field_name="operation_date", lookup_expr="gte")
    date_to = filters.DateFilter(field_name="operation_date", lookup_expr="lte")
    search = filters.CharFilter(method="filter_search")

    class Meta:
        model = Operation
        fields = ["category", "direction", "category_source"]

    def filter_search(self, qs, name, value):
        return qs.filter(Q(counterparty_name__icontains=value) | Q(payment_purpose__icontains=value) | Q(counterparty_inn__startswith=value))


class OperationViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, mixins.UpdateModelMixin, viewsets.GenericViewSet):
    serializer_class = OperationSerializer
    filterset_class = OperationFilter
    http_method_names = ["get", "patch", "head", "options"]

    def get_queryset(self):
        company = get_company_for_user(self.request.user, self.kwargs["company_pk"])
        return Operation.objects.filter(company=company).order_by("-operation_date", "-operation_id")

    def perform_update(self, serializer):
        # ручная правка категории — единственное изменяемое поле
        services.set_manual_category(serializer.instance, serializer.validated_data["category"])


class CategorySummaryView(APIView):
    def get(self, request, company_pk):
        company = get_company_for_user(request.user, company_pk)
        months = int(request.query_params.get("months", 3))
        as_of = pd.Timestamp(request.query_params.get("as_of") or company.as_of or pd.Timestamp.today())
        return Response(services.category_summary(company, as_of, months))


class CategoryListView(APIView):
    def get(self, request):
        return Response([{"code": c.code, "label": c.label, "direction": c.direction, "kind": c.kind, "mandatory": c.mandatory} for c in CATEGORIES])
