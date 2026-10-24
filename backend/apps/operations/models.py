from django.db import models

from cashflow_core.categories import CATEGORIES

CATEGORY_CHOICES = [(c.code, c.label) for c in CATEGORIES]


class Operation(models.Model):
    DIR_IN = "Поступление"
    DIR_OUT = "Списание"

    company = models.ForeignKey("companies.Company", on_delete=models.CASCADE, related_name="operations")
    operation_id = models.CharField(max_length=64)
    operation_date = models.DateField(db_index=True)
    amount = models.DecimalField(max_digits=18, decimal_places=2)
    direction = models.CharField(max_length=16)
    counterparty_inn = models.CharField(max_length=12, blank=True)
    counterparty_name = models.CharField(max_length=255, blank=True)
    payment_purpose = models.TextField(blank=True)
    balance_eod = models.DecimalField(max_digits=18, decimal_places=2)
    # результат категоризации
    category = models.CharField(max_length=32, choices=CATEGORY_CHOICES, db_index=True)
    category_confidence = models.FloatField(default=0.0)
    category_source = models.CharField(max_length=16, default="fallback")  # rule | counterparty | ml | fallback | manual
    source = models.CharField(max_length=32, default="statement")  # statement | 1c | manual
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("company", "operation_date", "operation_id")
        indexes = [models.Index(fields=["company", "operation_date"])]
        ordering = ["operation_date", "operation_id"]

    def __str__(self) -> str:  # без чувствительных полей
        return f"{self.company_id}:{self.operation_id}"


class IngestLog(models.Model):
    """История загрузок выписок и отчёты о качестве данных."""

    company = models.ForeignKey("companies.Company", on_delete=models.CASCADE, related_name="ingest_logs")
    created_at = models.DateTimeField(auto_now_add=True)
    source = models.CharField(max_length=32, default="statement")
    rows_total = models.IntegerField(default=0)
    rows_new = models.IntegerField(default=0)
    report = models.JSONField(default=dict)

    class Meta:
        ordering = ["-created_at"]
