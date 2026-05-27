from django.contrib import admin

from .models import (
    AuditEvent,
    EventSeries,
    EventSeriesVersion,
    FinancialEvent,
    InterestAccrualRun,
    InterestRatePeriod,
    LedgerAccount,
    LedgerEntry,
    LedgerTransaction,
    Obligation,
    Person,
)


@admin.register(Person)
class PersonAdmin(admin.ModelAdmin):
    list_display = ('user', 'display_name', 'email', 'active', 'created_at')
    search_fields = ('user__username', 'user__email', 'user__first_name', 'user__last_name')
    list_filter = ('active',)


class LedgerAccountInline(admin.TabularInline):
    model = LedgerAccount
    extra = 0


@admin.register(Obligation)
class ObligationAdmin(admin.ModelAdmin):
    list_display = ('title', 'borrower', 'creditor', 'status', 'opened_on', 'currency')
    list_filter = ('status', 'currency', 'category')
    search_fields = ('title', 'borrower__user__username', 'creditor__user__username')
    inlines = [LedgerAccountInline]


@admin.register(LedgerAccount)
class LedgerAccountAdmin(admin.ModelAdmin):
    list_display = ('name', 'obligation', 'person', 'account_type', 'active')
    list_filter = ('account_type', 'active')
    search_fields = ('name', 'person__user__username', 'obligation__title')


class LedgerEntryInline(admin.TabularInline):
    model = LedgerEntry
    extra = 0
    readonly_fields = ('created_at', 'updated_at')


@admin.register(LedgerTransaction)
class LedgerTransactionAdmin(admin.ModelAdmin):
    list_display = ('transaction_type', 'obligation', 'status', 'transaction_date', 'idempotency_key')
    list_filter = ('transaction_type', 'status')
    search_fields = ('obligation__title', 'idempotency_key', 'memo')
    inlines = [LedgerEntryInline]


@admin.register(LedgerEntry)
class LedgerEntryAdmin(admin.ModelAdmin):
    list_display = ('transaction', 'account', 'side', 'amount_units', 'effective_date')
    list_filter = ('side', 'entry_type')
    search_fields = ('account__name', 'transaction__memo', 'memo')


@admin.register(FinancialEvent)
class FinancialEventAdmin(admin.ModelAdmin):
    list_display = ('event_type', 'obligation', 'event_date', 'amount_units', 'source', 'direction')
    list_filter = ('event_type', 'source', 'direction')
    search_fields = ('obligation__title', 'memo', 'category')


@admin.register(EventSeries)
class EventSeriesAdmin(admin.ModelAdmin):
    list_display = ('name', 'obligation', 'frequency', 'day_of_month', 'active', 'starts_on', 'ends_on')
    list_filter = ('frequency', 'active', 'auto_post')
    search_fields = ('name', 'obligation__title')


@admin.register(EventSeriesVersion)
class EventSeriesVersionAdmin(admin.ModelAdmin):
    list_display = ('event_series', 'amount_units', 'valid_from', 'valid_to')
    search_fields = ('event_series__name', 'memo')


@admin.register(InterestRatePeriod)
class InterestRatePeriodAdmin(admin.ModelAdmin):
    list_display = ('obligation', 'annual_rate_percent', 'effective_from', 'effective_to')
    search_fields = ('obligation__title', 'memo')


@admin.register(InterestAccrualRun)
class InterestAccrualRunAdmin(admin.ModelAdmin):
    list_display = ('obligation', 'period_start', 'period_end', 'calculated_interest_amount_units', 'status')
    list_filter = ('status',)
    search_fields = ('obligation__title',)


@admin.register(AuditEvent)
class AuditEventAdmin(admin.ModelAdmin):
    list_display = ('event_type', 'obligation', 'financial_event', 'created_at')
    list_filter = ('event_type',)
    search_fields = ('event_type', 'obligation__title')
