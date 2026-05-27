from decimal import Decimal
from datetime import timedelta

from django import forms
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.utils import timezone

from ledger.models import EventSeries, EventSeriesVersion, FinancialEvent, InterestRatePeriod
from ledger.services.money import units_from_decimal


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


class CreateObligationForm(MoneyForm):
    ROLE_LENT = 'lent'
    ROLE_BORROWED = 'borrowed'
    ROLE_CHOICES = (
        (ROLE_LENT, 'I lent money'),
        (ROLE_BORROWED, 'I borrowed money'),
    )

    role = forms.ChoiceField(choices=ROLE_CHOICES)
    counterparty = forms.ModelChoiceField(queryset=get_user_model().objects.none())
    title = forms.CharField(max_length=160)
    category = forms.CharField(max_length=80, required=False)
    opened_on = forms.DateField(
        initial=timezone.localdate,
        widget=forms.DateInput(attrs={'type': 'date'}),
    )
    memo = forms.CharField(required=False, widget=forms.Textarea(attrs={'rows': 3}))

    def __init__(self, *args, user, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user
        self.fields['counterparty'].queryset = (
            get_user_model()
            .objects.filter(is_active=True)
            .exclude(pk=user.pk)
            .order_by('username')
        )
        self.order_fields(['role', 'counterparty', 'title', 'category', 'amount', 'opened_on', 'memo'])

    def get_participants(self):
        counterparty = self.cleaned_data['counterparty']
        if self.cleaned_data['role'] == self.ROLE_LENT:
            return self.user, counterparty
        return counterparty, self.user


class RepaymentForm(MoneyForm):
    event_date = forms.DateField(
        initial=timezone.localdate,
        widget=forms.DateInput(attrs={'type': 'date'}),
    )
    memo = forms.CharField(required=False, widget=forms.Textarea(attrs={'rows': 3}))


class RecurringChargeForm(MoneyForm):
    EVENT_TYPE_CHOICES = (
        (FinancialEvent.EventType.SCHEDULED_CHARGE, 'Scheduled charge - increases debt'),
        (FinancialEvent.EventType.REPAYMENT, 'Automatic repayment - decreases debt'),
    )
    DAY_OF_WEEK_CHOICES = (
        (0, 'Monday'),
        (1, 'Tuesday'),
        (2, 'Wednesday'),
        (3, 'Thursday'),
        (4, 'Friday'),
        (5, 'Saturday'),
        (6, 'Sunday'),
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

    day_of_week = forms.ChoiceField(choices=RecurringChargeForm.DAY_OF_WEEK_CHOICES, required=False)

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


def models_version_valid_on_or_after(valid_from):
    from django.db.models import Q

    return Q(valid_to__isnull=True) | Q(valid_to__gte=valid_from)


def _clean_day_of_week(day_of_week):
    if day_of_week in (None, ''):
        return None
    return int(day_of_week)
