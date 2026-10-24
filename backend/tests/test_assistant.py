"""Ассистент и Telegram: цикл инструментов с подменённой моделью, деградация без Ollama, привязка и дедупликация уведомлений."""
import pytest
from django.conf import settings
from django.contrib.auth import get_user_model

from apps.accounts.models import CompanyMembership
from apps.assistant import llm, tools
from apps.assistant.models import ChatMessage
from apps.companies.models import Company
from apps.tgbot import services as tg
from apps.tgbot.models import SentNotification, TelegramLink


@pytest.fixture
def user_company(db, monkeypatch):
    User = get_user_model()
    user = User.objects.create_user("owner", password="pass12345")
    company = Company.objects.create(code="T1", name="Тест", legal_form="ООО")
    CompanyMembership.objects.create(user=user, company=company)
    monkeypatch.setattr(llm, "system_prompt", lambda c, site_help=True: "system")
    return user, company


def test_coerce_maps_direction_synonyms_and_drops_unknown():
    schema = tools.TOOLS_BY_NAME["add_planned_event"].parameters
    assert tools._coerce(schema, {"direction": "income", "amount": "300000"}) == {"direction": "Поступление", "amount": 300000.0}
    assert tools._coerce(schema, {"direction": "expense"})["direction"] == "Списание"
    assert "direction" not in tools._coerce(schema, {"direction": "sideways"})


def test_router_offtopic_is_refused_without_model_answer(user_company, monkeypatch):
    user, company = user_company
    monkeypatch.setattr(settings, "LLM_MODE", "router")
    calls = []
    monkeypatch.setattr(llm.OllamaClient, "chat", lambda self, messages, specs, fmt=None, think=None: calls.append(fmt is not None) or {"role": "assistant", "content": '{"tool": "offtopic", "args": {}}'})
    out = llm.answer(company, user, "Сколько букв н в слове банан?")
    assert out["reply"] == llm.OFFTOPIC_REPLY and out["tools_used"] == [] and calls == [True]  # только шаг маршрутизации


def test_collect_notifications_respects_kinds(user_company, monkeypatch):
    _, company = user_company
    fake = {"risk": {"alerts": [{"kind": "cash_gap", "level": "high", "title": "Разрыв", "message": "m", "date": "2026-07-28", "amount": 1}, {"kind": "big_payment", "level": "info", "title": "Крупный", "message": "m", "date": "2026-07-10", "amount": 2}]},
            "forecast": {"as_of": "2026-07-01", "events": [{"direction": "Списание", "date": "2026-07-02", "amount": 100, "label": "Аренда", "source": "recurring"}]}}
    monkeypatch.setattr(tg.assistant_tools, "forecast_data", lambda c: fake)
    kinds = lambda items: [k.split(":")[0] + ":" + k.split(":")[2] if k.startswith("alert") else k.split(":")[0] for k, _ in items]  # noqa: E731
    assert kinds(tg.collect_notifications(company)) == ["alert:cash_gap", "alert:big_payment", "remind"]
    assert kinds(tg.collect_notifications(company, ["cash_gap"])) == ["alert:cash_gap"]
    assert kinds(tg.collect_notifications(company, ["reminders"])) == ["remind"]
    assert tg.collect_notifications(company, []) == []


def test_router_never_creates_event_from_how_to_question(user_company, monkeypatch):
    user, company = user_company
    monkeypatch.setattr(settings, "LLM_MODE", "router")
    created = []
    monkeypatch.setattr(tools.TOOLS_BY_NAME["add_planned_event"], "func", lambda company, **kw: created.append(kw) or {"id": 1})
    route = '{"tool": "add_planned_event", "args": {"direction": "income", "amount": 300000, "date": "2024-08-15", "label": "Счёт"}}'
    monkeypatch.setattr(llm.OllamaClient, "chat", lambda self, messages, specs, fmt=None, think=None: {"role": "assistant", "content": route if fmt else "Откройте «Календарь платежей»."})
    out = llm.answer(company, user, "Как добавить счёт от клиента в календарь?")  # чисел в вопросе нет → выдуманная сумма отбрасывается → событие не создаётся
    assert created == [] and out["tools_used"] == []


def test_router_picks_tool_and_answers_from_its_data(user_company, monkeypatch):
    user, company = user_company
    monkeypatch.setattr(settings, "LLM_MODE", "router")
    seen = []

    def fake_chat(self, messages, specs, fmt=None, think=None):
        if fmt:  # шаг выбора инструмента: структурированный JSON
            assert think is False and "get_category_breakdown" in messages[0]["content"]
            return {"role": "assistant", "content": '{"tool": "get_category_breakdown", "args": {"months": "6"}}'}
        seen.append(messages[-1]["content"])
        return {"role": "assistant", "content": "Больше всего — зарплата, 1 500 000 ₽ в месяц."}

    monkeypatch.setattr(llm.OllamaClient, "chat", fake_chat)
    monkeypatch.setattr(tools.TOOLS_BY_NAME["get_category_breakdown"], "func", lambda company, months=3: {"outflow": [{"label": "Зарплата", "monthly_avg": 1500000}], "months": months})
    out = llm.answer(company, user, "На что уходит больше всего?")
    assert out["tools_used"] == [{"name": "get_category_breakdown", "args": {"months": "6"}}]
    assert "Зарплата" in seen[0] and '"months": 6' in seen[0]  # данные инструмента попали в финальный запрос, аргумент приведён к int
    assert out["reply"].startswith("Больше всего")


def test_router_none_answers_without_tools(user_company, monkeypatch):
    user, company = user_company
    monkeypatch.setattr(settings, "LLM_MODE", "router")
    monkeypatch.setattr(llm.OllamaClient, "chat", lambda self, messages, specs, fmt=None, think=None: {"role": "assistant", "content": '{"tool": "none", "args": {}}' if fmt else "Откройте «Календарь платежей»."})
    out = llm.answer(company, user, "Как добавить счёт?")
    assert out["tools_used"] == [] and out["reply"] == "Откройте «Календарь платежей»."


def test_native_tool_loop_coerces_args_and_saves_history(user_company, monkeypatch):
    user, company = user_company
    monkeypatch.setattr(settings, "LLM_MODE", "native")
    replies = [
        {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "get_forecast_summary", "arguments": {"horizon": "30", "unknown": 1}}}]},
        {"role": "assistant", "content": "Разрыв 28 июля, не хватит 120 000 ₽."},
    ]
    seen = []
    monkeypatch.setattr(llm.OllamaClient, "chat", lambda self, messages, specs, fmt=None, think=None: replies.pop(0))
    monkeypatch.setattr(tools.TOOLS_BY_NAME["get_forecast_summary"], "func", lambda company, horizon=60: seen.append(horizon) or {"summary": "ok"})
    out = llm.answer(company, user, "Когда разрыв?")
    assert out["source"] == "llm"
    assert out["reply"].startswith("Разрыв")
    assert out["tools_used"] == [{"name": "get_forecast_summary", "args": {"horizon": "30", "unknown": 1}}]
    assert seen == [30]
    assert list(ChatMessage.objects.filter(user=user, company=company).values_list("role", flat=True)) == ["user", "assistant"]


def test_falls_back_to_rules_when_llm_down(user_company, monkeypatch):
    user, company = user_company

    def boom(self, messages, specs, fmt=None, think=None):
        raise llm.LLMUnavailable("down")

    monkeypatch.setattr(llm.OllamaClient, "chat", boom)
    monkeypatch.setattr(llm, "fallback_reply", lambda c: "ответ по правилам")
    out = llm.answer(company, user, "?")
    assert out == {"reply": "ответ по правилам", "tools_used": [], "source": "rules", "model": out["model"]}


def test_unknown_tool_is_reported_not_raised(user_company):
    _, company = user_company
    assert "error" in tools.call(company, "nope", {})


def test_link_code_flow(user_company):
    user, company = user_company
    rec = tg.create_link_code(user, company)
    info = tg.consume_link_code(rec.code.lower(), 42, "oleg")
    assert info == {"user": "owner", "company": "Тест", "company_id": company.id}
    link = TelegramLink.objects.get(chat_id=42)
    assert link.company == company and link.notify_kinds == ["cash_gap", "low_runway", "overdue_income", "big_payment", "reminders"]
    assert tg.toggle_notify(42, "reminders") is False and tg.toggle_notify(42, "nope") is None
    assert [o["on"] for o in tg.notify_options(42)] == [True, True, True, True, False]
    assert tg.consume_link_code(rec.code, 42, "oleg") is None  # одноразовый
    assert tg.link_status(user)["linked"] is True
    assert tg.unlink(user) == 1


def test_notifications_are_deduplicated(user_company, monkeypatch):
    user, company = user_company
    TelegramLink.objects.create(user=user, chat_id=1, company=company)
    monkeypatch.setattr(tg, "collect_notifications", lambda c, kinds: [("alert:T1:cash_gap:2026-07-28:120000", "Разрыв")])
    sent = []
    monkeypatch.setattr(tg, "send_message", lambda chat_id, text: sent.append((chat_id, text)))
    assert tg.send_pending_notifications() == 1
    assert tg.send_pending_notifications() == 0
    assert sent == [(1, "Разрыв")] and SentNotification.objects.count() == 1
