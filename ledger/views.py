from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from ledger.forms import (
    CreateObligationForm,
    InterestRatePeriodForm,
    RecurringChargeForm,
    RepaymentForm,
)
from ledger.models import (
    EventSeries,
    FinancialEvent,
    InterestRatePeriod,
    LedgerEntry,
    LedgerTransaction,
    Obligation,
)
from ledger.services.balances import get_obligation_balance
from ledger.services.events import post_principal_advance, post_repayment
from ledger.services.recurring import generate_due_recurring_events


def related_obligations(user):
    return Obligation.objects.filter(Q(creditor=user) | Q(borrower=user))


def get_related_obligation(user, pk):
    return get_object_or_404(related_obligations(user), pk=pk)


def user_label(user):
    return user.get_full_name() or user.get_username()


@login_required
def dashboard(request):
    obligations = list(
        related_obligations(request.user)
        .filter(status=Obligation.Status.OPEN)
        .select_related('borrower', 'creditor')
    )
    rows = [_obligation_row(obligation, request.user) for obligation in obligations]
    i_owe = sum(row['balance_units'] for row in rows if row['role'] == 'borrower')
    owed_to_me = sum(row['balance_units'] for row in rows if row['role'] == 'creditor')
    recent_transactions = (
        LedgerTransaction.objects.filter(obligation__in=obligations)
        .select_related('obligation', 'financial_event')
        .order_by('-transaction_date', '-created_at')[:10]
    )
    return render(
        request,
        'ledger/dashboard.html',
        {
            'rows': rows,
            'i_owe': i_owe,
            'owed_to_me': owed_to_me,
            'net_balance': owed_to_me - i_owe,
            'recent_transactions': recent_transactions,
        },
    )


@login_required
def obligation_list(request):
    obligations = related_obligations(request.user).select_related('borrower', 'creditor')
    rows = [_obligation_row(obligation, request.user) for obligation in obligations]
    return render(request, 'ledger/obligation_list.html', {'rows': rows})


@login_required
def obligation_detail(request, pk):
    obligation = get_related_obligation(request.user, pk)
    ledger_entries = (
        LedgerEntry.objects.filter(account__obligation=obligation)
        .select_related('transaction', 'account')
        .order_by('-effective_date', '-created_at')
    )
    event_series = (
        EventSeries.objects.filter(obligation=obligation)
        .prefetch_related('versions')
        .order_by('name')
    )
    context = {
        'obligation': obligation,
        'balance_units': get_obligation_balance(obligation),
        'role': _role_for(obligation, request.user),
        'ledger_entries': ledger_entries,
        'financial_events': FinancialEvent.objects.filter(obligation=obligation).order_by('-event_date'),
        'event_series': event_series,
        'interest_rates': InterestRatePeriod.objects.filter(obligation=obligation).order_by('-effective_from'),
    }
    return render(request, 'ledger/obligation_detail.html', context)


@login_required
def obligation_create(request):
    if request.method == 'POST':
        form = CreateObligationForm(request.POST, user=request.user)
        if form.is_valid():
            creditor, borrower = form.get_participants()
            try:
                with transaction.atomic():
                    obligation = Obligation(
                        creditor=creditor,
                        borrower=borrower,
                        title=form.cleaned_data['title'],
                        category=form.cleaned_data.get('category', ''),
                        opened_on=form.cleaned_data['opened_on'],
                    )
                    obligation.full_clean()
                    obligation.save()
                    post_principal_advance(
                        obligation,
                        amount_units=form.amount_units,
                        event_date=form.cleaned_data['opened_on'],
                        memo=form.cleaned_data.get('memo', ''),
                        category=form.cleaned_data.get('category', ''),
                    )
                return redirect('ledger:obligation_detail', pk=obligation.pk)
            except ValidationError as error:
                form.add_error(None, error)
    else:
        form = CreateObligationForm(user=request.user)
    return render(
        request,
        'ledger/form.html',
        {
            'title': 'New obligation',
            'form': form,
            'submit_label': 'Create obligation',
            'back_url': reverse('ledger:obligation_list'),
        },
    )


@login_required
def repayment_create(request, pk):
    obligation = get_related_obligation(request.user, pk)
    if request.method == 'POST':
        form = RepaymentForm(request.POST)
        if form.is_valid():
            try:
                post_repayment(
                    obligation,
                    amount_units=form.amount_units,
                    event_date=form.cleaned_data['event_date'],
                    memo=form.cleaned_data.get('memo', ''),
                )
                return redirect('ledger:obligation_detail', pk=obligation.pk)
            except ValidationError as error:
                form.add_error(None, error)
    else:
        form = RepaymentForm()
    return render(
        request,
        'ledger/form.html',
        {
            'title': f'Record repayment: {obligation.title}',
            'form': form,
            'submit_label': 'Record repayment',
            'back_url': reverse('ledger:obligation_detail', kwargs={'pk': obligation.pk}),
        },
    )


@login_required
def recurring_charge_create(request, pk):
    obligation = get_related_obligation(request.user, pk)
    if request.method == 'POST':
        form = RecurringChargeForm(request.POST)
        if form.is_valid():
            try:
                with transaction.atomic():
                    form.save(obligation)
                return redirect('ledger:obligation_detail', pk=obligation.pk)
            except ValidationError as error:
                form.add_error(None, error)
    else:
        form = RecurringChargeForm()
    return render(
        request,
        'ledger/form.html',
        {
            'title': f'New recurring charge: {obligation.title}',
            'form': form,
            'submit_label': 'Create recurring charge',
            'back_url': reverse('ledger:obligation_detail', kwargs={'pk': obligation.pk}),
        },
    )


@login_required
def interest_rate_create(request, pk):
    obligation = get_related_obligation(request.user, pk)
    if request.method == 'POST':
        form = InterestRatePeriodForm(request.POST)
        if form.is_valid():
            try:
                form.save_for_obligation(obligation)
                return redirect('ledger:obligation_detail', pk=obligation.pk)
            except ValidationError as error:
                form.add_error(None, error)
    else:
        form = InterestRatePeriodForm()
    return render(
        request,
        'ledger/form.html',
        {
            'title': f'New interest rate: {obligation.title}',
            'form': form,
            'submit_label': 'Save rate',
            'back_url': reverse('ledger:obligation_detail', kwargs={'pk': obligation.pk}),
        },
    )


@login_required
@require_POST
def recurring_due_generate(request, pk):
    obligation = get_related_obligation(request.user, pk)
    created_transactions = generate_due_recurring_events(obligation=obligation)
    messages.success(request, f'Generated {len(created_transactions)} due recurring charge(s).')
    return redirect('ledger:obligation_detail', pk=obligation.pk)


@login_required
@require_POST
def obligation_close(request, pk):
    obligation = get_related_obligation(request.user, pk)
    closed_on = timezone.localdate()
    with transaction.atomic():
        obligation.status = Obligation.Status.CLOSED
        obligation.closed_on = closed_on
        obligation.save(update_fields=['status', 'closed_on', 'updated_at'])
        EventSeries.objects.filter(obligation=obligation, active=True, starts_on__lte=closed_on).update(
            active=False,
            ends_on=closed_on,
        )
        EventSeries.objects.filter(obligation=obligation, active=True, starts_on__gt=closed_on).update(
            active=False,
        )
    messages.success(request, 'Obligation was closed and future recurring charges were stopped.')
    return redirect('ledger:obligation_detail', pk=obligation.pk)


def _obligation_row(obligation, user):
    return {
        'obligation': obligation,
        'balance_units': get_obligation_balance(obligation),
        'role': _role_for(obligation, user),
        'counterparty': obligation.creditor if obligation.borrower_id == user.id else obligation.borrower,
    }


def _role_for(obligation, user):
    if obligation.borrower_id == user.id:
        return 'borrower'
    return 'creditor'
