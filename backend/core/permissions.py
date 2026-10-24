"""Доступ к компании: пользователь видит только компании, где он участник."""
from __future__ import annotations

from django.shortcuts import get_object_or_404
from rest_framework.exceptions import PermissionDenied

from apps.companies.models import Company


def get_company_for_user(user, company_pk) -> Company:
    company = get_object_or_404(Company, pk=company_pk)
    if user.is_superuser or company.memberships.filter(user=user).exists():
        return company
    raise PermissionDenied("Нет доступа к этой компании")


def companies_for_user(user):
    qs = Company.objects.all()
    if user.is_superuser:
        return qs
    return qs.filter(memberships__user=user).distinct()
