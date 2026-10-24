from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from rest_framework import serializers

User = get_user_model()


class UserSerializer(serializers.ModelSerializer):
    companies = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = ("id", "username", "email", "first_name", "last_name", "is_superuser", "companies")

    def get_companies(self, obj):
        return [{"id": m.company_id, "code": m.company.code, "name": m.company.name, "role": m.role} for m in obj.memberships.select_related("company")]


class RegisterSerializer(serializers.Serializer):
    username = serializers.CharField(max_length=150)
    email = serializers.EmailField(required=False, allow_blank=True)
    password = serializers.CharField(write_only=True)

    def validate_username(self, v):
        if User.objects.filter(username=v).exists():
            raise serializers.ValidationError("Пользователь с таким именем уже существует")
        return v

    def validate_password(self, v):
        validate_password(v)
        return v

    def create(self, validated):
        return User.objects.create_user(**validated)
