from django.conf import settings
from django.db import models


class RecurringPattern(models.Model):
    """Регулярный платёж/поступление, выявленный из выписки."""

    company = models.ForeignKey("companies.Company", on_delete=models.CASCADE, related_name="recurring_patterns")
    key = models.CharField(max_length=255)
    category = models.CharField(max_length=32)
    direction = models.CharField(max_length=16)
    counterparty_name = models.CharField(max_length=255, blank=True)
    counterparty_inn = models.CharField(max_length=12, blank=True)
    subtype = models.CharField(max_length=16, blank=True)
    label = models.CharField(max_length=255)
    period = models.CharField(max_length=16)
    day_of_month = models.IntegerField(null=True, blank=True)
    weekday = models.IntegerField(null=True, blank=True)
    amount = models.DecimalField(max_digits=18, decimal_places=2)
    amount_cv = models.FloatField(default=0)
    occurrences = models.IntegerField(default=0)
    first_date = models.DateField()
    last_date = models.DateField()
    confidence = models.FloatField(default=0)
    active = models.BooleanField(default=True)
    user_disabled = models.BooleanField(default=False)  # пользователь отключил («этого платежа больше не будет»)
    history = models.JSONField(default=list)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ("company", "key")
        ordering = ["-amount"]


class PlannedEvent(models.Model):
    """Ожидаемое будущее событие, которого ещё нет в выписке: счёт, договор, налог, ручной ввод, данные банка."""

    KINDS = [
        ("invoice_in", "Выставленный счёт клиенту"),
        ("invoice_out", "Счёт от поставщика"),
        ("contract", "Платёж по договору"),
        ("manual", "Ручной ввод"),
        ("bank_credit", "График кредита (банк)"),
        ("bank_payroll", "Зарплатный проект (банк)"),
        ("bank_order", "Платёжное поручение (не исполнено)"),
        ("tax", "Налог / взнос"),
    ]
    STATUSES = [("expected", "Ожидается"), ("matched", "Исполнено"), ("overdue", "Просрочено"), ("cancelled", "Отменено")]

    company = models.ForeignKey("companies.Company", on_delete=models.CASCADE, related_name="planned_events")
    kind = models.CharField(max_length=16, choices=KINDS, default="manual")
    direction = models.CharField(max_length=16)
    amount = models.DecimalField(max_digits=18, decimal_places=2)
    date = models.DateField("Ожидаемая дата")
    issued_date = models.DateField(null=True, blank=True)
    due_date = models.DateField(null=True, blank=True)
    counterparty_inn = models.CharField(max_length=12, blank=True)
    counterparty_name = models.CharField(max_length=255, blank=True)
    category = models.CharField(max_length=32, default="OTHER_OUT")
    label = models.CharField(max_length=255)
    source = models.CharField(max_length=32, default="manual")  # manual | invoice:1c | contract | bank | registry | tax
    probability = models.FloatField(default=1.0)
    timing_std_days = models.FloatField(default=0.0)
    status = models.CharField(max_length=16, choices=STATUSES, default="expected")
    matched_operation = models.ForeignKey("operations.Operation", null=True, blank=True, on_delete=models.SET_NULL)
    matched_date = models.DateField(null=True, blank=True)
    note = models.TextField(blank=True)
    external_ref = models.CharField(max_length=128, blank=True)
    meta = models.JSONField(default=dict, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["date"]
        indexes = [models.Index(fields=["company", "date"])]


class Contract(models.Model):
    """Договор с клиентом или поставщиком: условия → график ожидаемых платежей (PlannedEvent)."""

    SCHEDULES = [("prepay_final", "Предоплата + окончательный расчёт"), ("equal", "Равные платежи ежемесячно"), ("postpay", "Оплата после закрытия с отсрочкой"), ("milestones", "По этапам")]

    company = models.ForeignKey("companies.Company", on_delete=models.CASCADE, related_name="contracts")
    number = models.CharField(max_length=64, blank=True)
    counterparty_inn = models.CharField(max_length=12, blank=True)
    counterparty_name = models.CharField(max_length=255)
    direction = models.CharField(max_length=16)  # Поступление (клиент) | Списание (поставщик)
    total_amount = models.DecimalField(max_digits=18, decimal_places=2)
    start_date = models.DateField()
    end_date = models.DateField(null=True, blank=True)
    schedule_type = models.CharField(max_length=16, choices=SCHEDULES, default="prepay_final")
    prepayment_pct = models.FloatField(default=30.0)
    deferral_days = models.IntegerField(default=0)
    installments = models.IntegerField(default=1)
    day_of_month = models.IntegerField(null=True, blank=True)
    milestones = models.JSONField(default=list, blank=True)
    category = models.CharField(max_length=32, default="REV_CLIENTS")
    note = models.TextField(blank=True)
    active = models.BooleanField(default=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    @property
    def ref(self) -> str:
        return f"contract:{self.id}"


class CounterpartyCheck(models.Model):
    """Результат проверки контрагента по открытым реестрам (ФНС, ЕФРСБ) — кэш."""

    inn = models.CharField(max_length=12, unique=True)
    name = models.CharField(max_length=255, blank=True)
    status = models.CharField(max_length=64)  # действует | ликвидация | банкротство | недостоверные сведения
    risk_level = models.CharField(max_length=16, default="low")
    tax_debt = models.BooleanField(default=False)
    details = models.JSONField(default=dict)
    checked_at = models.DateTimeField(auto_now=True)
    provider = models.CharField(max_length=32, default="mock")
