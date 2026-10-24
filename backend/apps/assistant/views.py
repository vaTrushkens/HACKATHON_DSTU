from django.conf import settings
from rest_framework.response import Response
from rest_framework.views import APIView

from core.permissions import get_company_for_user

from . import llm, tools
from .models import ChatMessage


class AssistantContextView(APIView):
    """Структурированный контекст компании и список инструментов — то, что получает модель."""

    def get(self, request, company_pk):
        company = get_company_for_user(request.user, company_pk)
        return Response({"llm_provider": settings.LLM_PROVIDER, "model": settings.LLM_MODEL, "enabled": llm.OllamaClient().available(), "context": tools.build_context(company)})


class AssistantToolView(APIView):
    """Прямой вызов инструмента (для отладки): POST {"tool": name, "args": {...}}."""

    def post(self, request, company_pk):
        company = get_company_for_user(request.user, company_pk)
        name = request.data.get("tool", "")
        if name not in tools.TOOLS_BY_NAME:
            return Response({"detail": "Неизвестный инструмент", "available": list(tools.TOOLS_BY_NAME)}, status=400)
        return Response({"tool": name, "result": tools.call(company, name, request.data.get("args") or {})})


class AssistantChatView(APIView):
    """GET — история и статус модели, POST {"message"} — ответ ассистента, DELETE — очистить историю."""

    def get(self, request, company_pk):
        company = get_company_for_user(request.user, company_pk)
        rows = ChatMessage.objects.filter(user=request.user, company=company, channel="web").order_by("-id")[:40]
        messages = [{"id": m.id, "role": m.role, "content": m.content, "tools_used": m.tools_used, "source": m.source, "created_at": m.created_at.isoformat()} for m in reversed(list(rows))]
        return Response({"messages": messages, "model": settings.LLM_MODEL, "enabled": llm.OllamaClient().available()})

    def post(self, request, company_pk):
        company = get_company_for_user(request.user, company_pk)
        message = (request.data.get("message") or "").strip()
        if not message:
            return Response({"detail": "Пустое сообщение"}, status=400)
        return Response(llm.answer(company, request.user, message, channel="web"))

    def delete(self, request, company_pk):
        company = get_company_for_user(request.user, company_pk)
        deleted, _ = ChatMessage.objects.filter(user=request.user, company=company, channel="web").delete()
        return Response({"deleted": deleted})
