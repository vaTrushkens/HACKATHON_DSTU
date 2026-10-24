"""Import a control dataset into new isolated companies; never overwrite existing data."""
import io
from pathlib import PurePosixPath
import uuid
import zipfile

import pandas as pd
from django.db import transaction

from apps.accounts.models import CompanyMembership
from apps.operations.services import import_operations
from apps.planning.services import sync_recurring_patterns
from cashflow_core.ingest import load_any, read_statement_csv
from .models import Company

MAX_BYTES = 100 * 1024 * 1024


def read_dataset(filename, content):
    if len(content) > MAX_BYTES:
        raise ValueError("Размер набора превышает 100 МБ")
    if not filename.lower().endswith('.zip'):
        return load_any(filename, content), pd.DataFrame()
    try:
        archive = zipfile.ZipFile(io.BytesIO(content))
    except zipfile.BadZipFile as exc:
        raise ValueError("Повреждённый ZIP-архив") from exc
    with archive:
        files = [f for f in archive.infolist() if not f.is_dir()]
        if sum(f.file_size for f in files) > MAX_BYTES or len(files) > 1000:
            raise ValueError("Архив слишком большой")
        meta = pd.DataFrame()
        combined = []
        all_operations = None
        for f in files:
            if PurePosixPath(f.filename).name == 'companies.csv':
                meta = read_statement_csv(io.BytesIO(archive.read(f)))
            elif PurePosixPath(f.filename).name == 'all_operations.csv':
                all_operations = load_any(f.filename, archive.read(f))
            elif f.filename.lower().endswith('.csv') and 'statements' in PurePosixPath(f.filename).parts:
                frame = load_any(f.filename, archive.read(f))
                if 'company_id' not in frame:
                    frame['company_id'] = PurePosixPath(f.filename).stem
                combined.append(frame)
        if all_operations is not None:
            return all_operations, meta
        if not combined:
            raise ValueError("В архиве нужны all_operations.csv или statements/*.csv")
        return pd.concat(combined, ignore_index=True), meta


@transaction.atomic
def import_dataset(user, filename, content):
    raw, meta = read_dataset(filename, content)
    if raw.empty:
        raise ValueError("Набор не содержит операций")
    if 'company_id' not in raw:
        raw['company_id'] = PurePosixPath(filename).stem[:128] or 'company'
    raw['company_id'] = raw.company_id.fillna('').astype(str).str.strip()
    if raw.company_id.eq('').any():
        raise ValueError("Часть строк не содержит company_id")
    if raw.company_id.nunique() > 300:
        raise ValueError("За один раз можно загрузить до 300 компаний")
    dataset_id = uuid.uuid4()
    result = []
    for code, frame in raw.groupby('company_id', sort=True):
        details = frame.iloc[0].fillna('').to_dict()
        if not meta.empty and 'company_id' in meta:
            selected = meta[meta.company_id.astype(str) == code]
            if len(selected):
                details.update(selected.iloc[0].fillna('').to_dict())
        company = Company.objects.create(code='D' + uuid.uuid4().hex[:15], external_code=code[:128], dataset_id=dataset_id,
            name=str(details.get('company_name') or code)[:255], inn=str(details.get('company_inn') or '')[:12],
            industry=str(details.get('industry') or '')[:128], legal_form=str(details.get('legal_form') or '')[:16])
        CompanyMembership.objects.create(user=user, company=company)
        report = import_operations(company, frame)
        if not report.rows_new:
            raise ValueError(f"В компании {code} нет корректных операций")
        sync_recurring_patterns(company)
        result.append({'company': company, 'report': report.as_dict()})
    return dataset_id, result
