from rest_framework import serializers

from cashflow_core.categories import LABELS

from .models import Contract, PlannedEvent, RecurringPattern


class ContractSerializer(serializers.ModelSerializer):
    events = serializers.SerializerMethodField()
    schedule_label = serializers.CharField(source="get_schedule_type_display", read_only=True)

    class Meta:
        model = Contract
        fields = ("id", "number", "counterparty_inn", "counterparty_name", "direction", "total_amount", "start_date", "end_date", "schedule_type", "schedule_label", "prepayment_pct", "deferral_days", "installments", "day_of_month", "milestones", "category", "note", "active", "created_at", "events")
        read_only_fields = ("created_at", "active")

    def get_events(self, obj):
        qs = PlannedEvent.objects.filter(company=obj.company, external_ref=obj.ref).exclude(status="cancelled").order_by("date")
        return [{"id": e.id, "date": e.date, "amount": float(e.amount), "label": e.label, "status": e.status, "probability": e.probability, "timing_std_days": e.timing_std_days} for e in qs]

    def validate(self, attrs):
        if attrs.get("total_amount") is not None and attrs["total_amount"] <= 0:
            raise serializers.ValidationError({"total_amount": "Сумма должна быть положительной"})
        st = attrs.get("schedule_type", getattr(self.instance, "schedule_type", "prepay_final"))
        if st in ("prepay_final", "postpay") and not attrs.get("end_date", getattr(self.instance, "end_date", None)):
            raise serializers.ValidationError({"end_date": "Для этого графика нужна дата закрытия (акта/поставки)"})
        if st == "equal" and attrs.get("installments", 1) < 1:
            raise serializers.ValidationError({"installments": "Число платежей ≥ 1"})
        if st == "milestones" and not attrs.get("milestones"):
            raise serializers.ValidationError({"milestones": "Укажите этапы"})
        return attrs


class RecurringPatternSerializer(serializers.ModelSerializer):
    category_label = serializers.SerializerMethodField()

    class Meta:
        model = RecurringPattern
        fields = ("id", "key", "category", "category_label", "direction", "counterparty_name", "counterparty_inn", "subtype", "label", "period", "day_of_month", "weekday", "amount", "amount_cv", "occurrences", "first_date", "last_date", "confidence", "active", "user_disabled", "history")
        read_only_fields = tuple(f for f in fields if f != "user_disabled")

    def get_category_label(self, obj):
        return LABELS.get(obj.category, obj.category)


class PlannedEventSerializer(serializers.ModelSerializer):
    category_label = serializers.SerializerMethodField()
    kind_label = serializers.CharField(source="get_kind_display", read_only=True)

    class Meta:
        model = PlannedEvent
        fields = ("id", "kind", "kind_label", "direction", "amount", "date", "issued_date", "due_date", "counterparty_inn", "counterparty_name", "category", "category_label", "label", "source", "probability", "timing_std_days", "status", "matched_date", "note", "external_ref", "meta", "created_at")
        read_only_fields = ("status", "matched_date", "created_at")

    def get_category_label(self, obj):
        return LABELS.get(obj.category, obj.category)

    def validate(self, attrs):
        if attrs.get("amount") is not None and attrs["amount"] <= 0:
            raise serializers.ValidationError({"amount": "Сумма должна быть положительной"})
        if attrs.get("category") and attrs["category"] not in LABELS:
            raise serializers.ValidationError({"category": "Неизвестная категория"})
        p = attrs.get("probability")
        if p is not None and not 0 < p <= 1:
            raise serializers.ValidationError({"probability": "Вероятность в диапазоне (0, 1]"})
        return attrs
