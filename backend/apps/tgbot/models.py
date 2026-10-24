from django.conf import settings
from django.db import models

NOTIFY_KINDS = [
    ("cash_gap", "Кассовый разрыв"),
    ("low_runway", "Денег хватит меньше чем на 2 недели"),
    ("overdue_income", "Клиент не заплатил в срок"),
    ("big_payment", "Крупный платёж впереди"),
    ("reminders", "Напоминания о платежах на ближайшие дни"),
]
NOTIFY_KIND_LABELS = dict(NOTIFY_KINDS)


def default_notify_kinds() -> list[str]:
    return [code for code, _ in NOTIFY_KINDS]


class TelegramLink(models.Model):
    """Привязка Telegram-чата к пользователю сайта. Компания фиксируется при привязке; в чате выбирают только типы уведомлений."""

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="telegram")
    chat_id = models.BigIntegerField(unique=True)
    username = models.CharField(max_length=64, blank=True)
    company = models.ForeignKey("companies.Company", null=True, blank=True, on_delete=models.SET_NULL, related_name="telegram_links")
    notify_kinds = models.JSONField(default=default_notify_kinds, blank=True)  # коды из NOTIFY_KINDS
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self) -> str:
        return f"{self.user} ↔ tg:{self.chat_id}"


class TelegramLinkCode(models.Model):
    """Одноразовый код для /start <code>: выдаётся на сайте, живёт 15 минут."""

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    company = models.ForeignKey("companies.Company", null=True, blank=True, on_delete=models.SET_NULL)
    code = models.CharField(max_length=16, unique=True)
    expires_at = models.DateTimeField()
    used = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)


class SentNotification(models.Model):
    """Что уже отправлено в чат — чтобы одно и то же предупреждение не приходило каждые полчаса."""

    link = models.ForeignKey(TelegramLink, on_delete=models.CASCADE, related_name="sent")
    key = models.CharField(max_length=200)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("link", "key")
