from ledger.models import LedgerAccount, LedgerEntry, LedgerTransaction


def get_obligation_balance(obligation, as_of=None):
    entries = LedgerEntry.objects.filter(
        account__obligation=obligation,
        account__account_type=LedgerAccount.AccountType.RECEIVABLE,
        transaction__status=LedgerTransaction.Status.POSTED,
    )
    if as_of is not None:
        entries = entries.filter(effective_date__lte=as_of)

    balance = 0
    for entry in entries.only('side', 'amount_units'):
        if entry.side == LedgerEntry.Side.DEBIT:
            balance += entry.amount_units
        else:
            balance -= entry.amount_units
    return balance
