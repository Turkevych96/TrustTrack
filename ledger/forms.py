from decimal import Decimal
from datetime import timedelta

from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.forms import UserCreationForm
from django.core.exceptions import ValidationError
from django.utils import timezone

from ledger.models import (
    EventSeries,
    EventSeriesVersion,
    FinancialEvent,
    InterestRatePeriod,
    Obligation,
    ObligationCategory,
    UserProfile,
)
from ledger.services.money import units_from_decimal


DAY_OF_WEEK_CHOICES = (
    (0, 'Monday'),
    (1, 'Tuesday'),
    (2, 'Wednesday'),
    (3, 'Thursday'),
    (4, 'Friday'),
    (5, 'Saturday'),
    (6, 'Sunday'),
)


class MoneyForm(forms.Form):
    amount = forms.DecimalField(
        label='Amount',
        max_digits=18,
        decimal_places=2,
        min_value=Decimal('0.01'),
        widget=forms.NumberInput(attrs={'step': '0.01', 'min': '0.01'}),
    )

    @property
    def amount_units(self):
        return units_from_decimal(self.cleaned_data['amount'])


class UserChoiceField(forms.ModelChoiceField):
    def label_from_instance(self, user):
        return user.get_full_name() or user.get_username()


class UserProfileForm(forms.ModelForm):
    class Meta:
        model = UserProfile
        fields = ['telegram_id']
        labels = {
            'telegram_id': 'Telegram ID',
        }
        widgets = {
            'telegram_id': forms.NumberInput(attrs={'min': '1'}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['telegram_id'].min_value = 1
        self.fields['telegram_id'].widget.attrs.update({'min': '1', 'step': '1'})


class ModulePreferencesForm(forms.ModelForm):
    class Meta:
        model = UserProfile
        fields = ['show_planner_module', 'show_dashboard_balance_history']
        labels = {
            'show_planner_module': 'Planner',
            'show_dashboard_balance_history': 'Dashboard balance history',
        }
        help_texts = {
            'show_planner_module': 'Show Planner in the navigation bar.',
            'show_dashboard_balance_history': 'Show the Balance history chart on Dashboard.',
        }


class SignUpForm(UserCreationForm):
    class Meta(UserCreationForm.Meta):
        model = get_user_model()
        fields = ('username', 'first_name', 'last_name', 'email')


class CreateObligationForm(MoneyForm):
    ROLE_LENT = 'lent'
    ROLE_BORROWED = 'borrowed'
    PAYMENT_MODE_ONE_TIME = 'one_time'
    PAYMENT_MODE_RECURRING = 'recurring'

    ROLE_CHOICES = (
        (ROLE_LENT, 'I lent money'),
        (ROLE_BORROWED, 'I borrowed money'),
    )
    PAYMENT_MODE_CHOICES = (
        (PAYMENT_MODE_ONE_TIME, 'One-time payment'),
        (PAYMENT_MODE_RECURRING, 'Recurring payment'),
    )

    role = forms.ChoiceField(choices=ROLE_CHOICES)
    counterparty = UserChoiceField(queryset=get_user_model().objects.none())
    title = forms.CharField(max_length=160)
    category = forms.ModelChoiceField(queryset=ObligationCategory.objects.none(), required=False)
    payment_mode = forms.ChoiceField(
        label='Payment schedule',
        choices=PAYMENT_MODE_CHOICES,
        initial=PAYMENT_MODE_ONE_TIME,
        required=False,
        widget=forms.RadioSelect,
    )
    opened_on = forms.DateField(
        initial=timezone.localdate,
        widget=forms.DateInput(attrs={'type': 'date'}),
    )
    recurring_frequency = forms.ChoiceField(
        label='Repeat',
        choices=EventSeries.Frequency.choices,
        initial=EventSeries.Frequency.MONTHLY,
        required=False,
    )
    recurring_day_of_month = forms.IntegerField(
        label='Day of month',
        min_value=1,
        max_value=31,
        initial=1,
        required=False,
    )
    recurring_day_of_week = forms.ChoiceField(
        label='Day of week',
        choices=DAY_OF_WEEK_CHOICES,
        required=False,
    )
    recurring_starts_on = forms.DateField(
        label='Recurring starts on',
        required=False,
        widget=forms.DateInput(attrs={'type': 'date'}),
    )
    recurring_ends_on = forms.DateField(
        label='Recurring ends on',
        required=False,
        widget=forms.DateInput(attrs={'type': 'date'}),
    )
    has_interest = forms.BooleanField(label='With interest', required=False)
    annual_rate_percent = forms.DecimalField(
        label='Annual interest rate (%)',
        max_digits=9,
        decimal_places=4,
        min_value=Decimal('0.0001'),
        required=False,
        widget=forms.NumberInput(attrs={'step': '0.0001', 'min': '0.0001'}),
        help_text='Example: 3.5 means 3.5% APR.',
    )
    memo = forms.CharField(required=False, widget=forms.Textarea(attrs={'rows': 3}))

    def __init__(self, *args, user, **kwargs):
        if args and args[0] is not None:
            data = args[0].copy()
            payment_mode = data.get('payment_mode') or self.PAYMENT_MODE_ONE_TIME
            frequency = data.get('recurring_frequency') or EventSeries.Frequency.MONTHLY
            if payment_mode != self.PAYMENT_MODE_RECURRING:
                for field_name in (
                    'recurring_frequency',
                    'recurring_day_of_month',
                    'recurring_day_of_week',
                    'recurring_starts_on',
                    'recurring_ends_on',
                ):
                    data[field_name] = ''
            elif frequency == EventSeries.Frequency.MONTHLY:
                data['recurring_day_of_week'] = ''
            else:
                data['recurring_day_of_month'] = ''
            if not data.get('has_interest'):
                data['annual_rate_percent'] = ''
            args = (data, *args[1:])
        super().__init__(*args, **kwargs)
        self.user = user
        self.fields['counterparty'].queryset = (
            get_user_model()
            .objects.filter(is_active=True)
            .exclude(pk=user.pk)
            .order_by('username')
        )
        self.fields['category'].queryset = ObligationCategory.objects.filter(active=True).order_by('name')
        self.order_fields([
            'role',
            'counterparty',
            'title',
            'category',
            'payment_mode',
            'amount',
            'opened_on',
            'recurring_frequency',
            'recurring_day_of_month',
            'recurring_day_of_week',
            'recurring_starts_on',
            'recurring_ends_on',
            'has_interest',
            'annual_rate_percent',
            'memo',
        ])

    def clean(self):
        cleaned_data = super().clean()
        opened_on = cleaned_data.get('opened_on')
        payment_mode = cleaned_data.get('payment_mode') or self.PAYMENT_MODE_ONE_TIME
        cleaned_data['payment_mode'] = payment_mode

        if payment_mode == self.PAYMENT_MODE_RECURRING:
            starts_on = cleaned_data.get('recurring_starts_on') or opened_on
            ends_on = cleaned_data.get('recurring_ends_on')
            frequency = cleaned_data.get('recurring_frequency') or EventSeries.Frequency.MONTHLY
            day_of_month = cleaned_data.get('recurring_day_of_month')
            day_of_week = cleaned_data.get('recurring_day_of_week')

            cleaned_data['recurring_starts_on'] = starts_on
            cleaned_data['recurring_frequency'] = frequency
            if starts_on and ends_on and ends_on < starts_on:
                self.add_error('recurring_ends_on', 'End date cannot be before start date.')
            if frequency == EventSeries.Frequency.MONTHLY and not day_of_month:
                self.add_error('recurring_day_of_month', 'Day of month is required for monthly schedules.')
            if frequency in (EventSeries.Frequency.WEEKLY, EventSeries.Frequency.BIWEEKLY) and day_of_week in (None, ''):
                self.add_error('recurring_day_of_week', 'Day of week is required for weekly schedules.')
            if frequency == EventSeries.Frequency.MONTHLY:
                cleaned_data['recurring_day_of_week'] = None
            else:
                cleaned_data['recurring_day_of_month'] = None
                cleaned_data['recurring_day_of_week'] = _clean_day_of_week(day_of_week)

        if cleaned_data.get('has_interest') and not cleaned_data.get('annual_rate_percent'):
            self.add_error('annual_rate_percent', 'Interest rate is required when interest is enabled.')
        return cleaned_data

    def get_participants(self):
        counterparty = self.cleaned_data['counterparty']
        if self.cleaned_data['role'] == self.ROLE_LENT:
            return self.user, counterparty
        return counterparty, self.user

    def is_recurring(self):
        return self.cleaned_data.get('payment_mode') == self.PAYMENT_MODE_RECURRING

    def save_recurring_series(self, obligation):
        if not self.is_recurring():
            return None

        series = EventSeries(
            obligation=obligation,
            name=self.cleaned_data['title'],
            event_type=FinancialEvent.EventType.SCHEDULED_CHARGE,
            frequency=self.cleaned_data['recurring_frequency'],
            day_of_month=self.cleaned_data.get('recurring_day_of_month'),
            day_of_week=self.cleaned_data.get('recurring_day_of_week'),
            starts_on=self.cleaned_data['recurring_starts_on'],
            ends_on=self.cleaned_data.get('recurring_ends_on'),
            memo=self.cleaned_data.get('memo', ''),
        )
        series.full_clean()
        series.save()

        version = EventSeriesVersion(
            event_series=series,
            amount_units=self.amount_units,
            valid_from=self.cleaned_data['recurring_starts_on'],
            memo=self.cleaned_data.get('memo', ''),
        )
        version.full_clean()
        version.save()
        return series

    def save_interest_rate(self, obligation):
        if not self.cleaned_data.get('has_interest'):
            return None

        rate = InterestRatePeriod(
            obligation=obligation,
            annual_rate_percent=self.cleaned_data['annual_rate_percent'],
            effective_from=self.cleaned_data['opened_on'],
            memo=self.cleaned_data.get('memo', ''),
        )
        rate.full_clean()
        rate.save()
        return rate


class RepaymentForm(MoneyForm):
    event_date = forms.DateField(
        initial=timezone.localdate,
        widget=forms.DateInput(attrs={'type': 'date'}),
    )
    memo = forms.CharField(required=False, widget=forms.Textarea(attrs={'rows': 3}))


class ManualTransferForm(MoneyForm):
    TRANSFER_TYPE_CHOICES = (
        (FinancialEvent.EventType.PRINCIPAL_ADVANCE, 'Debt increase'),
        (FinancialEvent.EventType.REPAYMENT, 'Repayment'),
    )

    transfer_type = forms.ChoiceField(choices=TRANSFER_TYPE_CHOICES)
    event_date = forms.DateField(widget=forms.DateInput(attrs={'type': 'date'}))
    category = forms.CharField(max_length=80, required=False)
    memo = forms.CharField(required=False, widget=forms.Textarea(attrs={'rows': 3}))

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.order_fields(['transfer_type', 'event_date', 'amount', 'category', 'memo'])


class PlannerHorizonForm(forms.Form):
    MONTH_CHOICES = (
        (12, '12 months'),
        (24, '24 months'),
        (36, '36 months'),
        (60, '5 years'),
    )

    projection_months = forms.TypedChoiceField(
        label='Projection',
        choices=MONTH_CHOICES,
        coerce=int,
        initial=12,
    )


class PayoffSimulatorForm(forms.Form):
    MONTH_CHOICES = (
        (12, '12 months'),
        (24, '24 months'),
        (36, '36 months'),
        (60, '5 years'),
        (120, '10 years'),
    )

    obligation = forms.ModelChoiceField(queryset=Obligation.objects.none())
    monthly_payment = forms.DecimalField(
        label='Monthly payment',
        max_digits=18,
        decimal_places=2,
        min_value=Decimal('0.01'),
        widget=forms.NumberInput(attrs={'step': '0.01', 'min': '0.01'}),
    )
    payment_day = forms.IntegerField(
        label='Payment day',
        min_value=1,
        max_value=31,
        initial=1,
    )
    simulation_months = forms.TypedChoiceField(
        label='Simulation',
        choices=MONTH_CHOICES,
        coerce=int,
        initial=60,
    )

    def __init__(self, *args, obligations, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['obligation'].queryset = obligations

    @property
    def monthly_payment_units(self):
        return units_from_decimal(self.cleaned_data['monthly_payment'])


class RecurringChargeForm(MoneyForm):
    EVENT_TYPE_CHOICES = (
        (FinancialEvent.EventType.SCHEDULED_CHARGE, 'Scheduled charge - increases debt'),
        (FinancialEvent.EventType.REPAYMENT, 'Automatic repayment - decreases debt'),
    )

    event_type = forms.ChoiceField(
        label='Recurring type',
        choices=EVENT_TYPE_CHOICES,
    )
    name = forms.CharField(max_length=160)
    frequency = forms.ChoiceField(choices=EventSeries.Frequency.choices)
    day_of_month = forms.IntegerField(min_value=1, max_value=31, required=False)
    day_of_week = forms.ChoiceField(choices=DAY_OF_WEEK_CHOICES, required=False)
    starts_on = forms.DateField(widget=forms.DateInput(attrs={'type': 'date'}))
    ends_on = forms.DateField(required=False, widget=forms.DateInput(attrs={'type': 'date'}))
    memo = forms.CharField(required=False, widget=forms.Textarea(attrs={'rows': 3}))

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.order_fields([
            'event_type',
            'name',
            'amount',
            'frequency',
            'day_of_month',
            'day_of_week',
            'starts_on',
            'ends_on',
            'memo',
        ])

    def clean(self):
        cleaned_data = super().clean()
        starts_on = cleaned_data.get('starts_on')
        ends_on = cleaned_data.get('ends_on')
        frequency = cleaned_data.get('frequency')
        day_of_month = cleaned_data.get('day_of_month')
        day_of_week = cleaned_data.get('day_of_week')
        if starts_on and ends_on and ends_on < starts_on:
            raise ValidationError('End date cannot be before start date.')
        if frequency == EventSeries.Frequency.MONTHLY and not day_of_month:
            raise ValidationError('Day of month is required for monthly schedules.')
        if frequency in (EventSeries.Frequency.WEEKLY, EventSeries.Frequency.BIWEEKLY) and day_of_week in (None, ''):
            raise ValidationError('Day of week is required for weekly schedules.')
        if frequency in (EventSeries.Frequency.WEEKLY, EventSeries.Frequency.BIWEEKLY):
            cleaned_data['day_of_week'] = _clean_day_of_week(day_of_week)
        return cleaned_data

    def save(self, obligation):
        series = EventSeries(
            obligation=obligation,
            name=self.cleaned_data['name'],
            event_type=self.cleaned_data['event_type'],
            frequency=self.cleaned_data['frequency'],
            day_of_month=self.cleaned_data.get('day_of_month') if self.cleaned_data['frequency'] == EventSeries.Frequency.MONTHLY else None,
            day_of_week=_clean_day_of_week(self.cleaned_data.get('day_of_week')) if self.cleaned_data['frequency'] != EventSeries.Frequency.MONTHLY else None,
            starts_on=self.cleaned_data['starts_on'],
            ends_on=self.cleaned_data.get('ends_on'),
            memo=self.cleaned_data.get('memo', ''),
        )
        series.full_clean()
        series.save()

        version = EventSeriesVersion(
            event_series=series,
            amount_units=self.amount_units,
            valid_from=self.cleaned_data['starts_on'],
            memo=self.cleaned_data.get('memo', ''),
        )
        version.full_clean()
        version.save()
        return series


class RecurringSeriesUpdateForm(forms.ModelForm):
    event_type = forms.ChoiceField(
        label='Recurring type',
        choices=RecurringChargeForm.EVENT_TYPE_CHOICES,
    )
    new_amount = forms.DecimalField(
        label='New amount',
        max_digits=18,
        decimal_places=2,
        min_value=Decimal('0.01'),
        required=False,
        widget=forms.NumberInput(attrs={'step': '0.01', 'min': '0.01'}),
        help_text='Leave blank to keep the current amount schedule.',
    )
    amount_valid_from = forms.DateField(
        label='New amount starts on',
        required=False,
        widget=forms.DateInput(attrs={'type': 'date'}),
    )
    version_memo = forms.CharField(
        label='New amount memo',
        required=False,
        widget=forms.Textarea(attrs={'rows': 3}),
    )

    day_of_week = forms.ChoiceField(choices=DAY_OF_WEEK_CHOICES, required=False)

    class Meta:
        model = EventSeries
        fields = [
            'event_type',
            'name',
            'frequency',
            'day_of_month',
            'day_of_week',
            'starts_on',
            'ends_on',
            'active',
            'memo',
        ]
        widgets = {
            'starts_on': forms.DateInput(attrs={'type': 'date'}),
            'ends_on': forms.DateInput(attrs={'type': 'date'}),
            'memo': forms.Textarea(attrs={'rows': 3}),
        }

    def clean(self):
        cleaned_data = super().clean()
        starts_on = cleaned_data.get('starts_on')
        ends_on = cleaned_data.get('ends_on')
        new_amount = cleaned_data.get('new_amount')
        amount_valid_from = cleaned_data.get('amount_valid_from')
        frequency = cleaned_data.get('frequency')
        day_of_month = cleaned_data.get('day_of_month')
        day_of_week = cleaned_data.get('day_of_week')
        if starts_on and ends_on and ends_on < starts_on:
            raise ValidationError('End date cannot be before start date.')
        if frequency == EventSeries.Frequency.MONTHLY and not day_of_month:
            raise ValidationError('Day of month is required for monthly schedules.')
        if frequency in (EventSeries.Frequency.WEEKLY, EventSeries.Frequency.BIWEEKLY) and day_of_week in (None, ''):
            raise ValidationError('Day of week is required for weekly schedules.')
        if frequency == EventSeries.Frequency.MONTHLY:
            cleaned_data['day_of_week'] = None
        else:
            cleaned_data['day_of_month'] = None
            cleaned_data['day_of_week'] = _clean_day_of_week(day_of_week)
        if new_amount and not amount_valid_from:
            raise ValidationError('New amount start date is required when changing the amount.')
        if amount_valid_from and starts_on and amount_valid_from < starts_on:
            raise ValidationError('New amount start date cannot be before the schedule start date.')
        if amount_valid_from and ends_on and amount_valid_from > ends_on:
            raise ValidationError('New amount start date cannot be after the schedule end date.')
        return cleaned_data

    def save(self, commit=True):
        series = super().save(commit=False)
        series.full_clean()
        if commit:
            series.save()
            self._save_amount_version(series)
        return series

    def _save_amount_version(self, series):
        amount = self.cleaned_data.get('new_amount')
        valid_from = self.cleaned_data.get('amount_valid_from')
        if not amount:
            return None

        amount_units = units_from_decimal(amount)
        next_version = (
            EventSeriesVersion.objects.filter(event_series=series, valid_from__gt=valid_from)
            .order_by('valid_from')
            .first()
        )
        new_valid_to = next_version.valid_from - timedelta(days=1) if next_version else None

        existing_version = EventSeriesVersion.objects.filter(event_series=series, valid_from=valid_from).first()
        if existing_version:
            existing_version.amount_units = amount_units
            existing_version.valid_to = new_valid_to
            existing_version.memo = self.cleaned_data.get('version_memo', '')
            existing_version.full_clean()
            existing_version.save()
            return existing_version

        overlapping_previous = (
            EventSeriesVersion.objects.filter(event_series=series, valid_from__lt=valid_from)
            .filter(models_version_valid_on_or_after(valid_from))
            .order_by('-valid_from')
            .first()
        )
        if overlapping_previous:
            overlapping_previous.valid_to = valid_from - timedelta(days=1)
            overlapping_previous.full_clean()
            overlapping_previous.save()

        version = EventSeriesVersion(
            event_series=series,
            amount_units=amount_units,
            valid_from=valid_from,
            valid_to=new_valid_to,
            memo=self.cleaned_data.get('version_memo', ''),
        )
        version.full_clean()
        version.save()
        return version


class InterestRatePeriodForm(forms.ModelForm):
    class Meta:
        model = InterestRatePeriod
        fields = ['annual_rate_percent', 'effective_from', 'effective_to', 'memo']
        widgets = {
            'effective_from': forms.DateInput(attrs={'type': 'date'}),
            'effective_to': forms.DateInput(attrs={'type': 'date'}),
            'memo': forms.Textarea(attrs={'rows': 3}),
        }

    def save_for_obligation(self, obligation):
        period = self.save(commit=False)
        period.obligation = obligation
        period.full_clean()
        period.save()
        return period


class InterestRecalculateForm(forms.Form):
    from_date = forms.DateField(
        label='Recalculate from date',
        help_text='Interest postings from this month forward will be reversed and regenerated.',
        widget=forms.DateInput(attrs={'type': 'date'}),
    )


class RecurringRecalculateForm(forms.Form):
    from_date = forms.DateField(
        label='Recalculate from date',
        help_text='Generated recurring events from this date forward will be compared with the current schedules.',
        widget=forms.DateInput(attrs={'type': 'date'}),
    )


def models_version_valid_on_or_after(valid_from):
    from django.db.models import Q

    return Q(valid_to__isnull=True) | Q(valid_to__gte=valid_from)


def _clean_day_of_week(day_of_week):
    if day_of_week in (None, ''):
        return None
    return int(day_of_week)
