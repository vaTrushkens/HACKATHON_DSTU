"""Сервисы Telegram-бота: привязка аккаунта, тексты команд, сбор и отправка уведомлений.

Все функции синхронные и возвращают простые типы (dict/str), чтобы их можно было
вызывать из async-обработчиков бота через поток без обращений к ORM в event loop.
"""
from __future__ import annotations

import logging
import secrets
from datetime import timedelta

import pandas as pd
import requests
from django.conf import settings
from django.core.cache import cache
from django.utils import timezone

from apps.assistant import llm
from apps.assistant import tools as assistant_tools
from apps.assistant.prompts import LEVELS, human_date, money
from apps.companies.models import Company
from core.permissions import companies_for_user

from .models import SentNotification, TelegramLink, TelegramLinkCode

log = logging.getLogger(__name__)
API = "https://api.telegram.org/bot{token}/{method}"
LINK_CODE_TTL = timedelta(minutes=15)
SOURCE_LABELS = {"recurring": "регулярный платёж", "tax": "налог", "bank": "данные банка", "invoice": "счёт", "contract": "договор", "manual": "введено вручную", "1c": "1С"}


# ---- Telegram Bot API (без зависимости от python-telegram-bot: нужно и one-shot команде, и API) ----
def _api(method: str, **payload) -> dict:
    r = requests.post(API.format(token=settings.TELEGRAM_BOT_TOKEN, method=method), json=payload, timeout=20)
    r.raise_for_status()
    return r.json()


def bot_username() -> str:
    if not settings.TELEGRAM_BOT_TOKEN:
        return ""
    name = cache.get("tgbot:username")
    if name is None:
        try:
            name = _api("getMe")["result"]["username"]
        except (requests.RequestException, KeyError, ValueError) as e:
            log.warning("telegram getMe failed: %s", e)
            name = ""
        cache.set("tgbot:username", name, 3600 if name else 60)
    return name


def send_message(chat_id: int, text: str) -> None:
    _api("sendMessage", chat_id=chat_id, text=text[:4000], disable_web_page_preview=True)


# ---- привязка ----
def create_link_code(user, company: Company | None) -> TelegramLinkCode:
    TelegramLinkCode.objects.filter(user=user, used=False).delete()
    return TelegramLinkCode.objects.create(user=user, company=company, code=secrets.token_hex(4).upper(), expires_at=timezone.now() + LINK_CODE_TTL)


def consume_link_code(code: str, chat_id: int, username: str) -> dict | None:
    rec = TelegramLinkCode.objects.select_related("user", "company").filter(code=(code or "").strip().upper(), used=False, expires_at__gt=timezone.now()).first()
    if not rec:
        return None
    TelegramLink.objects.filter(chat_id=chat_id).exclude(user=rec.user).delete()
    link, _ = TelegramLink.objects.update_or_create(user=rec.user, defaults={"chat_id": chat_id, "username": username[:64], "company": rec.company})
    if rec.company:
        link.notify_companies.add(rec.company)
    rec.used = True
    rec.save(update_fields=["used"])
    return {"user": rec.user.username, "company": rec.company.name if rec.company else "", "company_id": rec.company_id}


def link_status(user) -> dict:
    link = TelegramLink.objects.select_related("company").filter(user=user).first()
    if not link:
        return {"linked": False, "bot_username": bot_username()}
    return {"linked": True, "username": link.username, "company": link.company.name if link.company else None, "notify_companies": [c.code for c in link.notify_companies.all()], "bot_username": bot_username()}


def unlink(user) -> int:
    _, by_model = TelegramLink.objects.filter(user=user).delete()
    return by_model.get("tgbot.TelegramLink", 0)


def link_info(chat_id: int) -> dict | None:
    link = TelegramLink.objects.select_related("user", "company").filter(chat_id=chat_id).first()
    if not link:
        return None
    if link.company is None:
        link.company = companies_for_user(link.user).first()
        link.save(update_fields=["company"])
    return {"user_id": link.user_id, "username": link.user.username, "company_id": link.company_id, "company": link.company.name if link.company else ""}


def user_companies(chat_id: int) -> list[dict]:
    link = TelegramLink.objects.select_related("user").filter(chat_id=chat_id).first()
    if not link:
        return []
    notify = set(link.notify_companies.values_list("id", flat=True))
    return [{"id": c.id, "label": f"{c.code} · {c.name}"[:48], "selected": c.id == link.company_id, "notify": c.id in notify} for c in companies_for_user(link.user)[:30]]


def set_company(chat_id: int, company_id: int) -> str | None:
    link = TelegramLink.objects.select_related("user").filter(chat_id=chat_id).first()
    company = companies_for_user(link.user).filter(pk=company_id).first() if link else None
    if not company:
        return None
    link.company = company
    link.save(update_fields=["company"])
    return company.name


def toggle_notify(chat_id: int, company_id: int) -> bool | None:
    link = TelegramLink.objects.select_related("user").filter(chat_id=chat_id).first()
    company = companies_for_user(link.user).filter(pk=company_id).first() if link else None
    if not company:
        return None
    if link.notify_companies.filter(pk=company.pk).exists():
        link.notify_companies.remove(company)
        return False
    link.notify_companies.add(company)
    return True


# ---- тексты команд ----
def forecast_text(company_id: int) -> str:
    company = Company.objects.get(pk=company_id)
    s = assistant_tools.get_forecast_summary(company)
    lines = [f"📊 {company.name} — данные на {human_date(s['as_of'])}", f"Остаток: {money(s['opening_balance'])}", f"Через {s['horizon_days']} дней: {money(s['end_balance_p50'])} (минимум за период {money(s['min_balance_p50'])})", f"Риск разрыва: {LEVELS.get(s['risk_level'], s['risk_level'])}, вероятность {s['probability']:.0%}"]
    if s["gap_date"]:
        lines.append(f"Дата риска: {human_date(s['gap_date'])}, нехватка около {money(s['deficit_p50'])}")
    lines.append("")
    lines.append(s["explanation"])
    if s["recommendations"]:
        lines += ["", "Что можно сделать:"] + [f"• {r['title']}" for r in s["recommendations"][:3]]
    return "\n".join(lines)


def payments_text(company_id: int, days: int = 7) -> str:
    company = Company.objects.get(pk=company_id)
    p = assistant_tools.get_upcoming_payments(company, days)
    if not p["events"]:
        return f"На ближайшие {days} дн. ожидаемых платежей и поступлений нет."
    lines = [f"📅 {company.name}: ближайшие {days} дн. — списания {money(p['total_out'])}, поступления {money(p['total_in'])}", ""]
    for e in p["events"][:15]:
        sign = "−" if e["direction"] == "Списание" else "+"
        lines.append(f"{human_date(e['date'])}: {sign}{money(e['amount'])} — {e['label']} ({SOURCE_LABELS.get(e['source'], e['source'])})")
    return "\n".join(lines)


def assistant_reply(chat_id: int, text: str) -> str:
    link = TelegramLink.objects.select_related("user", "company").filter(chat_id=chat_id).first()
    if not link or not link.company:
        return "Сначала подключите аккаунт: на сайте откройте окно ассистента и нажмите «Подключить Telegram»."
    return llm.answer(link.company, link.user, text, channel="telegram")["reply"]


# ---- уведомления ----
def collect_notifications(company: Company) -> list[tuple[str, str]]:
    """(ключ, текст). Ключ стабилен для одной и той же ситуации, поэтому повторный запуск ничего не дублирует."""
    data = assistant_tools.forecast_data(company)
    risk, fc = data["risk"], data["forecast"]
    out: list[tuple[str, str]] = []
    for a in risk["alerts"]:
        if a["level"] not in ("high", "medium"):
            continue
        key = f"alert:{company.code}:{a['kind']}:{a.get('date')}:{round(a.get('amount') or 0)}"
        icon = "🔴" if a["level"] == "high" else "🟡"
        out.append((key, f"{icon} {company.name}: {a['title']}\n{a['message']}"))
    as_of = pd.Timestamp(fc["as_of"])
    horizon = as_of + pd.Timedelta(days=settings.TELEGRAM_REMIND_DAYS)
    due = [e for e in fc["events"] if e["direction"] == "Списание" and as_of < pd.Timestamp(e["date"]) <= horizon]
    if due:
        due.sort(key=lambda e: (e["date"], -e["amount"]))
        lines = [f"🔔 {company.name}: платежи на ближайшие {settings.TELEGRAM_REMIND_DAYS} дн. — всего {money(sum(e['amount'] for e in due))}"]
        lines += [f"• {human_date(e['date'])} — {e['label']}: {money(e['amount'])} ({SOURCE_LABELS.get(e['source'], e['source'])})" for e in due[:10]]
        out.append((f"remind:{company.code}:{fc['as_of']}:{timezone.localdate()}", "\n".join(lines)))
    return out


def send_pending_notifications(dry_run: bool = False) -> int:
    sent = 0
    for link in TelegramLink.objects.select_related("company").prefetch_related("notify_companies"):
        companies = list(link.notify_companies.all()) or ([link.company] if link.company else [])
        for company in companies:
            try:
                items = collect_notifications(company)
            except Exception:  # noqa: BLE001 — одна компания без данных не должна остановить рассылку
                log.exception("notifications for %s failed", company.code)
                continue
            for key, text in items:
                if SentNotification.objects.filter(link=link, key=key).exists():
                    continue
                if not dry_run:
                    try:
                        send_message(link.chat_id, text)
                    except requests.RequestException as e:
                        log.warning("telegram send to %s failed: %s", link.chat_id, e)
                        continue
                    SentNotification.objects.create(link=link, key=key)
                sent += 1
    return sent
