from django.contrib import admin

from .models import IngestLog, Operation


@admin.register(Operation)
class OperationAdmin(admin.ModelAdmin):
    list_display = ("company", "operation_id", "operation_date", "direction", "category", "category_source")
    list_filter = ("category", "direction", "category_source")
    search_fields = ("operation_id",)


admin.site.register(IngestLog)
