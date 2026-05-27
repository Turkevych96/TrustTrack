from decimal import Decimal

from django import forms
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.utils import timezone

from ledger.models import EventSeries, EventSeriesVersion, InterestRatePeriod
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
    name = forms.CharField(max_length=160)
    day_of_month = forms.IntegerField(min_value=1, max_value=31)
    starts_on = forms.DateField(widget=forms.DateInput(attrs={'type': 'date'}))
    ends_on = forms.DateField(required=False, widget=forms.DateInput(attrs={'type': 'date'}))
    memo = forms.CharField(required=False, widget=forms.Textarea(attrs={'rows': 3}))

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.order_fields(['name', 'amount', 'day_of_month', 'starts_on', 'ends_on', 'memo'])

    def clean(self):
        cleaned_data = super().clean()
        starts_on = cleaned_data.get('starts_on')
        ends_on = cleaned_data.get('ends_on')
        if starts_on and ends_on and ends_on < starts_on:
            raise ValidationError('End date cannot be before start date.')
        return cleaned_data

    def save(self, obligation):
        series = EventSeries(
            obligation=obligation,
            name=self.cleaned_data['name'],
            day_of_month=self.cleaned_data['day_of_month'],
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
