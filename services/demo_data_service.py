from __future__ import annotations

import random
from datetime import date, datetime, time, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from db.models import FiDocHeader, FiDocItem
from utils.config import settings


def seed_holiday_violations(db: Session, count: int = 10) -> dict:
    tenant_id = settings.default_tenant_id
    user_id = settings.default_user_id

    today = date.today()
    saturday = today
    while saturday.weekday() != 5:
        saturday = date.fromordinal(saturday.toordinal() + 1)

    inserted = 0
    keys: list[str] = []

    for i in range(count):
        belnr = f"P{i+1:09d}"[-10:]
        exists = db.scalar(
            select(FiDocHeader).where(
                FiDocHeader.tenant_id == tenant_id,
                FiDocHeader.bukrs == "1000",
                FiDocHeader.belnr == belnr,
                FiDocHeader.gjahr == str(saturday.year),
            )
        )
        if exists:
            continue

        header = FiDocHeader(
            tenant_id=tenant_id,
            bukrs="1000",
            belnr=belnr,
            gjahr=str(saturday.year),
            user_id=user_id,
            doc_source="SAP",
            budat=saturday,
            cpudt=saturday,
            cputm=time(hour=random.choice([22, 23, 1]), minute=random.randint(0, 59), second=random.randint(0, 59)),
            blart="SA",
            waers="KRW",
            bktxt=f"POC 휴일 식대 {i+1}",
            xblnr=f"DEMO-{i+1}",
            intended_risk_type="HOLIDAY_USAGE",
            hr_status="LEAVE",
            mcc_code="5813",
            budget_exceeded_flag="Y",
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        item = FiDocItem(
            tenant_id=tenant_id,
            bukrs="1000",
            belnr=belnr,
            gjahr=str(saturday.year),
            buzei="001",
            hkont="0000601000",
            wrbtr=random.randint(30000, 150000),
            waers="KRW",
            lifnr=f"C{1000+i}",
            sgtxt="심야 식대",
        )
        db.add(header)
        db.add(item)
        inserted += 1
        keys.append(f"1000-{belnr}-{saturday.year}")

    db.commit()
    return {"inserted": inserted, "voucher_keys": keys}


def clear_demo_data(db: Session) -> dict:
    tenant_id = settings.default_tenant_id
    headers = db.scalars(
        select(FiDocHeader).where(
            FiDocHeader.tenant_id == tenant_id,
            FiDocHeader.belnr.like("P%"),
            FiDocHeader.xblnr.like("DEMO-%"),
        )
    ).all()
    count = 0
    for h in headers:
        items = db.scalars(
            select(FiDocItem).where(
                FiDocItem.tenant_id == h.tenant_id,
                FiDocItem.bukrs == h.bukrs,
                FiDocItem.belnr == h.belnr,
                FiDocItem.gjahr == h.gjahr,
            )
        ).all()
        for it in items:
            db.delete(it)
        db.delete(h)
        count += 1
    db.commit()
    return {"deleted": count}
