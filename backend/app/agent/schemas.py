"""
Core data models for the company research pipeline.
All monetary values in USD unless noted; None means data unavailable.
"""
from enum import StrEnum
from typing import Any
from pydantic import BaseModel, Field


class CompanyType(StrEnum):
    PUBLIC = "public"
    PRIVATE = "private"
    UNKNOWN = "unknown"


class Confidence(StrEnum):
    HIGH = "high"      # structured API data or primary source
    MEDIUM = "medium"  # scraped / synthesized from multiple sources
    LOW = "low"        # LLM inference, single source, stale data


class Evidence(BaseModel):
    value: Any
    source: str
    confidence: Confidence
    note: str | None = None


class FinancialYear(BaseModel):
    year: int
    revenue: float | None = None         # USD
    net_income: float | None = None      # USD
    gross_profit: float | None = None    # USD
    operating_income: float | None = None
    ebitda: float | None = None
    eps: float | None = None
    free_cash_flow: float | None = None


class Financials(BaseModel):
    market_cap: float | None = None
    enterprise_value: float | None = None
    pe_ratio: float | None = None
    revenue_ttm: float | None = None
    gross_margin: float | None = None
    net_margin: float | None = None
    revenue_growth_yoy: float | None = None
    annual: list[FinancialYear] = Field(default_factory=list)
    currency: str = "USD"
    data_source: str = "unknown"


class Competitor(BaseModel):
    name: str
    ticker: str | None = None
    market_cap: float | None = None
    revenue_ttm: float | None = None
    summary: str | None = None


class ResolvedCompany(BaseModel):
    name: str
    ticker: str | None = None
    type: CompanyType = CompanyType.UNKNOWN
    exchange: str | None = None
    sector: str | None = None
    industry: str | None = None
    country: str | None = None
    website: str | None = None
    description: str | None = None
    founded: str | None = None
    headquarters: str | None = None


class Portfolio(BaseModel):
    company: ResolvedCompany
    headcount: Evidence | None = None
    financials: Financials = Field(default_factory=Financials)
    market_position: Evidence | None = None
    key_products: list[str] = Field(default_factory=list)
    competitors: list[Competitor] = Field(default_factory=list)
    recent_news: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    opportunities: list[str] = Field(default_factory=list)
    analyst_summary: str | None = None
    data_gaps: list[str] = Field(default_factory=list)


class ResearchRequest(BaseModel):
    company_name: str = Field(min_length=1, max_length=200)
    disable_web_search: bool = False


class ResearchJob(BaseModel):
    job_id: str
    company_name: str
    status: str = "queued"       # queued | running | done | error
    disable_web_search: bool = False
    portfolio: Portfolio | None = None
    excel_ready: bool = False
    error: str | None = None
