from decimal import Decimal, ROUND_HALF_UP

from ledger.models import DEFAULT_CURRENCY, DEFAULT_CURRENCY_EXPONENT


UNIT_SCALE = Decimal(10) ** DEFAULT_CURRENCY_EXPONENT


def units_from_decimal(amount):
    value = Decimal(str(amount))
    return int((value * UNIT_SCALE).quantize(Decimal('1'), rounding=ROUND_HALF_UP))


def decimal_from_units(amount_units):
    return Decimal(amount_units) / UNIT_SCALE


def validate_usd_units(currency, currency_exponent):
    return currency == DEFAULT_CURRENCY and currency_exponent == DEFAULT_CURRENCY_EXPONENT
