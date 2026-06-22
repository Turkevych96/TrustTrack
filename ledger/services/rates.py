from decimal import Decimal


MONTHS_PER_YEAR = Decimal('12')
PERCENT_SCALE = Decimal('100')
ONE = Decimal('1')


def monthly_rate_from_annual_yield_percent(annual_yield_percent):
    if annual_yield_percent <= 0:
        return Decimal('0')
    annual_yield = Decimal(annual_yield_percent) / PERCENT_SCALE
    return (ONE + annual_yield) ** (ONE / MONTHS_PER_YEAR) - ONE


def daily_rate_from_annual_yield_percent(annual_yield_percent, days_in_month):
    if days_in_month <= 0:
        raise ValueError('days_in_month must be greater than zero.')
    monthly_rate = monthly_rate_from_annual_yield_percent(annual_yield_percent)
    return monthly_rate / Decimal(days_in_month)
