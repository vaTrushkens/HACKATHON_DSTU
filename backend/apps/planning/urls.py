from django.urls import path

from .views import ContractViewSet, CounterpartiesView, CounterpartyCheckView, ImportInvoicesView, ImportOfdView, IntegrationConfigView, IntegrationsView, PlanFactStatusView, PlannedEventViewSet, RecurringViewSet, TaxCalendarView

contracts_list = ContractViewSet.as_view({"get": "list", "post": "create"})
contracts_detail = ContractViewSet.as_view({"get": "retrieve", "patch": "partial_update", "delete": "destroy"})

recurring_list = RecurringViewSet.as_view({"get": "list", "post": "create"})
recurring_detail = RecurringViewSet.as_view({"patch": "partial_update"})
events_list = PlannedEventViewSet.as_view({"get": "list", "post": "create"})
events_detail = PlannedEventViewSet.as_view({"get": "retrieve", "patch": "partial_update", "delete": "destroy"})

urlpatterns = [
    path("companies/<int:company_pk>/recurring/", recurring_list),
    path("companies/<int:company_pk>/recurring/<int:pk>/", recurring_detail),
    path("companies/<int:company_pk>/events/", events_list),
    path("companies/<int:company_pk>/events/<int:pk>/", events_detail),
    path("companies/<int:company_pk>/events/refresh-status/", PlanFactStatusView.as_view()),
    path("companies/<int:company_pk>/contracts/", contracts_list),
    path("companies/<int:company_pk>/contracts/<int:pk>/", contracts_detail),
    path("companies/<int:company_pk>/tax-calendar/", TaxCalendarView.as_view()),
    path("companies/<int:company_pk>/counterparties/", CounterpartiesView.as_view()),
    path("companies/<int:company_pk>/integrations/", IntegrationsView.as_view()),
    path("companies/<int:company_pk>/integrations/<str:kind>/config/", IntegrationConfigView.as_view()),
    path("companies/<int:company_pk>/import/invoices/", ImportInvoicesView.as_view()),
    path("companies/<int:company_pk>/import/ofd/", ImportOfdView.as_view()),
    path("registry/check/", CounterpartyCheckView.as_view()),
]
