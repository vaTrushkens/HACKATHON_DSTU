from django.contrib import admin

from .models import CounterpartyCheck, PlannedEvent, RecurringPattern


@admin.register(RecurringPattern)
class RecurringPatternAdmin(admin.ModelAdmin):
    list_display = ("company", "label", "period", "day_of_month", "amount", "confidence", "active")
    list_filter = ("period", "active", "category")


@admin.register(PlannedEvent)
class PlannedEventAdmin(admin.ModelAdmin):
    list_display = ("company", "date", "kind", "direction", "amount", "status", "source")
    list_filter = ("kind", "status", "source")


admin.site.register(CounterpartyCheck)
