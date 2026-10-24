from django.urls import path

from .views import BacktestView, ForecastView, PlanFactView, RunsView, ScenarioView

urlpatterns = [
    path("companies/<int:company_pk>/forecast/", ForecastView.as_view()),
    path("companies/<int:company_pk>/scenario/", ScenarioView.as_view()),
    path("companies/<int:company_pk>/plan-fact/", PlanFactView.as_view()),
    path("companies/<int:company_pk>/forecast-runs/", RunsView.as_view()),
    path("backtest/", BacktestView.as_view()),
]
