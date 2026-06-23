from django.core.management.base import BaseCommand

from ledger.services.telegram import (
    TelegramLookupError,
    answer_telegram_callback_query,
    delete_telegram_message,
    edit_telegram_message,
    get_telegram_updates,
    send_telegram_message,
)
from ledger.services.telegram_bot import process_telegram_update


class Command(BaseCommand):
    help = 'Run the TrustTrack Telegram bot with long polling.'

    def add_arguments(self, parser):
        parser.add_argument('--once', action='store_true', help='Process one polling batch and exit.')
        parser.add_argument('--timeout', type=int, default=30, help='Telegram long-poll timeout in seconds.')

    def handle(self, *args, **options):
        offset = None
        timeout = options['timeout']
        allowed_updates = ['message', 'callback_query']
        self.stdout.write(self.style.SUCCESS('TrustTrack Telegram bot polling started.'))

        while True:
            try:
                updates = get_telegram_updates(
                    offset=offset,
                    timeout=timeout,
                    allowed_updates=allowed_updates,
                )
            except TelegramLookupError as error:
                self.stderr.write(str(error))
                if options['once']:
                    return
                continue

            for update in updates:
                update_id = update.get('update_id')
                if update_id is not None:
                    offset = update_id + 1
                try:
                    result = process_telegram_update(update)
                    if result.callback_query_id:
                        answer_telegram_callback_query(result.callback_query_id, result.callback_text)
                    for message in result.messages:
                        if message.replace_existing and message.message_id is not None:
                            edit_telegram_message(
                                message.chat_id,
                                message.message_id,
                                message.text,
                                reply_markup=message.reply_markup,
                            )
                        else:
                            send_telegram_message(
                                message.chat_id,
                                message.text,
                                reply_markup=message.reply_markup,
                            )
                    for delete_request in result.delete_messages:
                        try:
                            delete_telegram_message(delete_request.chat_id, delete_request.message_id)
                        except TelegramLookupError:
                            pass
                except TelegramLookupError as error:
                    if 'message is not modified' not in str(error).lower():
                        self.stderr.write(str(error))

            if options['once']:
                return
