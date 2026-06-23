from dataclasses import dataclass
from datetime import timedelta
import secrets
from urllib.parse import quote

from django.conf import settings
from django.db import IntegrityError, transaction
from django.urls import reverse
from django.utils import timezone
import qrcode
from qrcode.image.svg import SvgPathImage

from ledger.models import TelegramLoginChallenge, UserProfile


CHALLENGE_SESSION_KEY = 'telegram_login_challenge_token'
DEEP_LINK_PREFIX = 'login_'
LOGIN_CODE_ALPHABET = 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789'
LOGIN_CODE_LENGTH = 8


@dataclass(frozen=True)
class TelegramLoginConfirmation:
    challenge: TelegramLoginChallenge | None
    user: object | None
    status: str


def get_or_create_session_challenge(request):
    token = request.session.get(CHALLENGE_SESSION_KEY)
    challenge = _usable_challenge_for_token(token) if token else None
    if challenge:
        return challenge

    challenge = create_login_challenge()
    request.session[CHALLENGE_SESSION_KEY] = challenge.token
    request.session.modified = True
    return challenge


def create_login_challenge(now=None):
    now = now or timezone.now()
    expires_at = now + timedelta(minutes=getattr(settings, 'TELEGRAM_LOGIN_CHALLENGE_TTL_MINUTES', 5))
    for _ in range(20):
        try:
            return TelegramLoginChallenge.objects.create(
                token=secrets.token_urlsafe(24),
                code=_generate_login_code(),
                expires_at=expires_at,
            )
        except IntegrityError:
            continue
    raise RuntimeError('Could not create a unique Telegram login challenge.')


def challenge_status_for_session(request):
    token = request.session.get(CHALLENGE_SESSION_KEY)
    challenge = TelegramLoginChallenge.objects.filter(token=token).select_related('user').first()
    if not challenge:
        return {'status': 'missing'}
    if challenge.is_consumed:
        return {'status': 'consumed'}
    if challenge.is_expired():
        return {'status': 'expired'}
    if challenge.is_confirmed:
        return {'status': 'confirmed', 'user': challenge.user}
    return {'status': 'pending'}


def consume_confirmed_session_challenge(request):
    token = request.session.get(CHALLENGE_SESSION_KEY)
    if not token:
        return None

    with transaction.atomic():
        challenge = (
            TelegramLoginChallenge.objects
            .select_for_update()
            .select_related('user')
            .filter(token=token)
            .first()
        )
        if (
            not challenge
            or challenge.is_consumed
            or challenge.is_expired()
            or not challenge.is_confirmed
            or not challenge.user.is_active
        ):
            return None
        challenge.consumed_at = timezone.now()
        challenge.save(update_fields=['consumed_at', 'updated_at'])

    request.session.pop(CHALLENGE_SESSION_KEY, None)
    request.session.modified = True
    return challenge.user


def confirm_challenge_by_start_payload(payload, telegram_user_id):
    payload = (payload or '').strip()
    if not payload.startswith(DEEP_LINK_PREFIX):
        return TelegramLoginConfirmation(None, None, 'unsupported')
    return confirm_challenge_by_token(payload[len(DEEP_LINK_PREFIX):], telegram_user_id)


def confirm_challenge_by_token(token, telegram_user_id):
    return _confirm_challenge(
        TelegramLoginChallenge.objects.filter(token=(token or '').strip()).first(),
        telegram_user_id,
    )


def confirm_challenge_by_code(code, telegram_user_id):
    normalized_code = normalize_login_code(code)
    return _confirm_challenge(
        TelegramLoginChallenge.objects.filter(code=normalized_code).first(),
        telegram_user_id,
    )


def normalize_login_code(code):
    return ''.join(char for char in str(code or '').upper() if char.isalnum())


def telegram_login_command(challenge):
    return f'/login {format_login_code(challenge.code)}'


def format_login_code(code):
    code = normalize_login_code(code)
    if len(code) <= 4:
        return code
    return f'{code[:4]}-{code[4:]}'


def telegram_deep_link(challenge):
    bot_username = getattr(settings, 'TELEGRAM_BOT_USERNAME', '').strip().lstrip('@')
    if not bot_username:
        return ''
    return f'https://t.me/{bot_username}?start={DEEP_LINK_PREFIX}{challenge.token}'


def telegram_login_page_context(request, challenge):
    deep_link = telegram_deep_link(challenge)
    return {
        'challenge': challenge,
        'telegram_login_command': telegram_login_command(challenge),
        'telegram_deep_link': deep_link,
        'telegram_qr_svg_data_uri': qr_svg_data_uri(deep_link) if deep_link else '',
        'telegram_bot_username': getattr(settings, 'TELEGRAM_BOT_USERNAME', '').strip().lstrip('@'),
        'status_url': reverse('telegram_login_status'),
        'restart_url': reverse('telegram_login_restart'),
    }


def qr_svg_data_uri(value):
    qr = qrcode.QRCode(
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=8,
        border=2,
    )
    qr.add_data(value)
    qr.make(fit=True)
    image = qr.make_image(image_factory=SvgPathImage)
    svg = image.to_string(encoding='unicode')
    return f'data:image/svg+xml;charset=utf-8,{quote(svg)}'


def _confirm_challenge(challenge, telegram_user_id):
    profile = _active_profile_for_telegram_id(telegram_user_id)
    if not profile:
        return TelegramLoginConfirmation(challenge, None, 'access_denied')
    if not challenge:
        return TelegramLoginConfirmation(None, profile.user, 'not_found')

    with transaction.atomic():
        challenge = (
            TelegramLoginChallenge.objects
            .select_for_update()
            .select_related('user')
            .get(pk=challenge.pk)
        )
        if challenge.is_consumed:
            return TelegramLoginConfirmation(challenge, profile.user, 'consumed')
        if challenge.is_expired():
            return TelegramLoginConfirmation(challenge, profile.user, 'expired')
        if challenge.is_confirmed:
            return TelegramLoginConfirmation(challenge, challenge.user, 'confirmed')

        challenge.user = profile.user
        challenge.telegram_id = telegram_user_id
        challenge.confirmed_at = timezone.now()
        challenge.save(update_fields=['user', 'telegram_id', 'confirmed_at', 'updated_at'])
    return TelegramLoginConfirmation(challenge, profile.user, 'confirmed')


def _active_profile_for_telegram_id(telegram_user_id):
    return (
        UserProfile.objects
        .select_related('user')
        .filter(telegram_id=telegram_user_id, user__is_active=True)
        .first()
    )


def _usable_challenge_for_token(token):
    challenge = TelegramLoginChallenge.objects.filter(token=token).first()
    if not challenge or challenge.is_consumed or challenge.is_expired():
        return None
    return challenge


def _generate_login_code():
    raw_code = ''.join(secrets.choice(LOGIN_CODE_ALPHABET) for _ in range(LOGIN_CODE_LENGTH))
    return normalize_login_code(raw_code)
