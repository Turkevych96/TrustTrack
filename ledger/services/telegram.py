from dataclasses import dataclass
import json
from urllib.error import HTTPError, URLError
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
    payload = telegram_api_request('getChat', {'chat_id': chat_id})
    chat = payload.get('result') or {}
    return TelegramChatIdentity(
        chat_id=chat.get('id') or chat_id,
        chat_type=chat.get('type') or '',
        username=chat.get('username') or '',
        first_name=chat.get('first_name') or '',
        last_name=chat.get('last_name') or '',
        title=chat.get('title') or '',
    )


def get_telegram_updates(offset=None, timeout=30, allowed_updates=None):
    params = {'timeout': timeout}
    if offset is not None:
        params['offset'] = offset
    if allowed_updates is not None:
        params['allowed_updates'] = allowed_updates
    payload = telegram_api_request('getUpdates', params, timeout=timeout + 5)
    return payload.get('result') or []


def send_telegram_message(chat_id, text, reply_markup=None):
    params = {
        'chat_id': chat_id,
        'text': text,
        'disable_web_page_preview': True,
    }
    if reply_markup:
        params['reply_markup'] = reply_markup
    return telegram_api_request('sendMessage', params).get('result')


def edit_telegram_message(chat_id, message_id, text, reply_markup=None):
    params = {
        'chat_id': chat_id,
        'message_id': message_id,
        'text': text,
        'disable_web_page_preview': True,
    }
    if reply_markup:
        params['reply_markup'] = reply_markup
    return telegram_api_request('editMessageText', params).get('result')


def answer_telegram_callback_query(callback_query_id, text=''):
    params = {'callback_query_id': callback_query_id}
    if text:
        params['text'] = text
    return telegram_api_request('answerCallbackQuery', params).get('result')


def telegram_api_request(method, params=None, timeout=5):
    token = getattr(settings, 'TELEGRAM_BOT_TOKEN', '')
    if not token:
        raise TelegramLookupError('Telegram bot token is not configured.')

    body = json.dumps(params or {}).encode('utf-8')
    request = Request(
        f'{TELEGRAM_API_BASE_URL}/bot{token}/{method}',
        data=body,
        headers={'Content-Type': 'application/json'},
        method='POST',
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode('utf-8'))
    except HTTPError as error:
        raise TelegramLookupError(_telegram_error_message(error)) from error
    except (OSError, URLError, json.JSONDecodeError) as error:
        raise TelegramLookupError('Telegram lookup failed.') from error

    if not payload.get('ok'):
        raise TelegramLookupError(payload.get('description') or 'Telegram lookup failed.')
    return payload


def _telegram_error_message(error):
    try:
        payload = json.loads(error.read().decode('utf-8'))
    except (OSError, json.JSONDecodeError):
        return 'Telegram lookup failed.'
    return payload.get('description') or 'Telegram lookup failed.'
