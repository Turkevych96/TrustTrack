from django import template

from ledger.services.money import decimal_from_units


register = template.Library()


@register.filter
def money_units(value):
    if value is None:
        value = 0
    return f'${decimal_from_units(value):,.4f}'


@register.filter
def user_label(user):
    if user is None:
        return ''
    return user.get_full_name() or user.get_username()
