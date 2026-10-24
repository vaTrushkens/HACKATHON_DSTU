from django.urls import path

from .views import CategoryListView, CategorySummaryView, OperationViewSet

ops_list = OperationViewSet.as_view({"get": "list"})
ops_detail = OperationViewSet.as_view({"get": "retrieve", "patch": "partial_update"})

urlpatterns = [
    path("categories/", CategoryListView.as_view()),
    path("companies/<int:company_pk>/operations/", ops_list),
    path("companies/<int:company_pk>/operations/<int:pk>/", ops_detail),
    path("companies/<int:company_pk>/categories/summary/", CategorySummaryView.as_view()),
]
