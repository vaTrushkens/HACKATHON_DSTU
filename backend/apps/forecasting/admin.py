from django.contrib import admin

from .models import BacktestResult, ForecastRun


@admin.register(ForecastRun)
class ForecastRunAdmin(admin.ModelAdmin):
    list_display = ("company", "as_of", "horizon_days", "model_name", "is_baseline", "created_at")
    list_filter = ("model_name", "is_baseline")


admin.site.register(BacktestResult)
