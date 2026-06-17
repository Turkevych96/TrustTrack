from decimal import Decimal, ROUND_HALF_UP

from django import template
from django.core.exceptions import ObjectDoesNotExist

from ledger.services.money import decimal_from_units


register = template.Library()


@register.filter
def money_units(value):
    if value is None:
        value = 0
    amount = decimal_from_units(value).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    return f'${amount:,.2f}'


@register.filter
def signed_money_units(value):
    if value is None:
        value = 0
    sign = '+' if value >= 0 else '-'
    amount = decimal_from_units(abs(value)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    return f'{sign}${amount:,.2f}'


@register.filter
def user_label(user):
    if user is None:
        return ''
    return user.get_full_name() or user.get_username()


@register.filter
def module_enabled(user, setting_name):
    if user is None or not user.is_authenticated:
        return False
    try:
        profile = user.trusttrack_profile
    except ObjectDoesNotExist:
        return True
    return getattr(profile, setting_name, True)
