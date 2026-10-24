from django.contrib import admin

from .models import Company


@admin.register(Company)
class CompanyAdmin(admin.ModelAdmin):
    list_display = ("code", "name", "legal_form", "industry", "last_operation_date", "operations_count")
    search_fields = ("code", "name")
    list_filter = ("industry", "legal_form")
