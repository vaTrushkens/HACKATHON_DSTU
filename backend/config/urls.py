from django.contrib import admin
from django.http import JsonResponse
from django.urls import include, path


def health(_request):
    return JsonResponse({"status": "ok"})


urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/health/", health),
    path("api/auth/", include("apps.accounts.urls")),
    path("api/", include("apps.companies.urls")),
    path("api/", include("apps.operations.urls")),
    path("api/", include("apps.planning.urls")),
    path("api/", include("apps.forecasting.urls")),
    path("api/", include("apps.assistant.urls")),
]
