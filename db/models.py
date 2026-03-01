from __future__ import annotations

from sqlalchemy import BigInteger, Date, DateTime, Numeric, String, Text, Time
from sqlalchemy.orm import Mapped, mapped_column

from db.session import Base


class FiDocHeader(Base):
    __tablename__ = "fi_doc_header"
    __table_args__ = {"schema": "dwp_aura"}

    tenant_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    bukrs: Mapped[str] = mapped_column(String(4), primary_key=True)
    belnr: Mapped[str] = mapped_column(String(10), primary_key=True)
    gjahr: Mapped[str] = mapped_column(String(4), primary_key=True)

    user_id: Mapped[int | None] = mapped_column(BigInteger)
    budat: Mapped[object | None] = mapped_column(Date)
    cputm: Mapped[object | None] = mapped_column(Time)
    blart: Mapped[str | None] = mapped_column(String(2))
    waers: Mapped[str | None] = mapped_column(String(5))
    bktxt: Mapped[str | None] = mapped_column(String(200))
    xblnr: Mapped[str | None] = mapped_column(String(30))

    intended_risk_type: Mapped[str | None] = mapped_column(String(50))
    hr_status: Mapped[str | None] = mapped_column(String(20))
    mcc_code: Mapped[str | None] = mapped_column(String(20))
    budget_exceeded_flag: Mapped[str | None] = mapped_column(String(1))

    created_at: Mapped[object | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[object | None] = mapped_column(DateTime(timezone=True))


class FiDocItem(Base):
    __tablename__ = "fi_doc_item"
    __table_args__ = {"schema": "dwp_aura"}

    tenant_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    bukrs: Mapped[str] = mapped_column(String(4), primary_key=True)
    belnr: Mapped[str] = mapped_column(String(10), primary_key=True)
    gjahr: Mapped[str] = mapped_column(String(4), primary_key=True)
    buzei: Mapped[str] = mapped_column(String(3), primary_key=True)

    hkont: Mapped[str | None] = mapped_column(String(10))
    wrbtr: Mapped[float | None] = mapped_column(Numeric(18, 2))
    waers: Mapped[str | None] = mapped_column(String(5))
    lifnr: Mapped[str | None] = mapped_column(String(20))
    sgtxt: Mapped[str | None] = mapped_column(String(200))


class AgentCase(Base):
    __tablename__ = "agent_case"
    __table_args__ = {"schema": "dwp_aura"}

    case_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(BigInteger)

    detected_at: Mapped[object | None] = mapped_column(DateTime(timezone=True))
    bukrs: Mapped[str | None] = mapped_column(String(4))
    belnr: Mapped[str | None] = mapped_column(String(10))
    gjahr: Mapped[str | None] = mapped_column(String(4))
    buzei: Mapped[str | None] = mapped_column(String(3))

    case_type: Mapped[str | None] = mapped_column(String(50))
    severity: Mapped[str | None] = mapped_column(String(10))
    score: Mapped[float | None] = mapped_column(Numeric(6, 4))
    reason_text: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str | None] = mapped_column(String(30))


class CaseAnalysisResult(Base):
    __tablename__ = "case_analysis_result"
    __table_args__ = {"schema": "dwp_aura"}

    run_id: Mapped[str] = mapped_column(String, primary_key=True)
    tenant_id: Mapped[int | None] = mapped_column(BigInteger)
    score: Mapped[float | None] = mapped_column(Numeric(5, 2))
    severity: Mapped[str | None] = mapped_column(String(20))
    reason_text: Mapped[str | None] = mapped_column(Text)
