from django.db import models


class ForecastRun(models.Model):
    """Снапшот прогноза: нужен для план-факта и инкрементального обновления."""

    company = models.ForeignKey("companies.Company", on_delete=models.CASCADE, related_name="forecast_runs")
    as_of = models.DateField(db_index=True)
    horizon_days = models.IntegerField()
    model_name = models.CharField(max_length=32)
    is_baseline = models.BooleanField(default=True)  # False — сценарий «что если»
    scenario = models.JSONField(default=dict)
    min_balance = models.DecimalField(max_digits=16, decimal_places=2, default=0)
    opening_balance = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    components = models.JSONField(default=dict)
    calculation_log = models.JSONField(default=list)
    data_quality = models.JSONField(default=dict)
    risk = models.JSONField(default=dict)
    events = models.JSONField(default=list)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["company", "as_of", "is_baseline"])]


class ForecastPoint(models.Model):
    run = models.ForeignKey(ForecastRun, on_delete=models.CASCADE, related_name="points")
    date = models.DateField()
    p10 = models.FloatField()
    p50 = models.FloatField()
    p90 = models.FloatField()
    mean = models.FloatField()
    prob_below = models.FloatField()
    det_in = models.FloatField(default=0)
    det_out = models.FloatField(default=0)
    stoch_in = models.FloatField(default=0)
    stoch_out = models.FloatField(default=0)

    class Meta:
        ordering = ["date"]
        unique_together = ("run", "date")


class BacktestResult(models.Model):
    name = models.CharField(max_length=64)
    created_at = models.DateTimeField(auto_now_add=True)
    config = models.JSONField(default=dict)
    summary = models.JSONField(default=dict)
    rows = models.JSONField(default=list)
    categorizer_metrics = models.JSONField(default=dict)

    class Meta:
        ordering = ["-created_at"]
