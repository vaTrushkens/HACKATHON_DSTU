"""Оркестратор ассистента: локальная модель через Ollama + инструменты проекта.

Один и тот же ``answer_stream()`` обслуживает чат на сайте (потоково) и Telegram-бота (``answer()`` собирает
поток в строку); история диалога хранится в БД отдельно по каналам. Если Ollama недоступна, ответ собирается
правилами из тех же инструментов, чтобы чат не молчал.

Два режима (``LLM_MODE``):

* ``router`` (по умолчанию, для компактных моделей) — сначала быстрый выбор инструмента по ключевым словам
  (``quick_route``), иначе модель выбирает его структурированным JSON-ответом по схеме (без размышлений);
  затем модель формулирует ответ по данным инструмента. Системный промпт одинаков на обоих шагах, поэтому
  Ollama переиспользует KV-кэш общего префикса.
* ``native`` — нативный function calling с циклом «модель → инструмент → модель»; надёжен у моделей от ~8B
  с включённым thinking, компактные в нём часто описывают вызов словами.
"""
from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterator

import requests
from django.conf import settings

from apps.companies.models import Company

from . import tools
from .models import ChatMessage
from .prompts import ROUTE_INSTRUCTION, minimal_prompt, system_prompt

log = logging.getLogger(__name__)
MAX_TOOL_ROUNDS = 4
HISTORY_LIMIT = 12
KEEP_ALIVE = "1h"
_capabilities: dict[str, list[str]] = {}

OFFTOPIC_REPLY = (
    "Я отвечаю только на вопросы о финансах компании и о работе сервиса: остаток и прогноз, кассовый разрыв, платежи и поступления, налоги, сценарии «что если», разделы сайта. "
    "Спросите, например: «Когда возможен кассовый разрыв?» или «Какие платежи на этой неделе?»"
)
ROUTE_SCHEMA = {
    "type": "object",
    "properties": {"tool": {"type": "string", "enum": [*tools.TOOLS_BY_NAME, "none", "offtopic"]}, "args": {"type": "object"}},
    "required": ["tool", "args"],
}
# Частые формулировки без параметров: инструмент выбирается без обращения к модели.
QUICK_ROUTES: list[tuple[re.Pattern, re.Pattern | None, str]] = [
    (re.compile(r"что\s*если|если\b.*(задерж|упад|сниз|вырас|подоро)", re.I), None, ""),  # сценарии оставляем модели: нужны аргументы
    (re.compile(r"нало[гж]|\bенп\b|\bусн\b|\bндс\b|взнос", re.I), None, "get_tax_calendar"),
    (re.compile(r"структур|на что уходит|стать[ияе]\s+расход|больше всего (денег|трат|уходит)|куда уходят", re.I), None, "get_category_breakdown"),
    (re.compile(r"плат[её]ж|заплатить|оплатить|поступлен|приход", re.I), re.compile(r"недел|дн[яей]|ближайш|сегодня|завтра|какие|что", re.I), "get_upcoming_payments"),
    (re.compile(r"разрыв|не хват|хватит ли|остат[ок]|риск|сколько (у нас )?денег|прогноз", re.I), None, "get_forecast_summary"),
]


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

    def warm_up(self) -> bool:
        """Загрузить модель в память без генерации, чтобы первый вопрос не ждал холодного старта."""
        try:
            r = requests.post(f"{self.base_url}/api/generate", json={"model": self.model, "keep_alive": KEEP_ALIVE}, timeout=120)
            return r.status_code == 200
        except requests.RequestException:
            return False

    def _payload(self, messages: list[dict], tools_spec: list[dict], fmt: dict | None, think: bool | None, stream: bool, num_predict: int | None) -> dict:
        payload = {"model": self.model, "messages": messages, "stream": stream, "keep_alive": KEEP_ALIVE, "options": {"temperature": 0.2, "num_ctx": settings.LLM_NUM_CTX, "num_predict": num_predict or settings.LLM_MAX_TOKENS}}
        if tools_spec:
            payload["tools"] = tools_spec
        if fmt:
            payload["format"] = fmt
        if "thinking" in self.capabilities():
            payload["think"] = settings.LLM_THINK if think is None else think
        return payload

    def chat(self, messages: list[dict], tools_spec: list[dict], *, fmt: dict | None = None, think: bool | None = None, num_predict: int | None = None) -> dict:
        try:
            r = requests.post(f"{self.base_url}/api/chat", json=self._payload(messages, tools_spec, fmt, think, False, num_predict), timeout=self.timeout)
            r.raise_for_status()
            return r.json()["message"]
        except (requests.RequestException, ValueError, KeyError) as e:
            raise LLMUnavailable(str(e)) from e

    def chat_stream(self, messages: list[dict], *, think: bool | None = None) -> Iterator[str]:
        """Куски текста ответа по мере генерации."""
        try:
            with requests.post(f"{self.base_url}/api/chat", json=self._payload(messages, [], None, think, True, None), stream=True, timeout=self.timeout) as r:
                r.raise_for_status()
                for line in r.iter_lines():
                    if not line:
                        continue
                    obj = json.loads(line)
                    if obj.get("error"):
                        raise LLMUnavailable(obj["error"])
                    chunk = (obj.get("message") or {}).get("content") or ""
                    if chunk:
                        yield chunk
                    if obj.get("done"):
                        return
        except (requests.RequestException, ValueError) as e:
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


def quick_route(question: str) -> str | None:
    """Имя инструмента для типовых вопросов без параметров; None — пусть решает модель."""
    for pattern, extra, name in QUICK_ROUTES:
        if pattern.search(question):
            if not name:
                return None
            if extra and not extra.search(question):
                continue
            return name
    return None


def _llm_route(client: OllamaClient, messages: list[dict]) -> tuple[str, dict]:
    route_msgs = [*messages[:-1], {"role": "user", "content": messages[-1]["content"] + ROUTE_INSTRUCTION}]
    try:
        route = json.loads(client.chat(route_msgs, [], fmt=ROUTE_SCHEMA, think=False, num_predict=120).get("content") or "{}")
    except ValueError:
        route = {}
    args = route.get("args") or {}
    return route.get("tool") or "none", {k: v for k, v in args.items() if v is not None} if isinstance(args, dict) else {}


def _tool_message(question: str, tool: tools.Tool, result: dict) -> str:
    label = tool.description.split(":")[0].split(".")[0]
    hint = " Сравни базовый вариант и сценарий: вероятность разрыва в процентах и нехватка в каждом, и на сколько они изменились." if tool.name == "run_what_if" else ""
    return f"{question}\n\n[Данные сервиса — {label}. Отвечай строго по ним, суммы и даты не меняй.{hint}\n{json.dumps(result, ensure_ascii=False, default=str)}]"


def _router_stream(client: OllamaClient, company: Company, messages: list[dict], used: list[dict]) -> Iterator[dict]:
    question = messages[-1]["content"]
    name = quick_route(question)
    args: dict = {}
    if name is None:
        yield {"type": "status", "text": "Выбираю данные…"}
        name, args = _llm_route(client, messages)
    if name == "offtopic":
        yield {"type": "delta", "text": OFFTOPIC_REPLY}  # фиксированный отказ без обращения к модели
        return
    tool = tools.TOOLS_BY_NAME.get(name)
    if not re.search(r"\d", question):
        args = {k: v for k, v in args.items() if not isinstance(v, (int, float))}  # чисел в вопросе нет — горизонт/дни берём по умолчанию, как на экране
    if tool and any(k not in args for k in tool.parameters.get("required", [])):
        tool = None  # «как добавить счёт?» — вопрос про сайт, а не команда: без суммы в тексте событие не создаём, даже если модель её выдумала
    if tool:
        yield {"type": "status", "text": "Считаю по данным сервиса…"}
        result = tools.call(company, tool.name, args)
        used.append({"name": tool.name, "args": args})
        yield {"type": "tools", "tools": list(used)}
        messages[-1] = {"role": "user", "content": _tool_message(question, tool, result)}
    yield {"type": "status", "text": "Формулирую ответ…"}
    yield from ({"type": "delta", "text": chunk} for chunk in client.chat_stream(messages, think=False))


def _native_stream(client: OllamaClient, company: Company, messages: list[dict], used: list[dict]) -> Iterator[dict]:
    for _ in range(MAX_TOOL_ROUNDS):
        msg = client.chat(messages, tools.ollama_specs())
        calls = msg.get("tool_calls") or []
        if not calls:
            yield {"type": "delta", "text": (msg.get("content") or "").strip()}
            return
        messages.append({"role": "assistant", "content": msg.get("content") or "", "tool_calls": calls})
        for c in calls:
            name, args = _parse_call(c)
            result = tools.call(company, name, args)
            used.append({"name": name, "args": args})
            messages.append({"role": "tool", "tool_name": name, "content": json.dumps(result, ensure_ascii=False, default=str)})
        yield {"type": "tools", "tools": list(used)}
    yield from ({"type": "delta", "text": chunk} for chunk in client.chat_stream(messages))


def answer_stream(company: Company, user, text: str, channel: str = "web") -> Iterator[dict]:
    """События: status (что происходит), tools (какие данные взяты), delta (кусок текста), done (итог). Итог сохраняется в историю."""
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
    parts: list[str] = []
    try:
        for evt in (_router_stream if settings.LLM_MODE == "router" else _native_stream)(client, company, messages, used):
            if evt["type"] == "delta":
                evt = {"type": "delta", "text": evt["text"].replace("**", "").replace("__", "")}  # ответ уходит в Telegram и чат как обычный текст
                parts.append(evt["text"])
            yield evt
    except LLMUnavailable as e:
        log.warning("LLM unavailable (%s), falling back to rules", e)
        source, used, parts = "rules", [], []
        try:
            parts.append(fallback_reply(company))
        except Exception:  # noqa: BLE001
            parts.append("Ассистент временно недоступен, а прогноз для компании построить не удалось. Загрузите выписку на странице «Операции».")
        yield {"type": "delta", "text": parts[0], "replace": True}
    reply = "".join(parts).strip() or "Не получилось сформулировать ответ. Попробуйте задать вопрос иначе, например: «Когда возможен кассовый разрыв?»"
    ChatMessage.objects.bulk_create(
        [ChatMessage(user=user, company=company, channel=channel, role="user", content=text), ChatMessage(user=user, company=company, channel=channel, role="assistant", content=reply, tools_used=used, source=source)]
    )
    yield {"type": "done", "reply": reply, "tools_used": used, "source": source, "model": client.model}


def answer(company: Company, user, text: str, channel: str = "web") -> dict:
    """Непотоковый вариант: собирает события answer_stream в итоговый ответ."""
    for evt in answer_stream(company, user, text, channel):
        if evt["type"] == "done":
            return {k: evt[k] for k in ("reply", "tools_used", "source", "model")}
    raise RuntimeError("answer_stream завершился без события done")
