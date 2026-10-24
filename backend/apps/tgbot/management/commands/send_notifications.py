from django.core.management.base import BaseCommand

from apps.tgbot import services


class Command(BaseCommand):
    help = "Разовая рассылка уведомлений в Telegram (то же, что делает бот по расписанию). --dry-run — только посчитать"

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **opts):
        n = services.send_pending_notifications(dry_run=opts["dry_run"])
        self.stdout.write(f"{'Будет отправлено' if opts['dry_run'] else 'Отправлено'}: {n}")
