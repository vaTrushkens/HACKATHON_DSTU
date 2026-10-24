from rest_framework.response import Response
from rest_framework.views import APIView

from core.permissions import get_company_for_user

from . import services


class LinkCodeView(APIView):
    """Код и deep-link для привязки Telegram к текущему пользователю и компании."""

    def post(self, request, company_pk):
        company = get_company_for_user(request.user, company_pk)
        rec = services.create_link_code(request.user, company)
        username = services.bot_username()
        return Response({"code": rec.code, "expires_at": rec.expires_at.isoformat(), "bot_username": username, "deep_link": f"https://t.me/{username}?start={rec.code}" if username else None})


class LinkView(APIView):
    def get(self, request):
        return Response(services.link_status(request.user))

    def delete(self, request):
        return Response({"deleted": services.unlink(request.user)})
