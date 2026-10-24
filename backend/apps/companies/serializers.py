from rest_framework import serializers

from .models import Company


class CompanySerializer(serializers.ModelSerializer):
    as_of = serializers.DateField(read_only=True)

    class Meta:
        model = Company
        fields = (
            "id", "code", "name", "inn", "legal_form", "industry",
            "first_operation_date", "last_operation_date", "operations_count",
            "min_balance", "as_of_override", "as_of", "tax_regime",
        )
        read_only_fields = ("code", "inn", "first_operation_date", "last_operation_date", "operations_count", "tax_regime")
