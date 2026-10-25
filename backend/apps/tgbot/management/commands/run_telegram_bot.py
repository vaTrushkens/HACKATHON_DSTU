from django.conf import settings
from core.commands import LoggedCommand


class Command(LoggedCommand):
    help = "Запускает Telegram-бота (long polling) и фоновую рассылку уведомлений"

    def handle(self, *args, **opts):
        if not settings.TELEGRAM_BOT_TOKEN:
            self.stderr.write("TELEGRAM_BOT_TOKEN не задан в .env — бот не запущен")
            return
        from apps.tgbot.bot import main

        self.stdout.write(f"Telegram-бот запущен, модель ассистента: {settings.LLM_MODEL} @ {settings.LLM_BASE_URL}")
        main()
