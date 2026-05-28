from django.core.exceptions import ValidationError
from django.db import transaction as db_transaction
from django.utils import timezone

from ledger.models import (
    AuditEvent,
    EventSeries,
    EventSeriesVersion,
    FinancialEvent,
    LedgerAccount,
    LedgerEntry,
    LedgerTransaction,
    Obligation,
)
from ledger.services.balances import get_obligation_balance


def ensure_obligation_accounts(obligation):
    receivable, _ = LedgerAccount.objects.get_or_create(
        obligation=obligation,
        account_type=LedgerAccount.AccountType.RECEIVABLE,
        defaults={
            'user': obligation.creditor,
            'name': f'{obligation.title} receivable',
            'currency': obligation.currency,
            'currency_exponent': obligation.currency_exponent,
        },
    )
    payable, _ = LedgerAccount.objects.get_or_create(
        obligation=obligation,
        account_type=LedgerAccount.AccountType.PAYABLE,
        defaults={
            'user': obligation.borrower,
            'name': f'{obligation.title} payable',
            'currency': obligation.currency,
            'currency_exponent': obligation.currency_exponent,
        },
    )
    return receivable, payable


def post_principal_advance(obligation, amount_units, event_date, memo='', category='', idempotency_key=None):
    return _post_debt_increase(
        obligation=obligation,
        amount_units=amount_units,
        event_date=event_date,
        event_type=FinancialEvent.EventType.PRINCIPAL_ADVANCE,
        source=FinancialEvent.Source.MANUAL,
        memo=memo,
        category=category,
        idempotency_key=idempotency_key,
    )


def post_scheduled_charge(
    obligation,
    amount_units,
    event_date,
    memo='',
    category='',
    event_series=None,
    event_series_version=None,
    period_start=None,
    period_end=None,
    revision=1,
    idempotency_key=None,
):
    return _post_debt_increase(
        obligation=obligation,
        amount_units=amount_units,
        event_date=event_date,
        event_type=FinancialEvent.EventType.SCHEDULED_CHARGE,
        source=FinancialEvent.Source.GENERATED if event_series else FinancialEvent.Source.MANUAL,
        memo=memo,
        category=category,
        event_series=event_series,
        event_series_version=event_series_version,
        period_start=period_start,
        period_end=period_end,
        revision=revision,
        idempotency_key=idempotency_key,
    )


def post_scheduled_repayment(
    obligation,
    amount_units,
    event_date,
    memo='',
    category='',
    event_series=None,
    event_series_version=None,
    period_start=None,
    period_end=None,
    revision=1,
    idempotency_key=None,
):
    if amount_units > get_obligation_balance(obligation, as_of=event_date):
        raise ValidationError('Scheduled repayment cannot exceed the obligation balance as of the repayment date.')

    return _post_debt_decrease(
        obligation=obligation,
        amount_units=amount_units,
        event_date=event_date,
        event_type=FinancialEvent.EventType.REPAYMENT,
        source=FinancialEvent.Source.GENERATED if event_series else FinancialEvent.Source.MANUAL,
        memo=memo,
        category=category,
        event_series=event_series,
        event_series_version=event_series_version,
        period_start=period_start,
        period_end=period_end,
        revision=revision,
        idempotency_key=idempotency_key,
    )


def post_repayment(obligation, amount_units, event_date, memo='', category='', idempotency_key=None):
    _validate_postable_amount(amount_units)
    if amount_units > get_obligation_balance(obligation, as_of=event_date):
        raise ValidationError('Repayment cannot exceed the obligation balance as of the repayment date.')

    return _post_debt_decrease(
        obligation=obligation,
        amount_units=amount_units,
        event_date=event_date,
        event_type=FinancialEvent.EventType.REPAYMENT,
        source=FinancialEvent.Source.MANUAL,
        memo=memo,
        category=category,
        idempotency_key=idempotency_key,
    )


def post_interest_reversal(obligation, amount_units, event_date, memo='', period_start=None, period_end=None, idempotency_key=None):
    return _post_debt_decrease(
        obligation=obligation,
        amount_units=amount_units,
        event_date=event_date,
        event_type=FinancialEvent.EventType.ADJUSTMENT,
        source=FinancialEvent.Source.SYSTEM,
        memo=memo,
        category='interest_reversal',
        period_start=period_start,
        period_end=period_end,
        idempotency_key=idempotency_key,
    )


def post_recurring_event_reversal(original_event, idempotency_key=None):
    if original_event.source != FinancialEvent.Source.GENERATED or not original_event.event_series_id:
        raise ValidationError('Only generated recurring events can be reversed by recurring recalculation.')

    idempotency_key = idempotency_key or f'recurring-reversal:{original_event.pk}'
    memo = f'Reverse generated {original_event.get_event_type_display()} from {original_event.event_date.isoformat()}'
    with db_transaction.atomic():
        if original_event.direction == FinancialEvent.Direction.INCREASES_DEBT:
            ledger_transaction = _post_debt_decrease(
                obligation=original_event.obligation,
                amount_units=original_event.amount_units,
                event_date=original_event.event_date,
                event_type=FinancialEvent.EventType.ADJUSTMENT,
                source=FinancialEvent.Source.SYSTEM,
                memo=memo,
                category='recurring_reversal',
                period_start=original_event.period_start,
                period_end=original_event.period_end,
                idempotency_key=idempotency_key,
            )
        else:
            ledger_transaction = _post_debt_increase(
                obligation=original_event.obligation,
                amount_units=original_event.amount_units,
                event_date=original_event.event_date,
                event_type=FinancialEvent.EventType.ADJUSTMENT,
                source=FinancialEvent.Source.SYSTEM,
                memo=memo,
                category='recurring_reversal',
                period_start=original_event.period_start,
                period_end=original_event.period_end,
                idempotency_key=idempotency_key,
            )
        if not original_event.voided_at:
            original_event.voided_at = timezone.now()
            original_event.save(update_fields=['voided_at', 'updated_at'])
        return ledger_transaction


def _post_debt_increase(
    obligation,
    amount_units,
    event_date,
    event_type,
    source,
    memo='',
    category='',
    event_series=None,
    event_series_version=None,
    period_start=None,
    period_end=None,
    revision=1,
    idempotency_key=None,
):
    _validate_postable_amount(amount_units)
    with db_transaction.atomic():
        existing = _get_existing_idempotent_transaction(idempotency_key)
        if existing:
            return existing

        receivable, payable = ensure_obligation_accounts(obligation)
        event = _create_financial_event(
            obligation=obligation,
            event_type=event_type,
            source=source,
            event_date=event_date,
            amount_units=amount_units,
            direction=FinancialEvent.Direction.INCREASES_DEBT,
            memo=memo,
            category=category,
            event_series=event_series,
            event_series_version=event_series_version,
            period_start=period_start,
            period_end=period_end,
            revision=revision,
        )
        ledger_transaction = _create_transaction(
            obligation=obligation,
            event=event,
            transaction_type=event_type,
            transaction_date=event_date,
            memo=memo,
            idempotency_key=idempotency_key,
        )
        _create_entry(ledger_transaction, receivable, LedgerEntry.Side.DEBIT, amount_units, event_date, memo)
        _create_entry(ledger_transaction, payable, LedgerEntry.Side.CREDIT, amount_units, event_date, memo)
        ledger_transaction.post()
        _audit(f'{event_type}_posted', obligation, event, {'amount_units': amount_units})
        return ledger_transaction


def _post_debt_decrease(
    obligation,
    amount_units,
    event_date,
    event_type,
    source,
    memo='',
    category='',
    event_series=None,
    event_series_version=None,
    period_start=None,
    period_end=None,
    revision=1,
    idempotency_key=None,
):
    _validate_postable_amount(amount_units)
    with db_transaction.atomic():
        existing = _get_existing_idempotent_transaction(idempotency_key)
        if existing:
            return existing

        receivable, payable = ensure_obligation_accounts(obligation)
        event = _create_financial_event(
            obligation=obligation,
            event_type=event_type,
            source=source,
            event_date=event_date,
            amount_units=amount_units,
            direction=FinancialEvent.Direction.DECREASES_DEBT,
            memo=memo,
            category=category,
            event_series=event_series,
            event_series_version=event_series_version,
            period_start=period_start,
            period_end=period_end,
            revision=revision,
        )
        ledger_transaction = _create_transaction(
            obligation=obligation,
            event=event,
            transaction_type=event_type,
            transaction_date=event_date,
            memo=memo,
            idempotency_key=idempotency_key,
        )
        _create_entry(ledger_transaction, payable, LedgerEntry.Side.DEBIT, amount_units, event_date, memo)
        _create_entry(ledger_transaction, receivable, LedgerEntry.Side.CREDIT, amount_units, event_date, memo)
        ledger_transaction.post()
        _audit(f'{event_type}_posted', obligation, event, {'amount_units': amount_units})
        return ledger_transaction


def _create_financial_event(
    obligation,
    event_type,
    source,
    event_date,
    amount_units,
    direction,
    memo='',
    category='',
    event_series=None,
    event_series_version=None,
    period_start=None,
    period_end=None,
    revision=1,
):
    event = FinancialEvent(
        obligation=obligation,
        event_type=event_type,
        source=source,
        event_date=event_date,
        amount_units=amount_units,
        currency=obligation.currency,
        currency_exponent=obligation.currency_exponent,
        direction=direction,
        memo=memo,
        category=category,
        event_series=event_series,
        event_series_version=event_series_version,
        period_start=period_start,
        period_end=period_end,
        revision=revision,
    )
    event.full_clean()
    event.save()
    return event


def _create_transaction(obligation, event, transaction_type, transaction_date, memo='', idempotency_key=None):
    ledger_transaction = LedgerTransaction(
        obligation=obligation,
        financial_event=event,
        transaction_type=transaction_type,
        transaction_date=transaction_date,
        idempotency_key=idempotency_key,
        memo=memo,
    )
    ledger_transaction.full_clean()
    ledger_transaction.save()
    return ledger_transaction


def _create_entry(ledger_transaction, account, side, amount_units, effective_date, memo=''):
    entry = LedgerEntry(
        transaction=ledger_transaction,
        account=account,
        entry_type=ledger_transaction.transaction_type,
        effective_date=effective_date,
        side=side,
        amount_units=amount_units,
        currency=account.currency,
        currency_exponent=account.currency_exponent,
        memo=memo,
    )
    entry.full_clean()
    entry.save()
    return entry


def _get_existing_idempotent_transaction(idempotency_key):
    if not idempotency_key:
        return None
    return LedgerTransaction.objects.filter(idempotency_key=idempotency_key).first()


def _validate_postable_amount(amount_units):
    if amount_units <= 0:
        raise ValidationError('Amount must be greater than zero.')


def _audit(event_type, obligation, financial_event, payload):
    AuditEvent.objects.create(
        event_type=event_type,
        obligation=obligation,
        financial_event=financial_event,
        payload=payload,
    )
