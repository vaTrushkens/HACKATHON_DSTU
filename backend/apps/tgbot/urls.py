from django.urls import path

from .views import LinkCodeView, LinkView

urlpatterns = [
    path("companies/<int:company_pk>/telegram/link-code/", LinkCodeView.as_view()),
    path("telegram/link/", LinkView.as_view()),
]
