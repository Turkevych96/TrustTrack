from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.db.models import Q
from django.utils import timezone


DEFAULT_CURRENCY = 'USD'
DEFAULT_CURRENCY_EXPONENT = 4


class TimestampedModel(models.Model):
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class ObligationCategory(TimestampedModel):
    name = models.CharField(max_length=80, unique=True)
    active = models.BooleanField(default=True)

    class Meta:
        ordering = ['name']
        verbose_name_plural = 'obligation categories'

    def __str__(self):
        return self.name


class UserProfile(TimestampedModel):
    class TelegramLanguage(models.TextChoices):
        ENGLISH = 'en', 'English'
        RUSSIAN = 'ru', 'Russian'

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='trusttrack_profile',
    )
    telegram_id = models.BigIntegerField(
        null=True,
        blank=True,
        unique=True,
        validators=[MinValueValidator(1)],
    )
    telegram_chat_type = models.CharField(max_length=32, blank=True)
    telegram_username = models.CharField(max_length=64, blank=True)
    telegram_first_name = models.CharField(max_length=255, blank=True)
    telegram_last_name = models.CharField(max_length=255, blank=True)
    telegram_title = models.CharField(max_length=255, blank=True)
    telegram_lookup_error = models.CharField(max_length=255, blank=True)
    telegram_checked_at = models.DateTimeField(null=True, blank=True)
    telegram_language = models.CharField(
        max_length=2,
        choices=TelegramLanguage.choices,
        default=TelegramLanguage.ENGLISH,
    )
    show_planner_module = models.BooleanField(default=True)
    show_dashboard_balance_history = models.BooleanField(default=True)
    payment_due_notifications = models.BooleanField(default=True)

    class Meta:
        ordering = ['user__username']

    def __str__(self):
        return f'Profile for {self.user}'

    @property
    def telegram_display_name(self):
        full_name = ' '.join(part for part in (self.telegram_first_name, self.telegram_last_name) if part)
        if self.telegram_username and full_name:
            return f'{full_name} (@{self.telegram_username})'
        if self.telegram_username:
            return f'@{self.telegram_username}'
        return full_name or self.telegram_title


class Obligation(TimestampedModel):
    class Status(models.TextChoices):
        OPEN = 'open', 'Open'
        CLOSED = 'closed', 'Closed'
        CANCELED = 'canceled', 'Canceled'

    creditor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name='credit_obligations',
    )
    borrower = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name='debt_obligations',
    )
    title = models.CharField(max_length=160)
    category = models.ForeignKey(
        ObligationCategory,
        on_delete=models.PROTECT,
        related_name='obligations',
        null=True,
        blank=True,
    )
    currency = models.CharField(max_length=3, default=DEFAULT_CURRENCY)
    currency_exponent = models.PositiveSmallIntegerField(
        default=DEFAULT_CURRENCY_EXPONENT,
    )
    opened_on = models.DateField()
    closed_on = models.DateField(null=True, blank=True)
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.OPEN,
    )
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ['-opened_on', 'title']

    def __str__(self):
        return f'{self.borrower} owes {self.creditor}: {self.title}'

    def clean(self):
        super().clean()
        if self.creditor_id and self.borrower_id and self.creditor_id == self.borrower_id:
            raise ValidationError({'borrower': 'Borrower and creditor must be different people.'})
        if self.currency != DEFAULT_CURRENCY:
            raise ValidationError({'currency': 'Only USD is supported in v1.'})
        if self.currency_exponent != DEFAULT_CURRENCY_EXPONENT:
            raise ValidationError({'currency_exponent': 'USD uses 4 ledger decimal places in v1.'})
        if self.closed_on and self.closed_on < self.opened_on:
            raise ValidationError({'closed_on': 'Closed date cannot be before opened date.'})


class LedgerAccount(TimestampedModel):
    class AccountType(models.TextChoices):
        RECEIVABLE = 'receivable', 'Receivable'
        PAYABLE = 'payable', 'Payable'

    obligation = models.ForeignKey(
        Obligation,
        on_delete=models.CASCADE,
        related_name='accounts',
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name='ledger_accounts',
    )
    account_type = models.CharField(max_length=20, choices=AccountType.choices)
    name = models.CharField(max_length=160)
    currency = models.CharField(max_length=3, default=DEFAULT_CURRENCY)
    currency_exponent = models.PositiveSmallIntegerField(
        default=DEFAULT_CURRENCY_EXPONENT,
    )
    active = models.BooleanField(default=True)

    class Meta:
        ordering = ['obligation_id', 'account_type']
        constraints = [
            models.UniqueConstraint(
                fields=['obligation', 'account_type'],
                name='unique_obligation_account_type',
            ),
        ]

    def __str__(self):
        return f'{self.name} ({self.get_account_type_display()})'

    def clean(self):
        super().clean()
        if self.obligation_id and self.user_id:
            if self.account_type == self.AccountType.RECEIVABLE and self.user_id != self.obligation.creditor_id:
                raise ValidationError({'user': 'Receivable account must belong to the creditor.'})
            if self.account_type == self.AccountType.PAYABLE and self.user_id != self.obligation.borrower_id:
                raise ValidationError({'user': 'Payable account must belong to the borrower.'})
        if self.currency != DEFAULT_CURRENCY:
            raise ValidationError({'currency': 'Only USD is supported in v1.'})
        if self.currency_exponent != DEFAULT_CURRENCY_EXPONENT:
            raise ValidationError({'currency_exponent': 'USD uses 4 ledger decimal places in v1.'})


class EventSeries(TimestampedModel):
    class Frequency(models.TextChoices):
        MONTHLY = 'monthly', 'Monthly'
        BIWEEKLY = 'biweekly', 'Every 2 weeks'
        WEEKLY = 'weekly', 'Weekly'

    obligation = models.ForeignKey(
        Obligation,
        on_delete=models.CASCADE,
        related_name='event_series',
    )
    name = models.CharField(max_length=160)
    event_type = models.CharField(max_length=40, default='scheduled_charge')
    frequency = models.CharField(
        max_length=20,
        choices=Frequency.choices,
        default=Frequency.MONTHLY,
    )
    day_of_month = models.PositiveSmallIntegerField(
        null=True,
        blank=True,
        validators=[MinValueValidator(1)],
    )
    day_of_week = models.PositiveSmallIntegerField(
        null=True,
        blank=True,
        validators=[MinValueValidator(0), MaxValueValidator(6)],
    )
    starts_on = models.DateField()
    ends_on = models.DateField(null=True, blank=True)
    active = models.BooleanField(default=True)
    auto_post = models.BooleanField(default=False)
    memo = models.TextField(blank=True)

    class Meta:
        ordering = ['name']

    def __str__(self):
        return f'{self.name} for {self.obligation}'

    def get_event_type_display(self):
        if self.event_type == FinancialEvent.EventType.SCHEDULED_CHARGE:
            return 'Scheduled charge'
        if self.event_type == FinancialEvent.EventType.REPAYMENT:
            return 'Automatic repayment'
        return self.event_type

    def clean(self):
        super().clean()
        if self.day_of_month and self.day_of_month > 31:
            raise ValidationError({'day_of_month': 'Day of month must be between 1 and 31.'})
        if self.frequency == self.Frequency.MONTHLY and not self.day_of_month:
            raise ValidationError({'day_of_month': 'Monthly schedules require a day of month.'})
        if self.frequency in (self.Frequency.WEEKLY, self.Frequency.BIWEEKLY) and self.day_of_week is None:
            raise ValidationError({'day_of_week': 'Weekly schedules require a day of week.'})
        if self.ends_on and self.ends_on < self.starts_on:
            raise ValidationError({'ends_on': 'End date cannot be before start date.'})
        allowed_event_types = (
            FinancialEvent.EventType.SCHEDULED_CHARGE,
            FinancialEvent.EventType.REPAYMENT,
        )
        if self.event_type not in allowed_event_types:
            raise ValidationError({'event_type': 'Only scheduled charges and repayments are supported in v1.'})


class EventSeriesVersion(TimestampedModel):
    event_series = models.ForeignKey(
        EventSeries,
        on_delete=models.CASCADE,
        related_name='versions',
    )
    amount_units = models.BigIntegerField(validators=[MinValueValidator(1)])
    currency = models.CharField(max_length=3, default=DEFAULT_CURRENCY)
    currency_exponent = models.PositiveSmallIntegerField(
        default=DEFAULT_CURRENCY_EXPONENT,
    )
    valid_from = models.DateField()
    valid_to = models.DateField(null=True, blank=True)
    memo = models.TextField(blank=True)

    class Meta:
        ordering = ['event_series_id', 'valid_from']

    def __str__(self):
        return f'{self.event_series}: {self.amount_units} from {self.valid_from}'

    def clean(self):
        super().clean()
        if self.currency != DEFAULT_CURRENCY:
            raise ValidationError({'currency': 'Only USD is supported in v1.'})
        if self.currency_exponent != DEFAULT_CURRENCY_EXPONENT:
            raise ValidationError({'currency_exponent': 'USD uses 4 ledger decimal places in v1.'})
        if self.valid_to and self.valid_to < self.valid_from:
            raise ValidationError({'valid_to': 'Valid-to date cannot be before valid-from date.'})
        if self.event_series_id:
            for other in EventSeriesVersion.objects.filter(event_series=self.event_series).exclude(pk=self.pk):
                if _date_ranges_overlap(self.valid_from, self.valid_to, other.valid_from, other.valid_to):
                    raise ValidationError('Event series versions cannot overlap.')


class FinancialEvent(TimestampedModel):
    class EventType(models.TextChoices):
        PRINCIPAL_ADVANCE = 'principal_advance', 'Principal advance'
        REPAYMENT = 'repayment', 'Repayment'
        SCHEDULED_CHARGE = 'scheduled_charge', 'Scheduled charge'
        INTEREST_POSTING = 'interest_posting', 'Interest posting'
        ADJUSTMENT = 'adjustment', 'Adjustment'

    class Source(models.TextChoices):
        MANUAL = 'manual', 'Manual'
        GENERATED = 'generated', 'Generated'
        SYSTEM = 'system', 'System'

    class Direction(models.TextChoices):
        INCREASES_DEBT = 'increases_debt', 'Increases debt'
        DECREASES_DEBT = 'decreases_debt', 'Decreases debt'

    obligation = models.ForeignKey(
        Obligation,
        on_delete=models.CASCADE,
        related_name='financial_events',
    )
    event_type = models.CharField(max_length=40, choices=EventType.choices)
    source = models.CharField(
        max_length=20,
        choices=Source.choices,
        default=Source.MANUAL,
    )
    event_date = models.DateField()
    amount_units = models.BigIntegerField(validators=[MinValueValidator(1)])
    currency = models.CharField(max_length=3, default=DEFAULT_CURRENCY)
    currency_exponent = models.PositiveSmallIntegerField(
        default=DEFAULT_CURRENCY_EXPONENT,
    )
    direction = models.CharField(max_length=20, choices=Direction.choices)
    memo = models.TextField(blank=True)
    category = models.CharField(max_length=80, blank=True)
    event_series = models.ForeignKey(
        EventSeries,
        on_delete=models.PROTECT,
        related_name='financial_events',
        null=True,
        blank=True,
    )
    event_series_version = models.ForeignKey(
        EventSeriesVersion,
        on_delete=models.PROTECT,
        related_name='financial_events',
        null=True,
        blank=True,
    )
    period_start = models.DateField(null=True, blank=True)
    period_end = models.DateField(null=True, blank=True)
    revision = models.PositiveIntegerField(default=1, validators=[MinValueValidator(1)])
    voided_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-event_date', '-created_at']
        constraints = [
            models.UniqueConstraint(
                fields=['event_series', 'period_start', 'revision'],
                condition=Q(event_series__isnull=False, period_start__isnull=False),
                name='unique_series_period_event_revision',
            ),
            models.UniqueConstraint(
                fields=['event_series', 'period_start'],
                condition=Q(
                    event_series__isnull=False,
                    period_start__isnull=False,
                    voided_at__isnull=True,
                ),
                name='unique_active_series_period_event',
            ),
        ]

    def __str__(self):
        return f'{self.get_event_type_display()} {self.amount_units} on {self.event_date}'

    def clean(self):
        super().clean()
        if self.currency != DEFAULT_CURRENCY:
            raise ValidationError({'currency': 'Only USD is supported in v1.'})
        if self.currency_exponent != DEFAULT_CURRENCY_EXPONENT:
            raise ValidationError({'currency_exponent': 'USD uses 4 ledger decimal places in v1.'})
        if self.period_start and self.period_end and self.period_end <= self.period_start:
            raise ValidationError({'period_end': 'Period end must be after period start.'})
        if self.event_series_version_id and self.event_series_id:
            if self.event_series_version.event_series_id != self.event_series_id:
                raise ValidationError({'event_series_version': 'Version must belong to the selected event series.'})


class LedgerTransaction(TimestampedModel):
    class Status(models.TextChoices):
        DRAFT = 'draft', 'Draft'
        POSTED = 'posted', 'Posted'
        VOIDED = 'voided', 'Voided'

    obligation = models.ForeignKey(
        Obligation,
        on_delete=models.CASCADE,
        related_name='ledger_transactions',
    )
    financial_event = models.OneToOneField(
        FinancialEvent,
        on_delete=models.PROTECT,
        related_name='ledger_transaction',
    )
    transaction_type = models.CharField(max_length=40, choices=FinancialEvent.EventType.choices)
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.DRAFT,
    )
    transaction_date = models.DateField()
    idempotency_key = models.CharField(max_length=160, unique=True, null=True, blank=True)
    memo = models.TextField(blank=True)
    posted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-transaction_date', '-created_at']

    def __str__(self):
        return f'{self.get_transaction_type_display()} on {self.transaction_date}'

    def save(self, *args, **kwargs):
        if self.pk:
            existing = LedgerTransaction.objects.get(pk=self.pk)
            if existing.status == self.Status.POSTED:
                raise ValidationError('Posted ledger transactions are immutable.')
        super().save(*args, **kwargs)

    def post(self):
        if self.status != self.Status.DRAFT:
            raise ValidationError('Only draft transactions can be posted.')
        entries = list(self.entries.all())
        if not entries:
            raise ValidationError('A transaction must have entries before posting.')
        debit_total = sum(entry.amount_units for entry in entries if entry.side == LedgerEntry.Side.DEBIT)
        credit_total = sum(entry.amount_units for entry in entries if entry.side == LedgerEntry.Side.CREDIT)
        if debit_total != credit_total:
            raise ValidationError('Posted transactions must balance: debit total must equal credit total.')
        if any(entry.currency != DEFAULT_CURRENCY for entry in entries):
            raise ValidationError('Only USD entries are supported in v1.')
        self.status = self.Status.POSTED
        self.posted_at = timezone.now()
        self.save(update_fields=['status', 'posted_at', 'updated_at'])


class LedgerEntry(TimestampedModel):
    class Side(models.TextChoices):
        DEBIT = 'debit', 'Debit'
        CREDIT = 'credit', 'Credit'

    transaction = models.ForeignKey(
        LedgerTransaction,
        on_delete=models.CASCADE,
        related_name='entries',
    )
    account = models.ForeignKey(
        LedgerAccount,
        on_delete=models.PROTECT,
        related_name='entries',
    )
    entry_type = models.CharField(max_length=40, choices=FinancialEvent.EventType.choices)
    effective_date = models.DateField()
    side = models.CharField(max_length=10, choices=Side.choices)
    amount_units = models.BigIntegerField(validators=[MinValueValidator(1)])
    currency = models.CharField(max_length=3, default=DEFAULT_CURRENCY)
    currency_exponent = models.PositiveSmallIntegerField(
        default=DEFAULT_CURRENCY_EXPONENT,
    )
    memo = models.TextField(blank=True)

    class Meta:
        ordering = ['effective_date', 'id']

    def __str__(self):
        return f'{self.get_side_display()} {self.amount_units} {self.account}'

    def clean(self):
        super().clean()
        if self.currency != DEFAULT_CURRENCY:
            raise ValidationError({'currency': 'Only USD is supported in v1.'})
        if self.currency_exponent != DEFAULT_CURRENCY_EXPONENT:
            raise ValidationError({'currency_exponent': 'USD uses 4 ledger decimal places in v1.'})
        if self.account_id and self.transaction_id:
            if self.account.obligation_id != self.transaction.obligation_id:
                raise ValidationError({'account': 'Entry account must belong to the same obligation as the transaction.'})

    def save(self, *args, **kwargs):
        if self.transaction_id:
            if self.transaction.status == LedgerTransaction.Status.POSTED:
                raise ValidationError('Posted ledger entries are immutable.')
            if self.pk:
                existing = LedgerEntry.objects.select_related('transaction').get(pk=self.pk)
                if existing.transaction.status == LedgerTransaction.Status.POSTED:
                    raise ValidationError('Posted ledger entries are immutable.')
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        if self.transaction.status == LedgerTransaction.Status.POSTED:
            raise ValidationError('Posted ledger entries are immutable.')
        return super().delete(*args, **kwargs)


class InterestRatePeriod(TimestampedModel):
    obligation = models.ForeignKey(
        Obligation,
        on_delete=models.CASCADE,
        related_name='interest_rate_periods',
    )
    annual_rate_percent = models.DecimalField(
        max_digits=9,
        decimal_places=4,
        validators=[MinValueValidator(0)],
    )
    effective_from = models.DateField()
    effective_to = models.DateField(null=True, blank=True)
    memo = models.TextField(blank=True)

    class Meta:
        ordering = ['obligation_id', 'effective_from']

    def __str__(self):
        return f'{self.annual_rate_percent}% from {self.effective_from}'

    def clean(self):
        super().clean()
        if self.effective_to and self.effective_to < self.effective_from:
            raise ValidationError({'effective_to': 'Effective-to date cannot be before effective-from date.'})
        if self.obligation_id:
            for other in InterestRatePeriod.objects.filter(obligation=self.obligation).exclude(pk=self.pk):
                if _date_ranges_overlap(self.effective_from, self.effective_to, other.effective_from, other.effective_to):
                    raise ValidationError('Interest rate periods cannot overlap.')


class InterestAccrualRun(TimestampedModel):
    class Status(models.TextChoices):
        CALCULATED = 'calculated', 'Calculated'
        POSTED = 'posted', 'Posted'
        VOIDED = 'voided', 'Voided'

    obligation = models.ForeignKey(
        Obligation,
        on_delete=models.CASCADE,
        related_name='interest_accrual_runs',
    )
    period_start = models.DateField()
    period_end = models.DateField()
    posted_on = models.DateField()
    revision = models.PositiveIntegerField(default=1)
    calculated_interest_amount_units = models.BigIntegerField(default=0)
    ledger_transaction = models.OneToOneField(
        LedgerTransaction,
        on_delete=models.PROTECT,
        related_name='interest_accrual_run',
        null=True,
        blank=True,
    )
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.CALCULATED,
    )
    calculation_payload = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ['-period_start', '-revision']
        constraints = [
            models.UniqueConstraint(
                fields=['obligation', 'period_start', 'period_end', 'revision'],
                name='unique_interest_run_period_revision',
            ),
            models.UniqueConstraint(
                fields=['obligation', 'period_start', 'period_end'],
                condition=Q(status='posted'),
                name='unique_posted_interest_run_period',
            ),
        ]

    def __str__(self):
        return f'Interest {self.obligation} {self.period_start} to {self.period_end}'

    def clean(self):
        super().clean()
        if self.period_end <= self.period_start:
            raise ValidationError({'period_end': 'Period end must be after period start.'})


class AuditEvent(models.Model):
    event_type = models.CharField(max_length=80)
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='ledger_audit_events',
    )
    obligation = models.ForeignKey(
        Obligation,
        on_delete=models.CASCADE,
        related_name='audit_events',
        null=True,
        blank=True,
    )
    financial_event = models.ForeignKey(
        FinancialEvent,
        on_delete=models.SET_NULL,
        related_name='audit_events',
        null=True,
        blank=True,
    )
    payload = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return self.event_type


def _date_ranges_overlap(left_start, left_end, right_start, right_end):
    effective_left_end = left_end or models.DateField().to_python('9999-12-31')
    effective_right_end = right_end or models.DateField().to_python('9999-12-31')
    return left_start <= effective_right_end and right_start <= effective_left_end
