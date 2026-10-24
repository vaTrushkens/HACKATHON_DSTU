from django.conf import settings
from rest_framework.response import Response
from rest_framework.views import APIView

from core.permissions import get_company_for_user

from . import tools


class AssistantContextView(APIView):
    """Структурированный контекст компании и список инструментов — то, что получит LLM."""

    def get(self, request, company_pk):
        company = get_company_for_user(request.user, company_pk)
        return Response({"llm_provider": settings.LLM_PROVIDER, "enabled": settings.LLM_PROVIDER != "none", "context": tools.build_context(company)})


class AssistantToolView(APIView):
    """Прямой вызов инструмента (для отладки и для будущего оркестратора): POST {"tool": name, "args": {...}}."""

    def post(self, request, company_pk):
        company = get_company_for_user(request.user, company_pk)
        tool = tools.TOOLS_BY_NAME.get(request.data.get("tool"))
        if not tool:
            return Response({"detail": "Неизвестный инструмент", "available": list(tools.TOOLS_BY_NAME)}, status=400)
        return Response({"tool": tool.name, "result": tool.func(company, **(request.data.get("args") or {}))})


class AssistantChatView(APIView):
    """Заглушка чата: пока LLM не подключён, отвечает шаблонной сводкой из тех же инструментов."""

    def post(self, request, company_pk):
        company = get_company_for_user(request.user, company_pk)
        summary = tools.get_forecast_summary(company)
        text = summary["summary"] + ". " + summary["explanation"]
        if summary["recommendations"]:
            text += " Что можно сделать: " + "; ".join(r["title"] for r in summary["recommendations"][:3]) + "."
        return Response({"reply": text, "llm": settings.LLM_PROVIDER, "note": "Ответ сформирован правилами; LLM-провайдер не подключён (LLM_PROVIDER=none)."})
