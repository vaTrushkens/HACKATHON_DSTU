"""Reconcile known obligations with inferred events and statistical cash flows."""
import numpy as np
import pandas as pd

from ..categories import VARIABLE_CODES


def _text(value):
    return '' if value is None or pd.isna(value) else str(value).strip().casefold()


def same_counterparty(a, b):
    ai, bi = _text(a.get('counterparty_inn')), _text(b.get('counterparty_inn'))
    if ai and bi:
        return ai == bi
    an, bn = _text(a.get('counterparty_name')), _text(b.get('counterparty_name'))
    return bool(an and an == bn)


def reconcile_events(inferred, planned):
    """One document replaces one inferred occurrence, never an entire category."""
    if planned is None or planned.empty:
        return inferred.copy(), 0
    inferred = inferred.copy()
    planned = planned.copy().sort_values('date')
    removed = set()
    accepted = []
    reconciled = 0
    for row in planned.to_dict('records'):
        # Across documents, collapse only an explicitly shared obligation identifier.
        obligation = _text(row.get('obligation_key'))
        if obligation and any(p.get('obligation_key') == obligation for p in accepted):
            reconciled += 1
            continue
        candidates = []
        for i, auto in inferred.iterrows():
            if i in removed or auto.direction != row['direction'] or auto.category != row['category']:
                continue
            delta = abs((pd.Timestamp(auto.date) - pd.Timestamp(row['date'])).days)
            ref_match = bool(_text(row.get('external_ref'))) and row.get('external_ref') == auto.get('pattern_key')
            cp_match = same_counterparty(auto, row) or row['category'] == 'TAXES'
            if delta <= 7 and (ref_match or (cp_match and abs(float(auto.amount) - float(row['amount'])) <= max(float(auto.amount) * .15, .01))):
                candidates.append((delta, i))
        if candidates:
            removed.add(min(candidates)[1])
            reconciled += 1
        accepted.append(row)
    return pd.concat([inferred.drop(index=list(removed)), pd.DataFrame(accepted)], ignore_index=True), reconciled


def residual_means(ops, events, means, as_of):
    """Known cash flows replace the matching historical share of aggregate forecasts.

    The budget is capped by both the known amount and the historical counterparty
    share. Unrelated/new counterparties remain additive. Applied before scenarios,
    so delaying a payment cannot resurrect it in the statistical layer.
    """
    result = means.copy()
    removed = {'in': 0.0, 'out': 0.0}
    if ops.empty or events.empty:
        return result, removed
    recent = ops[ops.operation_date > as_of - pd.Timedelta(days=91)]
    for direction, key, col in [('Поступление', 'in', 'inflow_mean'), ('Списание', 'out', 'outflow_mean')]:
        history = recent[(recent.direction == direction) & recent.category.isin(VARIABLE_CODES)]
        known = events[(events.direction == direction) & events.category.isin(VARIABLE_CODES)]
        total = float(history.amount.sum())
        budget = float(result[col].sum())
        if total <= 0 or budget <= 0:
            continue
        # Group documents by the same counterparty/category to avoid reusing a share.
        buckets = {}
        for event in known.to_dict('records'):
            identity = (event['category'], _text(event.get('counterparty_inn')) or _text(event.get('counterparty_name')))
            if not identity[1]:
                continue
            buckets.setdefault(identity, []).append(event)
        deduction = 0.0
        for (category, _), group in buckets.items():
            matched = history[history.category == category]
            matched = matched[matched.apply(lambda row: same_counterparty(row, group[0]), axis=1)] if len(matched) else matched
            share = float(matched.amount.sum()) / total
            deduction += min(sum(float(e['amount']) for e in group), budget * share)
        deduction = min(deduction, budget)
        result[col] *= max(0., 1. - deduction / budget)
        removed[key] = round(deduction, 2)
    return result, removed
