from datetime import timedelta

from django.contrib import messages
from django.contrib.auth import update_session_auth_hash
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import PasswordChangeForm
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from ledger.forms import (
    CreateObligationForm,
    InterestRecalculateForm,
    InterestRatePeriodForm,
    ManualTransferForm,
    PayoffSimulatorForm,
    PlannerHorizonForm,
    RecurringChargeForm,
    RecurringRecalculateForm,
    RecurringSeriesUpdateForm,
    RepaymentForm,
    UserProfileForm,
)
from ledger.models import (
    EventSeries,
    FinancialEvent,
    InterestAccrualRun,
    InterestRatePeriod,
    LedgerEntry,
    LedgerTransaction,
    Obligation,
    UserProfile,
)
from ledger.services.balances import get_obligation_balance
from ledger.services.events import edit_manual_transfer, post_principal_advance, post_repayment
from ledger.services.history import build_balance_history
from ledger.services.interest import generate_due_interest, recalculate_interest_from
from ledger.services.money import decimal_from_units
from ledger.services.planner import build_portfolio_projection, simulate_monthly_payment
from ledger.services.recalculation import recalculate_obligation
from ledger.services.recurring import generate_due_recurring_events, recalculate_due_recurring_events
from ledger.services.telegram import TelegramLookupError, get_telegram_chat_identity


HISTORY_PREVIEW_LIMIT = 10
STOP_TRACKING_CONFIRMATION = 'STOP'
TELEGRAM_LOOKUP_TTL = timedelta(hours=24)
TELEGRAM_IDENTITY_UPDATE_FIELDS = (
    'telegram_chat_type',
    'telegram_username',
    'telegram_first_name',
    'telegram_last_name',
    'telegram_title',
    'telegram_lookup_error',
    'telegram_checked_at',
    'updated_at',
)


def related_obligations(user):
    return Obligation.objects.filter(Q(creditor=user) | Q(borrower=user))


def get_related_obligation(user, pk):
    return get_object_or_404(related_obligations(user), pk=pk)


def user_label(user):
    return user.get_full_name() or user.get_username()


@login_required
def dashboard(request):
    all_obligations = list(
        related_obligations(request.user)
        .select_related('borrower', 'creditor')
    )
    open_obligations = [
        obligation
        for obligation in all_obligations
        if obligation.status == Obligation.Status.OPEN
    ]
    balance_history = build_balance_history(all_obligations, request.user, months=12)
    balance_history_payload = _balance_history_chart_payload(balance_history['points'])
    rows = [_obligation_row(obligation, request.user) for obligation in open_obligations]
    i_owe = sum(row['balance_units'] for row in rows if row['role'] == 'borrower')
    owed_to_me = sum(row['balance_units'] for row in rows if row['role'] == 'creditor')
    recent_transactions = (
        LedgerTransaction.objects.filter(obligation__in=open_obligations)
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
            'balance_history': balance_history,
            'balance_history_chart_payload': balance_history_payload,
            'balance_history_latest_net_class': 'positive' if balance_history['latest_net_units'] >= 0 else 'negative',
        },
    )


@login_required
def profile(request):
    profile_obj, _ = UserProfile.objects.get_or_create(user=request.user)
    if _should_refresh_telegram_identity(profile_obj):
        _refresh_telegram_identity(profile_obj)

    profile_form = UserProfileForm(instance=profile_obj)
    password_form = PasswordChangeForm(request.user)
    show_telegram_form = not profile_obj.telegram_id
    show_password_form = False

    if request.method == 'POST':
        if 'profile_submit' in request.POST:
            show_telegram_form = True
            profile_form = UserProfileForm(request.POST, instance=profile_obj)
            if profile_form.is_valid():
                profile_obj = profile_form.save()
                _refresh_telegram_identity(profile_obj)
                messages.success(request, 'Profile was updated.')
                return redirect('ledger:profile')
        elif 'password_submit' in request.POST:
            show_password_form = True
            password_form = PasswordChangeForm(request.user, request.POST)
            if password_form.is_valid():
                user = password_form.save()
                update_session_auth_hash(request, user)
                messages.success(request, 'Password was changed.')
                return redirect('ledger:profile')

    return render(
        request,
        'ledger/profile.html',
        {
            'profile_obj': profile_obj,
            'profile_form': profile_form,
            'password_form': password_form,
            'show_telegram_form': show_telegram_form,
            'show_password_form': show_password_form,
        },
    )


def _should_refresh_telegram_identity(profile):
    if not profile.telegram_id:
        return False
    if not profile.telegram_checked_at:
        return True
    if profile.telegram_lookup_error == 'Telegram bot token is not configured.':
        return True
    return profile.telegram_checked_at < timezone.now() - TELEGRAM_LOOKUP_TTL


def _refresh_telegram_identity(profile):
    if not profile.telegram_id:
        _clear_telegram_identity(profile)
        return

    try:
        identity = get_telegram_chat_identity(profile.telegram_id)
    except TelegramLookupError as error:
        profile.telegram_chat_type = ''
        profile.telegram_username = ''
        profile.telegram_first_name = ''
        profile.telegram_last_name = ''
        profile.telegram_title = ''
        profile.telegram_lookup_error = str(error)
    else:
        profile.telegram_chat_type = identity.chat_type
        profile.telegram_username = identity.username
        profile.telegram_first_name = identity.first_name
        profile.telegram_last_name = identity.last_name
        profile.telegram_title = identity.title
        profile.telegram_lookup_error = ''
    profile.telegram_checked_at = timezone.now()
    profile.save(update_fields=TELEGRAM_IDENTITY_UPDATE_FIELDS)


def _clear_telegram_identity(profile):
    profile.telegram_chat_type = ''
    profile.telegram_username = ''
    profile.telegram_first_name = ''
    profile.telegram_last_name = ''
    profile.telegram_title = ''
    profile.telegram_lookup_error = ''
    profile.telegram_checked_at = None
    profile.save(update_fields=TELEGRAM_IDENTITY_UPDATE_FIELDS)


@login_required
def planner(request):
    obligations_queryset = (
        related_obligations(request.user)
        .filter(status=Obligation.Status.OPEN)
        .select_related('borrower', 'creditor', 'category')
        .order_by('title')
    )
    obligations = list(obligations_queryset)
    horizon_form = PlannerHorizonForm(request.GET or None)
    projection_months = 12
    if horizon_form.is_valid():
        projection_months = horizon_form.cleaned_data['projection_months']
    else:
        horizon_form = PlannerHorizonForm(initial={'projection_months': projection_months})

    portfolio_projection = build_portfolio_projection(
        obligations,
        request.user,
        months=projection_months,
    )
    simulator_result = None
    simulator_form_initial = {
        'obligation': obligations[0].pk if obligations else None,
        'payment_day': 1,
        'simulation_months': 60,
    }
    simulator_form = PayoffSimulatorForm(
        request.GET if request.GET.get('simulate') else None,
        obligations=obligations_queryset,
        initial=simulator_form_initial,
    )
    if request.GET.get('simulate') and simulator_form.is_valid():
        simulator_result = simulate_monthly_payment(
            simulator_form.cleaned_data['obligation'],
            monthly_payment_units=simulator_form.monthly_payment_units,
            payment_day=simulator_form.cleaned_data['payment_day'],
            months=simulator_form.cleaned_data['simulation_months'],
        )

    return render(
        request,
        'ledger/planner.html',
        {
            'horizon_form': horizon_form,
            'simulator_form': simulator_form,
            'simulator_result': simulator_result,
            'portfolio_projection': portfolio_projection,
            'planner_rows': portfolio_projection['rows'],
            'chart_payload': _planner_chart_payload(portfolio_projection['points']),
            'current_net_class': 'positive' if portfolio_projection['current_net_units'] >= 0 else 'negative',
            'projected_net_class': 'positive' if portfolio_projection['projected_net_units'] >= 0 else 'negative',
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
    financial_events_queryset = FinancialEvent.objects.filter(obligation=obligation).order_by('-event_date')
    manual_transfers = _manual_transfer_events(obligation)
    event_series = (
        EventSeries.objects.filter(obligation=obligation)
        .prefetch_related('versions')
        .order_by('name')
    )
    activity_total = financial_events_queryset.count()
    context = {
        'obligation': obligation,
        'balance_units': get_obligation_balance(obligation),
        'role': _role_for(obligation, request.user),
        'activity_rows': [
            _activity_row(event, request.user)
            for event in financial_events_queryset.select_related('obligation')[:HISTORY_PREVIEW_LIMIT]
        ],
        'activity_title': 'Recent activity',
        'activity_preview': True,
        'activity_total': activity_total,
        'activity_has_more': activity_total > HISTORY_PREVIEW_LIMIT,
        'manual_transfer_rows': [_manual_transfer_row(event, request.user) for event in manual_transfers],
        'event_series_rows': [_event_series_row(series) for series in event_series],
        'interest_rates': InterestRatePeriod.objects.filter(obligation=obligation).order_by('-effective_from'),
    }
    return render(request, 'ledger/obligation_detail.html', context)


@login_required
def obligation_history(request, pk):
    obligation = get_related_obligation(request.user, pk)
    financial_events = FinancialEvent.objects.filter(obligation=obligation).select_related('obligation').order_by('-event_date')
    activity_total = financial_events.count()
    return render(
        request,
        'ledger/obligation_history.html',
        {
            'obligation': obligation,
            'activity_rows': [_activity_row(event, request.user) for event in financial_events],
            'activity_title': 'Activity history',
            'activity_preview': False,
            'activity_total': activity_total,
            'activity_has_more': False,
        },
    )


@login_required
def obligation_accounting_history(request, pk):
    obligation = get_related_obligation(request.user, pk)
    ledger_entries = (
        LedgerEntry.objects.filter(account__obligation=obligation)
        .select_related('transaction', 'account')
        .order_by('-effective_date', '-created_at')
    )
    financial_events = FinancialEvent.objects.filter(obligation=obligation).order_by('-event_date')
    interest_runs = InterestAccrualRun.objects.filter(obligation=obligation).order_by('-period_start', '-revision')
    return render(
        request,
        'ledger/obligation_accounting_history.html',
        {
            'obligation': obligation,
            'ledger_entries': ledger_entries,
            'ledger_entries_total': ledger_entries.count(),
            'financial_events': financial_events,
            'financial_events_total': financial_events.count(),
            'interest_runs': interest_runs,
            'interest_runs_total': interest_runs.count(),
            'history_preview': False,
        },
    )


@login_required
def obligation_create(request):
    if request.method == 'POST':
        form = CreateObligationForm(request.POST, user=request.user)
        if form.is_valid():
            creditor, borrower = form.get_participants()
            try:
                with transaction.atomic():
                    category = form.cleaned_data.get('category')
                    obligation = Obligation(
                        creditor=creditor,
                        borrower=borrower,
                        title=form.cleaned_data['title'],
                        category=category,
                        opened_on=form.cleaned_data['opened_on'],
                    )
                    obligation.full_clean()
                    obligation.save()
                    post_principal_advance(
                        obligation,
                        amount_units=form.amount_units,
                        event_date=form.cleaned_data['opened_on'],
                        memo=form.cleaned_data.get('memo', ''),
                        category=category.name if category else '',
                    )
                    form.save_recurring_series(obligation)
                    form.save_interest_rate(obligation)
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
def manual_transfer_update(request, pk, event_pk):
    obligation = get_related_obligation(request.user, pk)
    if obligation.status != Obligation.Status.OPEN:
        messages.error(request, 'Manual transfers can only be edited while the obligation is open.')
        return redirect('ledger:obligation_detail', pk=obligation.pk)
    transfer = get_object_or_404(_manual_transfer_events(obligation), pk=event_pk)
    initial = {
        'transfer_type': transfer.event_type,
        'event_date': transfer.event_date,
        'amount': decimal_from_units(transfer.amount_units),
        'category': transfer.category,
        'memo': transfer.memo,
    }
    if request.method == 'POST':
        form = ManualTransferForm(request.POST)
        if form.is_valid():
            try:
                result = edit_manual_transfer(
                    transfer,
                    event_type=form.cleaned_data['transfer_type'],
                    amount_units=form.amount_units,
                    event_date=form.cleaned_data['event_date'],
                    category=form.cleaned_data.get('category', ''),
                    memo=form.cleaned_data.get('memo', ''),
                )
                if result['changed']:
                    messages.success(request, 'Manual transfer was updated.')
                else:
                    messages.success(request, 'Manual transfer was already current.')
                return redirect('ledger:obligation_detail', pk=obligation.pk)
            except ValidationError as error:
                form.add_error(None, error)
    else:
        form = ManualTransferForm(initial=initial)

    return render(
        request,
        'ledger/form.html',
        {
            'title': f'Edit manual transfer: {obligation.title}',
            'form': form,
            'submit_label': 'Save transfer',
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
        form = RecurringChargeForm(initial={'starts_on': obligation.opened_on})
    return render(
        request,
        'ledger/form.html',
        {
            'title': f'New recurring event: {obligation.title}',
            'form': form,
            'submit_label': 'Create recurring event',
            'back_url': reverse('ledger:obligation_detail', kwargs={'pk': obligation.pk}),
        },
    )


@login_required
def recurring_series_update(request, pk, series_pk):
    obligation = get_related_obligation(request.user, pk)
    series = get_object_or_404(EventSeries, obligation=obligation, pk=series_pk)
    if request.method == 'POST':
        form = RecurringSeriesUpdateForm(request.POST, instance=series)
        if form.is_valid():
            try:
                with transaction.atomic():
                    form.save()
                return redirect('ledger:obligation_detail', pk=obligation.pk)
            except ValidationError as error:
                form.add_error(None, error)
    else:
        form = RecurringSeriesUpdateForm(instance=series)
    return render(
        request,
        'ledger/form.html',
        {
            'title': f'Edit recurring event: {series.name}',
            'form': form,
            'submit_label': 'Save recurring event',
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
        form = InterestRatePeriodForm(initial={'effective_from': obligation.opened_on})
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
def interest_rate_update(request, pk, rate_pk):
    obligation = get_related_obligation(request.user, pk)
    rate = get_object_or_404(InterestRatePeriod, obligation=obligation, pk=rate_pk)
    if request.method == 'POST':
        form = InterestRatePeriodForm(request.POST, instance=rate)
        if form.is_valid():
            try:
                form.save_for_obligation(obligation)
                return redirect('ledger:obligation_detail', pk=obligation.pk)
            except ValidationError as error:
                form.add_error(None, error)
    else:
        form = InterestRatePeriodForm(instance=rate)
    return render(
        request,
        'ledger/form.html',
        {
            'title': f'Edit interest rate: {obligation.title}',
            'form': form,
            'submit_label': 'Save rate',
            'back_url': reverse('ledger:obligation_detail', kwargs={'pk': obligation.pk}),
        },
    )


@login_required
@require_POST
def obligation_recalculate(request, pk):
    obligation = get_related_obligation(request.user, pk)
    try:
        result = recalculate_obligation(obligation)
        recurring_result = result['recurring']
        interest_result = result['interest']
        messages.success(
            request,
            (
                f"Recalculated from {result['from_date']}: "
                f"reversed {len(recurring_result['reversed_events'])} recurring event(s), "
                f"generated {len(recurring_result['created_transactions'])} recurring event(s), "
                f"reversed {len(interest_result['reversed_runs'])} interest month(s), and "
                f"posted {len(interest_result['posted_runs'])} interest month(s). "
                f"{len(interest_result['unchanged_runs'])} interest month(s) were already current."
            ),
        )
    except ValidationError as error:
        messages.error(request, error.message if hasattr(error, 'message') else str(error))
    return redirect('ledger:obligation_detail', pk=obligation.pk)


@login_required
@require_POST
def interest_due_generate(request, pk):
    obligation = get_related_obligation(request.user, pk)
    posted_runs = generate_due_interest(obligation=obligation)
    messages.success(request, f'Posted {len(posted_runs)} due interest month(s).')
    return redirect('ledger:obligation_detail', pk=obligation.pk)


@login_required
def interest_recalculate(request, pk):
    obligation = get_related_obligation(request.user, pk)
    if request.method == 'POST':
        form = InterestRecalculateForm(request.POST)
        if form.is_valid():
            try:
                result = recalculate_interest_from(obligation, form.cleaned_data['from_date'])
                messages.success(
                    request,
                    (
                        f"Reversed {len(result['reversed_runs'])} old interest month(s) and "
                        f"posted {len(result['posted_runs'])} recalculated month(s). "
                        f"{len(result['unchanged_runs'])} interest month(s) were already current."
                    ),
                )
                return redirect('ledger:obligation_detail', pk=obligation.pk)
            except ValidationError as error:
                form.add_error(None, error)
    else:
        form = InterestRecalculateForm(initial={'from_date': obligation.opened_on})
    return render(
        request,
        'ledger/form.html',
        {
            'title': f'Recalculate interest: {obligation.title}',
            'form': form,
            'submit_label': 'Recalculate interest',
            'back_url': reverse('ledger:obligation_detail', kwargs={'pk': obligation.pk}),
        },
    )


@login_required
def recurring_recalculate(request, pk):
    obligation = get_related_obligation(request.user, pk)
    if request.method == 'POST':
        form = RecurringRecalculateForm(request.POST)
        if form.is_valid():
            try:
                result = recalculate_due_recurring_events(obligation, form.cleaned_data['from_date'])
                messages.success(
                    request,
                    (
                        f"Reversed {len(result['reversed_events'])} no-longer-due recurring event(s) and "
                        f"generated {len(result['created_transactions'])} missing recurring event(s)."
                    ),
                )
                return redirect('ledger:obligation_detail', pk=obligation.pk)
            except ValidationError as error:
                form.add_error(None, error)
    else:
        form = RecurringRecalculateForm(initial={'from_date': obligation.opened_on})
    return render(
        request,
        'ledger/form.html',
        {
            'title': f'Recalculate recurring events: {obligation.title}',
            'form': form,
            'submit_label': 'Recalculate recurring events',
            'back_url': reverse('ledger:obligation_detail', kwargs={'pk': obligation.pk}),
        },
    )


@login_required
@require_POST
def recurring_due_generate(request, pk):
    obligation = get_related_obligation(request.user, pk)
    try:
        created_transactions = generate_due_recurring_events(obligation=obligation)
        messages.success(request, f'Generated {len(created_transactions)} due recurring event(s).')
    except ValidationError as error:
        messages.error(request, error.message if hasattr(error, 'message') else str(error))
    return redirect('ledger:obligation_detail', pk=obligation.pk)


@login_required
@require_POST
def obligation_close(request, pk):
    obligation = get_related_obligation(request.user, pk)
    if request.POST.get('stop_tracking_confirmation') != STOP_TRACKING_CONFIRMATION:
        messages.error(request, 'Type STOP to confirm stopping this obligation.')
        return redirect('ledger:obligation_detail', pk=obligation.pk)

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


def _event_series_row(series):
    version = _event_series_version_for_display(series)
    return {
        'series': series,
        'current_amount_units': version.amount_units if version else None,
        'schedule_label': _event_series_schedule_label(series),
    }


def _manual_transfer_events(obligation):
    return (
        FinancialEvent.objects.filter(
            obligation=obligation,
            source=FinancialEvent.Source.MANUAL,
            event_type__in=[
                FinancialEvent.EventType.PRINCIPAL_ADVANCE,
                FinancialEvent.EventType.REPAYMENT,
            ],
            voided_at__isnull=True,
        )
        .select_related('obligation')
        .order_by('-event_date', '-created_at')
    )


def _manual_transfer_row(event, user):
    signed_amount_units = _signed_event_amount_units(event, user)
    return {
        'event': event,
        'label': _activity_label(event, user),
        'signed_amount_units': signed_amount_units,
        'amount_class': 'positive' if signed_amount_units >= 0 else 'negative',
        'details': _truncate_activity_details(event.memo),
    }


def _event_series_version_for_display(series):
    today = timezone.localdate()
    return (
        series.versions.filter(valid_from__lte=today)
        .filter(Q(valid_to__isnull=True) | Q(valid_to__gte=today))
        .order_by('-valid_from')
        .first()
        or series.versions.order_by('-valid_from').first()
    )


def _event_series_schedule_label(series):
    if series.frequency == EventSeries.Frequency.MONTHLY:
        return f'{series.get_frequency_display()} on day {series.day_of_month}'
    return f'{series.get_frequency_display()} on {_weekday_name(series.day_of_week)}'


def _activity_row(event, user):
    signed_amount_units = _signed_event_amount_units(event, user)
    return {
        'event': event,
        'label': _activity_label(event, user),
        'signed_amount_units': signed_amount_units,
        'amount_class': 'positive' if signed_amount_units >= 0 else 'negative',
        'category': event.category,
        'details': _truncate_activity_details(event.memo),
    }


def _signed_event_amount_units(event, user):
    user_is_borrower = event.obligation.borrower_id == user.id
    if event.direction == FinancialEvent.Direction.INCREASES_DEBT:
        return event.amount_units if user_is_borrower else -event.amount_units
    return -event.amount_units if user_is_borrower else event.amount_units


def _activity_label(event, user):
    user_is_borrower = event.obligation.borrower_id == user.id
    if event.event_type == FinancialEvent.EventType.PRINCIPAL_ADVANCE:
        return 'You borrowed' if user_is_borrower else 'You lent'
    if event.event_type == FinancialEvent.EventType.REPAYMENT:
        return 'You paid' if user_is_borrower else 'You received'
    if event.event_type == FinancialEvent.EventType.SCHEDULED_CHARGE:
        return 'Charge added'
    if event.event_type == FinancialEvent.EventType.INTEREST_POSTING:
        return 'Interest added'
    if event.event_type == FinancialEvent.EventType.ADJUSTMENT:
        if event.category == 'recurring_reversal':
            return 'Recurring event reversed'
        return 'Adjustment'
    return event.get_event_type_display()


def _weekday_name(day_of_week):
    names = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']
    if day_of_week is None:
        return '-'
    return names[int(day_of_week)]


def _role_for(obligation, user):
    if obligation.borrower_id == user.id:
        return 'borrower'
    return 'creditor'


def _truncate_activity_details(value, limit=50):
    value = (value or '').strip()
    if len(value) <= limit:
        return value
    return f'{value[:limit - 3]}...'


def _planner_chart_payload(points):
    return {
        'labels': [point['label'] for point in points],
        'values': [round(point['net_units'] / 10000, 2) for point in points],
    }


def _balance_history_chart_payload(points):
    return {
        'labels': [point['label'] for point in points],
        'values': [round(point['net_units'] / 10000, 2) for point in points],
        'iOweValues': [round(point['i_owe_units'] / 10000, 2) for point in points],
        'owedToMeValues': [round(point['owed_to_me_units'] / 10000, 2) for point in points],
    }
