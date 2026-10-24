"""Правила категоризации по назначению платежа и контрагенту.

Правила проверяются по порядку, первое совпадение побеждает, поэтому
специфичные паттерны (ЕНП, зарплата, кредит) стоят выше общих («оплата по счёту»).
Каждое правило возвращает категорию и уверенность — она используется, чтобы
отобрать «чистую» обучающую выборку для ML-доразметки.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from ..categories import IN, OUT, fallback_for

FNS_INN = "7727406020"  # Казначейство России (ФНС)


def normalize_text(s: str | None) -> str:
    s = (s or "").lower().replace("ё", "е")
    s = re.sub(r"\s+", " ", s)
    return s.strip()


# Служебные хвосты про НДС есть почти в каждом назначении («Без налога (НДС)», «НДС не облагается»,
# «В т.ч. НДС 20% 1234.56») и не несут смысла для категории — иначе слово «налога» уводит платёж в TAXES.
_VAT_TAIL_RE = re.compile(r"(без налога\s*\(ндс\)|ндс не облагается|без ндс|в т\.?\s?ч\.?\s*ндс.*$|в том числе ндс.*$|сумма [\d\s.,-]+ в т\.?\s?ч\.?.*$)")


def strip_vat_tail(s: str) -> str:
    return _VAT_TAIL_RE.sub(" ", s).strip()


@dataclass(frozen=True)
class Rule:
    code: str
    direction: str | None  # None = любое направление
    purpose: str | None = None  # regex по назначению
    counterparty: str | None = None  # regex по наименованию контрагента
    confidence: float = 0.95
    require_both: bool = False  # требовать совпадения и назначения, и контрагента

    def matches(self, direction: str, purpose: str, cp: str) -> bool:
        if self.direction and direction != self.direction:
            return False
        p_ok = bool(self.purpose and re.search(self.purpose, purpose))
        c_ok = bool(self.counterparty and re.search(self.counterparty, cp))
        if self.require_both:
            return p_ok and c_ok
        return p_ok or c_ok


RULES: list[Rule] = [
    # --- финансирование и собственник (важно поймать раньше «возвратов» и «договоров») ---
    Rule("OWNER_IN", IN, r"займ\w* учредител|пополнение счета собственными|собственн\w+ средств\w* с (расчетного|р/с|счета|карты)|взнос собственника"),
    Rule("OWNER_IN", IN, counterparty=r"учредитель"),
    Rule("LOAN_IN", IN, r"выдача кредита|транш|кредитн\w+ лини\w+ .*выдача|предоставление кредита|получение займа"),
    Rule("OWNER_OUT", OUT, r"возврат .*займ\w* учредител|дивиденд|перевод на личн|собственн\w+ средств\w* (ип )?на (расчетный|р/с|счет|личн|карт)|остатка денежных средств в связи с закрытием"),
    Rule("OWNER_OUT", OUT, counterparty=r"учредитель"),
    # --- налоги ---
    Rule("TAXES", OUT, r"единый налогов|\bенп\b|налог|страхов\w+ взнос|ндфл|пенсионн|\bфсс\b|\bпфр\b|торговый сбор", confidence=0.98),
    Rule("TAXES", OUT, counterparty=r"фнс|казначейство|налогов", confidence=0.98),
    Rule("FINES", OUT, r"исполнительн\w+ лист|по поручению налогового органа|штраф|пени|неустойк|взыскан"),
    # --- зарплата и люди ---
    Rule("SALARY", OUT, r"заработн\w+ плат|аванс по заработн|зарплат|окончательный расчет по заработн|по реестру .*зарплат|отпускн|больничн|компенсация при увольнении", confidence=0.98),
    Rule("CONTRACTORS", OUT, r"договор\w* гпх|гражданско-правов|самозанят|вознаграждение по договору подряда физ|услуг\w* по договору гпх"),
    Rule("CONTRACTORS", OUT, counterparty=r"самозанят", confidence=0.9),
    # --- кредиты, лизинг ---
    Rule("LOAN_OUT", OUT, r"погашение кредита|погашение основного долга|процент\w* за пользование|процент\w* по (договору )?кредит|овердрафт|лизингов\w+ платеж|договор\w* лизинга|погашение займа", confidence=0.98),
    Rule("LOAN_OUT", OUT, counterparty=r"лизинг", confidence=0.9),
    # --- выручка ---
    Rule("REV_ACQUIRING", IN, r"эквайринг|операциям оплаты через|сбп|система быстрых платежей|по операциям с картами", confidence=0.98),
    Rule("REV_CASH", IN, r"взнос наличных|инкассирован|инкассац|внесение наличн|торгов\w+ выручк", confidence=0.98),
    Rule("REV_MARKETPLACE", IN, r"агентск\w+ договор|за вычетом (агентского )?вознаграждения|маркетплейс|отчет\w* агента|реализац\w+ товар\w* через"),
    Rule("REV_MARKETPLACE", IN, counterparty=r"маркетплейс|сервис бронирования|агрегатор", confidence=0.9),
    Rule("REFUND_IN", IN, r"возврат|страхов\w+ возмещен|ошибочно перечисл|гарантийн\w+ удержан|компенсац"),
    # --- аренда, коммуналка, банк ---
    Rule("RENT", OUT, r"аренд\w+ (нежилого|помещен|офис|склад|земельн|торгов)|арендн\w+ плат|обеспечительный платеж по договору аренды|за аренду (помещения|офиса|склада|земл)", confidence=0.97),
    Rule("RENT", OUT, counterparty=r"арендодатель", confidence=0.95),
    Rule("BANK_FEES", OUT, r"комисси|аренд\w+ pos|pos-терминал|обслуживание (счета|р/с)|смс-информирован|за ведение счета", confidence=0.98),
    Rule("UTILITIES", OUT, r"электроэнерг|водоснабжен|водоотведен|теплоснабж|отоплен|газоснабж|услуг\w* связи|интернет|тко|твердых коммунальных|вывоз (мусора|отходов)|коммунальн", confidence=0.97),
    Rule("UTILITIES", OUT, counterparty=r"энергосбыт|водоканал|телеком|оператор тко|теплосет|горгаз", confidence=0.95),
    # --- маркетинг, сервисы ---
    Rule("MARKETING", OUT, r"рекламн|реклам|продвижен|маркетинг|контекстн", confidence=0.95),
    Rule("MARKETING", OUT, counterparty=r"рекламн", confidence=0.9),
    Rule("SERVICES", OUT, r"бухгалтерск|клининг|уборк|охран|сопровожден|абонентск|облачн|лицензи|программн\w+ обеспечен|подписк|учетн\w+ систем|провоза по дорогам|расчетной записи|юридическ|аудит|страхован|полис|услуг\w* хранени|подработк|ремонт\w* и (техническ|обслуж)|техническ\w+ обслуживан", confidence=0.9),
    Rule("SUPPLIERS", OUT, r"топливн\w+ карт|за топливо|\bгсм\b|пополнение лицевого счета по договору", confidence=0.9),
    Rule("SERVICES", OUT, counterparty=r"бухгалтер|клининг|охранн|учетные системы|страхов|юрист", confidence=0.85),
    Rule("REFUND_OUT", OUT, r"возврат (денежных средств|покупател|клиент|аванса|предоплаты|оплаты)|ошибочно|неверно указан"),
    # --- закупки (широкие правила в конце) ---
    Rule("SUPPLIERS", OUT, r"за (товар|продукци|поставк|материал|сырь|запчаст|шины|оборудован|семена|удобрен|средства защиты|топлив|гсм|мебел|комплектующ|ткан|фурнитур|расходн)|по договору поставки|субподряд|купли-продажи|транспортно-экспедицион|за перевозку|за доставку", confidence=0.85),
    Rule("SUPPLIERS", OUT, r"оплата по (счету|упд|договору|накладной|акту)", confidence=0.7),
    # --- клиенты (широкие правила для поступлений) ---
    Rule("REV_CLIENTS", IN, r"оплата|предоплата|аванс|окончательный расчет|доплата|по счету|по договору|по упд|по акту|за услуг|за работ|за товар|за продукц|за перевозк|за изготовлен", confidence=0.85),
]


def rule_category(direction: str, purpose: str | None, counterparty: str | None) -> tuple[str, float, str]:
    """Возвращает (код категории, уверенность, источник)."""
    p = strip_vat_tail(normalize_text(purpose))
    c = normalize_text(counterparty)
    direction = IN if direction in (IN, "Поступление") else OUT
    for rule in RULES:
        if rule.matches(direction, p, c):
            return rule.code, rule.confidence, "rule"
    return fallback_for(direction), 0.0, "fallback"


def is_salary_advance(purpose: str | None) -> bool:
    return bool(re.search(r"аванс", normalize_text(purpose)))
