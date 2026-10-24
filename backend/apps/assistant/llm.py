"""Оркестратор ассистента: локальная модель через Ollama + инструменты проекта.

Один и тот же ``answer()`` обслуживает чат на сайте и Telegram-бота; история диалога
хранится в БД отдельно по каналам. Если Ollama недоступна, ответ собирается правилами
из тех же инструментов, чтобы чат не молчал.

Два режима (``LLM_MODE``):

* ``router`` (по умолчанию, для компактных моделей) — модель сначала выбирает инструмент
  структурированным JSON-ответом по схеме (без размышлений, уклониться в «я сейчас проверю…»
  нельзя), затем формулирует ответ по данным инструмента. Два коротких вызова вместо
  долгих размышлений.
* ``native`` — нативный function calling с циклом «модель → инструмент → модель»; надёжен
  у моделей от ~8B с включённым thinking, компактные в нём часто описывают вызов словами.
"""
from __future__ import annotations

import json
import logging
import re

import requests
from django.conf import settings

from apps.companies.models import Company

from . import tools
from .models import ChatMessage
from .prompts import minimal_prompt, router_prompt, system_prompt

log = logging.getLogger(__name__)
MAX_TOOL_ROUNDS = 4
HISTORY_LIMIT = 12
ROUTER_HISTORY = 4
_capabilities: dict[str, list[str]] = {}


class LLMUnavailable(Exception):
    pass


class OllamaClient:
    def __init__(self, base_url: str | None = None, model: str | None = None, timeout: int | None = None):
        self.base_url = (base_url or settings.LLM_BASE_URL).rstrip("/")
        self.model = model or settings.LLM_MODEL
        self.timeout = timeout or settings.LLM_TIMEOUT

    def capabilities(self) -> list[str]:
        if self.model not in _capabilities:
            try:
                r = requests.post(f"{self.base_url}/api/show", json={"model": self.model}, timeout=10)
                r.raise_for_status()
                _capabilities[self.model] = r.json().get("capabilities") or []
            except (requests.RequestException, ValueError) as e:
                raise LLMUnavailable(f"модель {self.model} недоступна: {e}") from e
        return _capabilities[self.model]

    def available(self) -> bool:
        try:
            return "completion" in self.capabilities()
        except LLMUnavailable:
            return False

    def chat(self, messages: list[dict], tools_spec: list[dict], *, fmt: dict | None = None, think: bool | None = None) -> dict:
        payload = {"model": self.model, "messages": messages, "stream": False, "keep_alive": "30m", "options": {"temperature": 0.2, "num_ctx": settings.LLM_NUM_CTX}}
        if tools_spec:
            payload["tools"] = tools_spec
        if fmt:
            payload["format"] = fmt
        if "thinking" in self.capabilities():
            payload["think"] = settings.LLM_THINK if think is None else think
        try:
            r = requests.post(f"{self.base_url}/api/chat", json=payload, timeout=self.timeout)
            r.raise_for_status()
            return r.json()["message"]
        except (requests.RequestException, ValueError, KeyError) as e:
            raise LLMUnavailable(str(e)) from e


def fallback_reply(company: Company) -> str:
    s = tools.get_forecast_summary(company)
    text = s["summary"] + ". " + s["explanation"]
    if s["recommendations"]:
        text += " Что можно сделать: " + "; ".join(r["title"] for r in s["recommendations"][:3]) + "."
    return text


def _history(user, company: Company, channel: str, limit: int = HISTORY_LIMIT) -> list[dict]:
    rows = ChatMessage.objects.filter(user=user, company=company, channel=channel).order_by("-id")[:limit]
    return [{"role": m.role, "content": m.content} for m in reversed(list(rows))]


def _parse_call(c: dict) -> tuple[str, dict]:
    fn = c.get("function") or {}
    args = fn.get("arguments") or {}
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except ValueError:
            args = {}
    return fn.get("name", ""), args


def _native(client: OllamaClient, company: Company, messages: list[dict], used: list[dict]) -> str:
    for _ in range(MAX_TOOL_ROUNDS):
        msg = client.chat(messages, tools.ollama_specs())
        calls = msg.get("tool_calls") or []
        if not calls:
            return (msg.get("content") or "").strip()
        messages.append({"role": "assistant", "content": msg.get("content") or "", "tool_calls": calls})
        for c in calls:
            name, args = _parse_call(c)
            result = tools.call(company, name, args)
            used.append({"name": name, "args": args})
            messages.append({"role": "tool", "tool_name": name, "content": json.dumps(result, ensure_ascii=False, default=str)})
    return (client.chat(messages, []).get("content") or "").strip()


ROUTE_SCHEMA = {
    "type": "object",
    "properties": {"tool": {"type": "string", "enum": [*tools.TOOLS_BY_NAME, "none"]}, "args": {"type": "object"}},
    "required": ["tool", "args"],
}


def _router(client: OllamaClient, company: Company, messages: list[dict], used: list[dict]) -> str:
    question = messages[-1]["content"]
    recent = [m for m in messages[1:-1]][-ROUTER_HISTORY:]
    route_msgs = [{"role": "system", "content": router_prompt()}, *recent, {"role": "user", "content": question}]
    try:
        route = json.loads(client.chat(route_msgs, [], fmt=ROUTE_SCHEMA, think=False).get("content") or "{}")
    except ValueError:
        route = {}
    name, args = route.get("tool") or "none", route.get("args") or {}
    args = {k: v for k, v in args.items() if v is not None} if isinstance(args, dict) else {}
    tool = tools.TOOLS_BY_NAME.get(name)
    if not re.search(r"\d", question):
        args = {k: v for k, v in args.items() if not isinstance(v, (int, float))}  # чисел в вопросе нет — горизонт/дни берём по умолчанию, как на экране
    if tool and any(k not in args for k in tool.parameters.get("required", [])):
        tool = None  # «как добавить счёт?» — вопрос про сайт, а не команда: без суммы в тексте событие не создаём, даже если модель её выдумала
    if tool:
        result = tools.call(company, tool.name, args)
        used.append({"name": tool.name, "args": args})
        label = tool.description.split(":")[0].split(".")[0]
        hint = " Сравни базовый вариант и сценарий: вероятность разрыва в процентах и нехватка в каждом, и на сколько они изменились." if tool.name == "run_what_if" else ""
        messages[-1] = {"role": "user", "content": f"{question}\n\n[Данные сервиса — {label}. Отвечай строго по ним, суммы и даты не меняй.{hint}\n{json.dumps(result, ensure_ascii=False, default=str)}]"}
        messages[0] = {"role": "system", "content": system_prompt(company, site_help=False)}  # короче промпт — быстрее ответ на CPU
    return (client.chat(messages, [], think=False).get("content") or "").strip()


def answer(company: Company, user, text: str, channel: str = "web") -> dict:
    text = (text or "").strip()[:2000]
    try:
        prompt = system_prompt(company)
    except Exception:  # noqa: BLE001 — прогноз не строится (нет операций и т.п.), но про сайт ответить можно
        log.exception("assistant context failed for %s", company.code)
        prompt = minimal_prompt(company)
    messages = [{"role": "system", "content": prompt}, *_history(user, company, channel), {"role": "user", "content": text}]
    client = OllamaClient()
    used: list[dict] = []
    source = "llm"
    reply = ""
    try:
        reply = (_router if settings.LLM_MODE == "router" else _native)(client, company, messages, used)
    except LLMUnavailable as e:
        log.warning("LLM unavailable (%s), falling back to rules", e)
        source = "rules"
        try:
            reply = fallback_reply(company)
        except Exception:  # noqa: BLE001
            reply = "Ассистент временно недоступен, а прогноз для компании построить не удалось. Загрузите выписку на странице «Операции»."
    reply = reply.replace("**", "").replace("__", "")  # ответ уходит в Telegram и чат как обычный текст
    if not reply:
        reply = "Не получилось сформулировать ответ. Попробуйте задать вопрос иначе, например: «Когда возможен кассовый разрыв?»"
    ChatMessage.objects.bulk_create(
        [ChatMessage(user=user, company=company, channel=channel, role="user", content=text), ChatMessage(user=user, company=company, channel=channel, role="assistant", content=reply, tools_used=used, source=source)]
    )
    return {"reply": reply, "tools_used": used, "source": source, "model": client.model}
