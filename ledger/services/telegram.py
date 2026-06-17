from dataclasses import dataclass
import json
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from django.conf import settings


TELEGRAM_API_BASE_URL = 'https://api.telegram.org'


class TelegramLookupError(Exception):
    pass


@dataclass(frozen=True)
class TelegramChatIdentity:
    chat_id: int
    chat_type: str = ''
    username: str = ''
    first_name: str = ''
    last_name: str = ''
    title: str = ''


def get_telegram_chat_identity(chat_id):
    token = getattr(settings, 'TELEGRAM_BOT_TOKEN', '')
    if not token:
        raise TelegramLookupError('Telegram bot token is not configured.')

    query = urlencode({'chat_id': chat_id})
    request = Request(f'{TELEGRAM_API_BASE_URL}/bot{token}/getChat?{query}')
    try:
        with urlopen(request, timeout=5) as response:
            payload = json.loads(response.read().decode('utf-8'))
    except HTTPError as error:
        raise TelegramLookupError(_telegram_error_message(error)) from error
    except (OSError, URLError, json.JSONDecodeError) as error:
        raise TelegramLookupError('Telegram lookup failed.') from error

    if not payload.get('ok'):
        raise TelegramLookupError(payload.get('description') or 'Telegram lookup failed.')

    chat = payload.get('result') or {}
    return TelegramChatIdentity(
        chat_id=chat.get('id') or chat_id,
        chat_type=chat.get('type') or '',
        username=chat.get('username') or '',
        first_name=chat.get('first_name') or '',
        last_name=chat.get('last_name') or '',
        title=chat.get('title') or '',
    )


def _telegram_error_message(error):
    try:
        payload = json.loads(error.read().decode('utf-8'))
    except (OSError, json.JSONDecodeError):
        return 'Telegram lookup failed.'
    return payload.get('description') or 'Telegram lookup failed.'
