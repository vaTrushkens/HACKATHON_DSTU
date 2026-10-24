from django.db import models


class Company(models.Model):
    code = models.CharField("Идентификатор", max_length=16, unique=True)  # C001…
    name = models.CharField("Название", max_length=255)
    inn = models.CharField("ИНН", max_length=12, blank=True)
    legal_form = models.CharField("Форма", max_length=16, blank=True)  # ООО / ИП
    industry = models.CharField("Вид деятельности", max_length=128, blank=True)
    first_operation_date = models.DateField(null=True, blank=True)
    last_operation_date = models.DateField(null=True, blank=True)
    operations_count = models.IntegerField(default=0)
    # настройки прогноза
    min_balance = models.DecimalField("Неснижаемый остаток", max_digits=16, decimal_places=2, default=0)
    as_of_override = models.DateField("Дата «сегодня» для демо", null=True, blank=True)
    tax_regime = models.CharField(max_length=128, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["code"]
        verbose_name = "Компания"
        verbose_name_plural = "Компании"

    def __str__(self) -> str:
        return f"{self.code} {self.name}"

    @property
    def as_of(self):
        return self.as_of_override or self.last_operation_date
