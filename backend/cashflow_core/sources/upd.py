"""УПД / счёт-фактура в XML-формате ФНС (приказы ММВ-7-15/820@ и ЕД-7-26/970@, форматы 5.01–5.03) —
то, что выгружают Диадок, СБИС и другие операторы ЭДО.

Из документа берутся номер и дата (``СвСчФакт@НомерСчФ/ДатаСчФ`` или ``@НомерДок/ДатаДок``), продавец и покупатель
(``СвПрод``/``СвПокуп`` → ``СвЮЛУч@ИННЮЛ,НаимОрг`` или ``СвИП@ИННФЛ`` + ``ФИО``) и итог с НДС (``ВсегоОпл@СтТовУчНалВсего``).
Направление определяет импортёр: если ИНН продавца — наш, документ выставлен нами и ожидается поступление.
"""
from __future__ import annotations

import xml.etree.ElementTree as ET

from .base import SourceInvoice, child, local, parse_amount, parse_date


def _party(el) -> tuple[str, str]:
    idsv = child(el, "ИдСв")
    if idsv is None:
        return "", ""
    ul = child(idsv, "СвЮЛУч")
    if ul is not None:
        return ul.get("ИННЮЛ", ""), ul.get("НаимОрг", "")
    ip = child(idsv, "СвИП")
    if ip is not None:
        fio = child(ip, "ФИО")
        name = " ".join(filter(None, [fio.get("Фамилия"), fio.get("Имя"), fio.get("Отчество")])) if fio is not None else ""
        return ip.get("ИННФЛ", ""), f"ИП {name}".strip()
    return "", ""


def parse_upd(data: bytes) -> list[SourceInvoice]:
    root = ET.fromstring(data)
    out: list[SourceInvoice] = []
    for doc in root.iter():
        if local(doc.tag) != "Документ":
            continue
        head = child(doc, "СвСчФакт")
        if head is None:
            continue
        number = head.get("НомерСчФ") or head.get("НомерДок") or ""
        date = parse_date(head.get("ДатаСчФ") or head.get("ДатаДок"))
        seller_inn, seller_name = _party(child(head, "СвПрод"))
        buyer_inn, buyer_name = _party(child(head, "СвПокуп"))
        total = None
        tbl = child(doc, "ТаблСчФакт")
        if tbl is not None:
            tot = child(tbl, "ВсегоОпл")
            if tot is not None:
                total = parse_amount(tot.get("СтТовУчНалВсего") or tot.get("СтТовБезНДСВсего"))
            if total is None:
                rows = [parse_amount(r.get("СтТовУчНал")) or 0.0 for r in tbl if local(r.tag) == "СведТов"]
                total = sum(rows) if rows else None
        if not total or total <= 0:
            continue
        func = doc.get("Функция", "")
        out.append(
            SourceInvoice(
                number=number, date=date, amount=total, seller_inn=seller_inn, seller_name=seller_name, buyer_inn=buyer_inn, buyer_name=buyer_name,
                label=f"УПД № {number}".strip() if number else "УПД", external_id=f"{number}:{date}" if number else "", kind=func,
            )
        )
    return out
