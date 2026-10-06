"""Price settings from the environment: an unedited example value is a configuration error,
not an unknown price (which would either block every call or, with
JETTAE_LLM_ALLOW_UNKNOWN_PRICE=1, record spending as null)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from jettae.llm.base import LLMError
from jettae.llm.budget import PriceTable


def test_rate_and_json_prices_are_read():
    t = PriceTable.from_env(
        {
            "JETTAE_LLM_KRW_PER_USD": "1400",
            "JETTAE_LLM_PRICES": (
                '{"m": {"input_krw_per_mtok": "6000", "output_krw_per_mtok": 30000}}'
            ),
        }
    )
    assert t.krw_per_usd == Decimal("1400")
    price = t.get("m")
    assert price is not None and price.output_krw_per_mtok == Decimal("30000")


def test_empty_values_mean_unset():
    t = PriceTable.from_env({"JETTAE_LLM_KRW_PER_USD": "", "JETTAE_LLM_PRICES": " "})
    assert t.krw_per_usd is None and t.get("m") is None


@pytest.mark.parametrize("rate", ["change-me-krw-per-usd", "0", "-3", "NaN"])
def test_placeholder_or_non_positive_rate_is_refused(rate):
    with pytest.raises(LLMError, match="JETTAE_LLM_KRW_PER_USD"):
        PriceTable.from_env({"JETTAE_LLM_KRW_PER_USD": rate})


@pytest.mark.parametrize(
    "prices",
    [
        "{not json",
        '{"m": {"input_krw_per_mtok": "<KRW>", "output_krw_per_mtok": "1"}}',
        '{"m": {"input_krw_per_mtok": "1"}}',
        '{"m": "6000"}',
    ],
)
def test_malformed_price_json_is_refused(prices):
    with pytest.raises(LLMError, match="JETTAE_LLM_PRICES"):
        PriceTable.from_env({"JETTAE_LLM_PRICES": prices})
