"""
tests/unit/test_repricing_engine.py — RepricingEngine Unit Tests

Covers:
  - custom_id length invariant: must be <=64 chars even with full-length UUIDs
  - custom_id_map is returned in BatchSubmitResult and maps to correct product_ids
  - retrieve_batch_results() uses custom_id_to_product_id dict (not string parsing)
  - retrieve_batch_results() logs CRITICAL and skips unknown custom_ids
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from core.repricing_engine import (
    BatchSubmitResult,
    CompetitorProduct,
    MyProduct,
    RepricingEngine,
    RepricingRecommendation,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_USER_ID = str(uuid.uuid4())   # 36 chars
_PRODUCT_ID_A = str(uuid.uuid4())   # 36 chars
_PRODUCT_ID_B = str(uuid.uuid4())   # 36 chars

ANTHROPIC_CUSTOM_ID_MAX_LEN = 64


def _make_product(product_id: str, user_id: str = _USER_ID) -> MyProduct:
    """Return a minimal valid MyProduct for testing."""
    return MyProduct(
        product_id=product_id,
        platform_product_id="ASIN123456",
        title="Test Widget",
        platform="amazon",
        current_price=29.99,
        cost=10.00,
        min_margin_floor=5.00,
        user_id=user_id,
    )


def _make_competitor(price: float = 27.99) -> CompetitorProduct:
    """Return a minimal valid CompetitorProduct for testing."""
    return CompetitorProduct(
        price=price,
        platform="amazon",
        condition="new",
    )


def _make_mock_batch_response(batch_id: str = "batch_test123") -> MagicMock:
    """Return a mock Anthropic batch creation response."""
    mock = MagicMock()
    mock.id = batch_id
    mock.processing_status = "in_progress"
    return mock


# ---------------------------------------------------------------------------
# custom_id length invariant
# ---------------------------------------------------------------------------


class TestCustomIdLength:
    """Anthropic rejects custom_ids longer than 64 characters."""

    def test_custom_id_within_limit_for_single_product(self) -> None:
        """custom_id must be <=64 chars even when user_id and product_id are both full UUIDs."""
        mock_client = MagicMock()
        mock_client.messages.batches.create.return_value = _make_mock_batch_response()

        with patch("anthropic.Anthropic", return_value=mock_client):
            engine = RepricingEngine(api_key="sk-ant-test-key")

        product = _make_product(_PRODUCT_ID_A)
        competitor = _make_competitor()

        with patch.object(engine, "_client", mock_client):
            result = engine.submit_batch(
                user_id=_USER_ID,
                products_with_competitors=[(product, [competitor])],
            )

        # custom_id_map keys are the exact custom_ids passed to Anthropic
        for custom_id in result.custom_id_map:
            assert len(custom_id) <= ANTHROPIC_CUSTOM_ID_MAX_LEN, (
                f"custom_id '{custom_id}' is {len(custom_id)} chars — "
                f"exceeds Anthropic's {ANTHROPIC_CUSTOM_ID_MAX_LEN}-char limit"
            )

    def test_custom_id_within_limit_for_multiple_products(self) -> None:
        """All custom_ids in a multi-product batch must be <=64 chars."""
        mock_client = MagicMock()
        mock_client.messages.batches.create.return_value = _make_mock_batch_response()

        with patch("anthropic.Anthropic", return_value=mock_client):
            engine = RepricingEngine(api_key="sk-ant-test-key")

        products_and_competitors = [
            (_make_product(str(uuid.uuid4())), [_make_competitor()])
            for _ in range(10)
        ]

        with patch.object(engine, "_client", mock_client):
            result = engine.submit_batch(
                user_id=_USER_ID,
                products_with_competitors=products_and_competitors,
            )

        for custom_id in result.custom_id_map:
            assert len(custom_id) <= ANTHROPIC_CUSTOM_ID_MAX_LEN, (
                f"custom_id '{custom_id}' is {len(custom_id)} chars"
            )

    def test_old_format_would_have_exceeded_limit(self) -> None:
        """Confirm the old '{user_id}:{product_id}' format exceeds 64 chars — regression guard."""
        old_format = f"{_USER_ID}:{_PRODUCT_ID_A}"
        assert len(old_format) > ANTHROPIC_CUSTOM_ID_MAX_LEN, (
            "If this fails, the old format no longer triggers the bug — update the test"
        )


# ---------------------------------------------------------------------------
# custom_id_map correctness
# ---------------------------------------------------------------------------


class TestCustomIdMap:
    """BatchSubmitResult.custom_id_map must map every custom_id to the correct product_id."""

    def test_custom_id_map_has_entry_per_product(self) -> None:
        """custom_id_map must have exactly one entry per submitted product."""
        mock_client = MagicMock()
        mock_client.messages.batches.create.return_value = _make_mock_batch_response()

        with patch("anthropic.Anthropic", return_value=mock_client):
            engine = RepricingEngine(api_key="sk-ant-test-key")

        product_a = _make_product(_PRODUCT_ID_A)
        product_b = _make_product(_PRODUCT_ID_B)

        with patch.object(engine, "_client", mock_client):
            result = engine.submit_batch(
                user_id=_USER_ID,
                products_with_competitors=[
                    (product_a, [_make_competitor()]),
                    (product_b, [_make_competitor(25.00)]),
                ],
            )

        assert isinstance(result, BatchSubmitResult)
        assert len(result.custom_id_map) == 2
        assert set(result.custom_id_map.values()) == {_PRODUCT_ID_A, _PRODUCT_ID_B}

    def test_custom_id_map_keys_match_requests_sent_to_anthropic(self) -> None:
        """Keys in custom_id_map must exactly match the custom_ids in the Anthropic requests."""
        mock_client = MagicMock()
        mock_client.messages.batches.create.return_value = _make_mock_batch_response()

        with patch("anthropic.Anthropic", return_value=mock_client):
            engine = RepricingEngine(api_key="sk-ant-test-key")

        product = _make_product(_PRODUCT_ID_A)

        with patch.object(engine, "_client", mock_client):
            result = engine.submit_batch(
                user_id=_USER_ID,
                products_with_competitors=[(product, [_make_competitor()])],
            )

        # BatchRequest is a TypedDict at runtime — use dict access, not attribute access
        call_args = mock_client.messages.batches.create.call_args
        requests_sent = call_args.kwargs.get("requests") or call_args.args[0]
        sent_custom_ids = {req["custom_id"] for req in requests_sent}

        assert sent_custom_ids == set(result.custom_id_map.keys())

    def test_custom_id_map_values_are_product_ids(self) -> None:
        """Values in custom_id_map must be the product UUIDs passed to submit_batch."""
        mock_client = MagicMock()
        mock_client.messages.batches.create.return_value = _make_mock_batch_response()

        with patch("anthropic.Anthropic", return_value=mock_client):
            engine = RepricingEngine(api_key="sk-ant-test-key")

        product = _make_product(_PRODUCT_ID_A)

        with patch.object(engine, "_client", mock_client):
            result = engine.submit_batch(
                user_id=_USER_ID,
                products_with_competitors=[(product, [_make_competitor()])],
            )

        assert list(result.custom_id_map.values()) == [_PRODUCT_ID_A]


# ---------------------------------------------------------------------------
# retrieve_batch_results — custom_id lookup
# ---------------------------------------------------------------------------


class TestRetrieveBatchResults:
    """retrieve_batch_results() must use the custom_id_to_product_id dict, not string parsing."""

    def _make_succeeded_result(self, custom_id: str, price: float = 25.99) -> MagicMock:
        """Build a mock Anthropic batch result with a succeeded response."""
        content_block = MagicMock()
        content_block.type = "text"
        content_block.text = (
            f'{{"recommended_price": {price}, "strategy": "undercut", '
            f'"action": "decrease", "confidence": 85, '
            f'"reasoning": "Lower than competitors.", '
            f'"competitor_low": 24.99, "competitor_count": 3}}'
        )
        message = MagicMock()
        message.content = [content_block]
        message.stop_reason = "end_turn"

        result_inner = MagicMock()
        result_inner.type = "succeeded"
        result_inner.message = message

        batch_result = MagicMock()
        batch_result.custom_id = custom_id
        batch_result.result = result_inner
        return batch_result

    def test_successful_lookup_via_custom_id_map(self) -> None:
        """Results are matched to products using custom_id_to_product_id, not string parsing."""
        mock_client = MagicMock()
        custom_id = "a1b2c3d4"   # 8 hex chars
        mock_client.messages.batches.results.return_value = [
            self._make_succeeded_result(custom_id)
        ]

        with patch("anthropic.Anthropic", return_value=mock_client):
            engine = RepricingEngine(api_key="sk-ant-test-key")

        product = _make_product(_PRODUCT_ID_A)

        with patch.object(engine, "_client", mock_client):
            recommendations = engine.retrieve_batch_results(
                batch_id="batch_test123",
                products_by_id={_PRODUCT_ID_A: product},
                custom_id_to_product_id={custom_id: _PRODUCT_ID_A},
            )

        assert len(recommendations) == 1
        assert recommendations[0].product_id == _PRODUCT_ID_A

    def test_unknown_custom_id_is_skipped_with_critical_log(self) -> None:
        """A custom_id absent from custom_id_to_product_id logs CRITICAL and is skipped."""
        mock_client = MagicMock()
        mock_client.messages.batches.results.return_value = [
            self._make_succeeded_result("unknownid")
        ]

        with patch("anthropic.Anthropic", return_value=mock_client):
            engine = RepricingEngine(api_key="sk-ant-test-key")

        product = _make_product(_PRODUCT_ID_A)

        with patch.object(engine, "_client", mock_client):
            with patch("core.repricing_engine.logger") as mock_logger:
                recommendations = engine.retrieve_batch_results(
                    batch_id="batch_test123",
                    products_by_id={_PRODUCT_ID_A: product},
                    custom_id_to_product_id={},   # empty — no valid mappings
                )

        assert recommendations == []
        mock_logger.critical.assert_called_once()
        critical_call_msg = mock_logger.critical.call_args[0][0]
        assert "unrecognised custom_id" in critical_call_msg

    def test_custom_id_map_keys_are_8_hex_chars(self) -> None:
        """Generated custom_ids are exactly 8 lowercase hex characters."""
        mock_client = MagicMock()
        mock_client.messages.batches.create.return_value = _make_mock_batch_response()

        with patch("anthropic.Anthropic", return_value=mock_client):
            engine = RepricingEngine(api_key="sk-ant-test-key")

        product = _make_product(_PRODUCT_ID_A)

        with patch.object(engine, "_client", mock_client):
            result = engine.submit_batch(
                user_id=_USER_ID,
                products_with_competitors=[(product, [_make_competitor()])],
            )

        for custom_id in result.custom_id_map:
            assert len(custom_id) == 8
            assert custom_id.isalnum() and custom_id == custom_id.lower()
