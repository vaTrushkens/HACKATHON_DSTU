"""Справочник категорий операций.

``kind`` определяет, как категория участвует в прогнозе:

* ``fixed``     — обязательные/регулярные платежи, их ищет детектор регулярности
                  и они попадают в детерминированный слой прогноза;
* ``variable``  — операционные потоки (выручка, закупки), их прогнозирует
                  статистическая модель;
* ``financing`` — внешнее финансирование и «спасательные» вливания владельца.
                  В базовый прогноз НЕ включаются: именно их отсутствие и есть
                  кассовый разрыв, который мы хотим увидеть заранее.
"""
from dataclasses import dataclass

IN = "in"
OUT = "out"


@dataclass(frozen=True)
class Category:
    code: str
    label: str
    direction: str
    kind: str  # fixed | variable | financing
    mandatory: bool = False  # обязательный платёж (зарплата, налоги, аренда, кредит)


CATEGORIES: list[Category] = [
    # ---- поступления ----
    Category("REV_ACQUIRING", "Эквайринг и СБП", IN, "variable"),
    Category("REV_CASH", "Внесение наличной выручки", IN, "variable"),
    Category("REV_CLIENTS", "Оплата от клиентов", IN, "variable"),
    Category("REV_MARKETPLACE", "Выплаты маркетплейсов и агентов", IN, "variable"),
    Category("REFUND_IN", "Возвраты и возмещения", IN, "variable"),
    Category("LOAN_IN", "Получение кредита", IN, "financing"),
    Category("OWNER_IN", "Вливания собственника", IN, "financing"),
    Category("OTHER_IN", "Прочие поступления", IN, "variable"),
    # ---- списания ----
    Category("SALARY", "Зарплата", OUT, "fixed", mandatory=True),
    Category("CONTRACTORS", "Подрядчики-физлица (ГПХ, самозанятые)", OUT, "fixed", mandatory=True),
    Category("TAXES", "Налоги и взносы (ЕНП)", OUT, "fixed", mandatory=True),
    Category("RENT", "Аренда", OUT, "fixed", mandatory=True),
    Category("LOAN_OUT", "Кредиты и лизинг", OUT, "fixed", mandatory=True),
    Category("UTILITIES", "Коммунальные услуги и связь", OUT, "fixed"),
    Category("BANK_FEES", "Комиссии банка", OUT, "fixed"),
    Category("SERVICES", "Услуги и сервисы", OUT, "fixed"),
    Category("MARKETING", "Реклама и маркетинг", OUT, "variable"),
    Category("SUPPLIERS", "Поставщики и закупки", OUT, "variable"),
    Category("REFUND_OUT", "Возвраты покупателям", OUT, "variable"),
    Category("FINES", "Штрафы и взыскания", OUT, "variable"),
    Category("OWNER_OUT", "Выплаты собственнику", OUT, "variable"),
    Category("OTHER_OUT", "Прочие списания", OUT, "variable"),
]

BY_CODE: dict[str, Category] = {c.code: c for c in CATEGORIES}
LABELS: dict[str, str] = {c.code: c.label for c in CATEGORIES}
FINANCING_CODES = frozenset(c.code for c in CATEGORIES if c.kind == "financing")
FIXED_CODES = frozenset(c.code for c in CATEGORIES if c.kind == "fixed")
VARIABLE_CODES = frozenset(c.code for c in CATEGORIES if c.kind == "variable")
MANDATORY_CODES = frozenset(c.code for c in CATEGORIES if c.mandatory)


def fallback_for(direction: str) -> str:
    return "OTHER_IN" if direction == IN else "OTHER_OUT"
