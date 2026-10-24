from rest_framework import serializers

from cashflow_core.categories import LABELS, BY_CODE

from .models import Operation


class OperationSerializer(serializers.ModelSerializer):
    category_label = serializers.SerializerMethodField()

    class Meta:
        model = Operation
        fields = ("id", "operation_id", "operation_date", "amount", "direction", "counterparty_inn", "counterparty_name", "payment_purpose", "balance_eod", "category", "category_label", "category_confidence", "category_source", "source")
        read_only_fields = tuple(f for f in fields if f != "category")

    def get_category_label(self, obj):
        return LABELS.get(obj.category, obj.category)

    def validate_category(self, value):
        if self.instance and BY_CODE[value].direction != ("in" if self.instance.direction == "Поступление" else "out"):
            raise serializers.ValidationError("Категория не соответствует направлению операции")
        return value
