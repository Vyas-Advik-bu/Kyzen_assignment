"""Pydantic schema validation tests."""
import pytest
from pydantic import ValidationError

from app.agent.schemas import (
    Portfolio, ResolvedCompany, Financials, FinancialYear,
    Evidence, Confidence, CompanyType, ResearchRequest,
)


class TestResolvedCompany:
    def test_minimal(self):
        c = ResolvedCompany(name="Apple Inc")
        assert c.name == "Apple Inc"
        assert c.type == CompanyType.UNKNOWN
        assert c.ticker is None

    def test_public(self):
        c = ResolvedCompany(name="Apple Inc", ticker="AAPL", type=CompanyType.PUBLIC)
        assert c.ticker == "AAPL"


class TestFinancials:
    def test_defaults_are_none(self):
        f = Financials()
        assert f.market_cap is None
        assert f.annual == []

    def test_annual_years(self):
        f = Financials(annual=[
            FinancialYear(year=2023, revenue=1e11),
            FinancialYear(year=2022, revenue=9e10),
        ])
        assert len(f.annual) == 2
        assert f.annual[0].year == 2023


class TestPortfolio:
    def test_round_trip(self):
        p = Portfolio(
            company=ResolvedCompany(name="Test Corp", ticker="TST"),
            financials=Financials(market_cap=1e9),
            headcount=Evidence(value=5000, source="web", confidence=Confidence.MEDIUM),
        )
        dumped = p.model_dump()
        assert dumped["company"]["name"] == "Test Corp"
        assert dumped["financials"]["market_cap"] == 1e9

    def test_data_gaps_default_empty(self):
        p = Portfolio(company=ResolvedCompany(name="X"))
        assert p.data_gaps == []


class TestResearchRequest:
    def test_rejects_empty_name(self):
        with pytest.raises(ValidationError):
            ResearchRequest(company_name="")

    def test_rejects_too_long(self):
        with pytest.raises(ValidationError):
            ResearchRequest(company_name="x" * 201)

    def test_accepts_valid(self):
        r = ResearchRequest(company_name="Apple Inc")
        assert r.company_name == "Apple Inc"
