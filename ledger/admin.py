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
    ObligationCategory,
    TelegramLoginChallenge,
    UserProfile,
)


class NoRelatedObjectLinksMixin:
    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        formfield = super().formfield_for_foreignkey(db_field, request, **kwargs)
        widget = getattr(formfield, 'widget', None)
        for permission_name in (
            'can_add_related',
            'can_change_related',
            'can_delete_related',
            'can_view_related',
        ):
            if hasattr(widget, permission_name):
                setattr(widget, permission_name, False)
        return formfield


class InternalLedgerAdminMixin(admin.ModelAdmin):
    def has_module_permission(self, request):
        return False

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def get_readonly_fields(self, request, obj=None):
        field_names = [field.name for field in self.model._meta.fields]
        return tuple(dict.fromkeys((*super().get_readonly_fields(request, obj), *field_names)))


@admin.register(ObligationCategory)
class ObligationCategoryAdmin(admin.ModelAdmin):
    list_display = ('name', 'active')
    list_filter = ('active',)
    search_fields = ('name',)


@admin.register(UserProfile)
class UserProfileAdmin(admin.ModelAdmin):
    list_display = ('user', 'telegram_id', 'telegram_username', 'telegram_language', 'telegram_checked_at')
    search_fields = (
        'user__username',
        'user__first_name',
        'user__last_name',
        '=telegram_id',
        'telegram_username',
    )


@admin.register(TelegramLoginChallenge)
class TelegramLoginChallengeAdmin(InternalLedgerAdminMixin):
    list_display = ('code', 'user', 'telegram_id', 'expires_at', 'confirmed_at', 'consumed_at')
    search_fields = ('code', 'token', 'user__username', '=telegram_id')


@admin.register(Obligation)
class ObligationAdmin(NoRelatedObjectLinksMixin, admin.ModelAdmin):
    list_display = ('title', 'borrower', 'creditor', 'category', 'status', 'opened_on')
    list_filter = ('status', 'category')
    search_fields = ('title', 'borrower__username', 'creditor__username', 'category__name')
    fieldsets = (
        ('People', {
            'fields': ('creditor', 'borrower'),
            'description': 'Creditor is the person who is owed money. Borrower is the person who owes money.',
        }),
        ('Obligation', {
            'fields': ('title', 'category', 'opened_on', 'closed_on', 'status', 'notes'),
        }),
    )


@admin.register(LedgerAccount)
class LedgerAccountAdmin(InternalLedgerAdminMixin):
    list_display = ('name', 'obligation', 'user', 'account_type', 'active')
    list_filter = ('account_type', 'active')
    search_fields = ('name', 'user__username', 'obligation__title')


class LedgerEntryInline(admin.TabularInline):
    model = LedgerEntry
    extra = 0
    can_delete = False
    readonly_fields = (
        'account',
        'side',
        'amount_units',
        'currency',
        'currency_exponent',
        'entry_type',
        'effective_date',
        'memo',
        'created_at',
        'updated_at',
    )

    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(LedgerTransaction)
class LedgerTransactionAdmin(InternalLedgerAdminMixin):
    list_display = ('transaction_type', 'obligation', 'status', 'transaction_date', 'idempotency_key')
    list_filter = ('transaction_type', 'status')
    search_fields = ('obligation__title', 'idempotency_key', 'memo')
    inlines = [LedgerEntryInline]


@admin.register(LedgerEntry)
class LedgerEntryAdmin(InternalLedgerAdminMixin):
    list_display = ('transaction', 'account', 'side', 'amount_units', 'effective_date')
    list_filter = ('side', 'entry_type')
    search_fields = ('account__name', 'transaction__memo', 'memo')


@admin.register(FinancialEvent)
class FinancialEventAdmin(InternalLedgerAdminMixin):
    list_display = ('event_type', 'obligation', 'event_date', 'amount_units', 'source', 'direction')
    list_filter = ('event_type', 'source', 'direction')
    search_fields = ('obligation__title', 'memo', 'category')


@admin.register(EventSeries)
class EventSeriesAdmin(admin.ModelAdmin):
    list_display = ('name', 'obligation', 'event_type', 'frequency', 'day_of_month', 'active', 'starts_on', 'ends_on')
    list_filter = ('event_type', 'frequency', 'active', 'auto_post')
    search_fields = ('name', 'obligation__title')


@admin.register(EventSeriesVersion)
class EventSeriesVersionAdmin(InternalLedgerAdminMixin):
    list_display = ('event_series', 'amount_units', 'valid_from', 'valid_to')
    search_fields = ('event_series__name', 'memo')


@admin.register(InterestRatePeriod)
class InterestRatePeriodAdmin(admin.ModelAdmin):
    list_display = ('obligation', 'annual_rate_percent', 'effective_from', 'effective_to')
    search_fields = ('obligation__title', 'memo')


@admin.register(InterestAccrualRun)
class InterestAccrualRunAdmin(InternalLedgerAdminMixin):
    list_display = ('obligation', 'period_start', 'period_end', 'revision', 'calculated_interest_amount_units', 'status')
    list_filter = ('status',)
    search_fields = ('obligation__title',)


@admin.register(AuditEvent)
class AuditEventAdmin(InternalLedgerAdminMixin):
    list_display = ('event_type', 'obligation', 'financial_event', 'created_at')
    list_filter = ('event_type',)
    search_fields = ('event_type', 'obligation__title')
