from django.conf import settings
from django.db import models


class ChatMessage(models.Model):
    """Сообщение диалога с ассистентом. Канал — сайт или Telegram, история по ним ведётся раздельно."""

    CHANNELS = [("web", "Сайт"), ("telegram", "Telegram")]
    ROLES = [("user", "Пользователь"), ("assistant", "Ассистент")]

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="chat_messages")
    company = models.ForeignKey("companies.Company", on_delete=models.CASCADE, related_name="chat_messages")
    channel = models.CharField(max_length=16, choices=CHANNELS, default="web")
    role = models.CharField(max_length=16, choices=ROLES)
    content = models.TextField()
    tools_used = models.JSONField(default=list, blank=True)
    source = models.CharField(max_length=16, blank=True)  # llm | rules
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["id"]
        indexes = [models.Index(fields=["user", "company", "channel"])]

    def __str__(self) -> str:
        return f"{self.channel}/{self.role}: {self.content[:40]}"
