from django.conf import settings
from django.db import models


class CompanyMembership(models.Model):
    """Связь пользователь ↔ компания. Пользователь видит только свои компании."""

    ROLE_OWNER = "owner"
    ROLE_ACCOUNTANT = "accountant"
    ROLE_VIEWER = "viewer"
    ROLES = [(ROLE_OWNER, "Владелец"), (ROLE_ACCOUNTANT, "Бухгалтер"), (ROLE_VIEWER, "Просмотр")]

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="memberships")
    company = models.ForeignKey("companies.Company", on_delete=models.CASCADE, related_name="memberships")
    role = models.CharField(max_length=20, choices=ROLES, default=ROLE_OWNER)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("user", "company")

    def __str__(self) -> str:
        return f"{self.user} → {self.company_id} ({self.role})"
