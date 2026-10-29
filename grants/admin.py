from django.contrib import admin

from .models import (
    Budget,
    BudgetLineItem,
    Funder,
    FunderProgram,
    Grant,
    GrantSection,
    OutcomeReport,
    SectionVersion,
)


class FunderProgramInline(admin.TabularInline):
    model = FunderProgram
    extra = 0


class FunderAdmin(admin.ModelAdmin):
    list_display = ["name", "funder_type", "website"]
    search_fields = ["name", "focus_areas"]
    inlines = [FunderProgramInline]


class BudgetLineItemInline(admin.TabularInline):
    model = BudgetLineItem
    extra = 0


class BudgetAdmin(admin.ModelAdmin):
    list_display = ["grant", "indirect_cost_rate", "indirect_cost_base"]
    inlines = [BudgetLineItemInline]


class GrantSectionInline(admin.TabularInline):
    model = GrantSection
    extra = 0


class GrantAdmin(admin.ModelAdmin):
    list_display = ["title", "organization", "status", "deadline", "amount_requested"]
    list_filter = ["status"]
    search_fields = ["title", "organization__name"]
    inlines = [GrantSectionInline]


admin.site.register(Funder, FunderAdmin)
admin.site.register(FunderProgram)
admin.site.register(Grant, GrantAdmin)
admin.site.register(GrantSection)
admin.site.register(SectionVersion)
admin.site.register(Budget, BudgetAdmin)
admin.site.register(BudgetLineItem)
admin.site.register(OutcomeReport)
