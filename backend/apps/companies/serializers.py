from rest_framework import serializers
import uuid

from .models import Company


class CompanySerializer(serializers.ModelSerializer):
    as_of = serializers.DateField(read_only=True)
    code = serializers.CharField(max_length=16, required=False, allow_blank=True)

    class Meta:
        model = Company
        fields = (
            "id", "code", "name", "inn", "legal_form", "industry",
            "first_operation_date", "last_operation_date", "operations_count",
            "min_balance", "as_of_override", "as_of", "tax_regime", "external_code", "dataset_id",
        )
        read_only_fields = ("first_operation_date", "last_operation_date", "operations_count", "tax_regime", "external_code", "dataset_id")

    def validate_code(self, value):
        value = value.strip() or (self.instance.code if self.instance else "N" + uuid.uuid4().hex[:15])
        if self.instance and value != self.instance.code:
            raise serializers.ValidationError("Идентификатор существующей компании менять нельзя")
        if Company.objects.filter(code=value).exclude(pk=self.instance.pk if self.instance else None).exists():
            raise serializers.ValidationError("Идентификатор уже занят")
        return value

    def create(self, validated_data):
        validated_data.setdefault("code", "N" + uuid.uuid4().hex[:15])
        return super().create(validated_data)
