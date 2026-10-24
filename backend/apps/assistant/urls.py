from django.urls import path

from .views import AssistantChatView, AssistantContextView, AssistantToolView

urlpatterns = [
    path("companies/<int:company_pk>/assistant/context/", AssistantContextView.as_view()),
    path("companies/<int:company_pk>/assistant/tool/", AssistantToolView.as_view()),
    path("companies/<int:company_pk>/assistant/chat/", AssistantChatView.as_view()),
]
