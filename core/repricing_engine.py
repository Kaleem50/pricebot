"""
core/repricing_engine.py — PriceBot AI Repricing Engine

Platform-agnostic repricing brain.  Accepts structured product and competitor
data from any supported platform (Amazon, Etsy, eBay, Shopify, WooCommerce),
builds Anthropic Batch API requests with a cached system prompt, and returns
typed RepricingRecommendation objects after applying mandatory fail-safe
guardrails.

Design constraints (from CLAUDE.md / ARCHITECTURE.md / SECURITY.md / COSTS.md):
  - Model:       claude-haiku-4-5 ONLY — no Sonnet or Opus.
  - Delivery:    Anthropic Batch API only — never synchronous messages.create().
  - Caching:     System prompt tagged cache_control="ephemeral" on every request.
  - Guardrail:   final_price = max(claude_price, cost + min_margin_floor) — NEVER bypassed.
  - Logging:     Structured JSON via logging module — zero print() calls.
  - Types:       Full Pydantic v2 models on every public boundary.
  - State:       IDLE → BATCH_SUBMITTED → PROCESSING → SYNCED | FAILED
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from enum import Enum
from typing import Any, Final, Literal

import anthropic
from anthropic.types.message_create_params import MessageCreateParamsNonStreaming
from anthropic.types.messages.batch_create_params import Request as BatchRequest
from pydantic import BaseModel, Field, field_validator, model_validator

# ---------------------------------------------------------------------------
# Module-level logger — structured JSON format configured at app startup.
# Never use print() anywhere in this module.
# ---------------------------------------------------------------------------
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: The only permitted model for repricing jobs (COSTS.md §1).
#: Changing this to Sonnet or Opus is a margin-destruction event.
REPRICING_MODEL: Final[str] = "claude-haiku-4-5"

#: Maximum output tokens per repricing call.
#: Structured JSON output is always <200 tokens; 512 is a hard safety ceiling.
MAX_OUTPUT_TOKENS: Final[int] = 512

#: Price ceiling multiplier — a recommended price above (current × ceiling)
#: is treated as a guardrail violation and aborts the update.
PRICE_CEILING_MULTIPLIER: Final[float] = 10.0

#: Supported platform identifiers — must match platform_connections.platform CHECK constraint.
SupportedPlatform = Literal["amazon", "etsy", "shopify", "ebay", "woocommerce"]

# ---------------------------------------------------------------------------
# System prompt (cached)
# ---------------------------------------------------------------------------

#: Repricing system prompt — identical for every call, so prompt caching
#: eliminates ~90% of repeated input token cost (COSTS.md §2.2).
#: Minimum 1024 tokens required for cache eligibility; this prompt exceeds that.
REPRICING_SYSTEM_PROMPT: Final[str] = """
You are a pricing intelligence engine for PriceBot, an AI-powered ecommerce repricing tool.

YOUR ROLE
Analyse the seller's product data and live competitor prices, then recommend the single
optimal price that keeps the seller competitive while protecting their profit margin.

INPUTS YOU WILL RECEIVE (JSON)
{
  "product": {
    "platform":          string,   // "amazon" | "etsy" | "ebay" | "shopify" | "woocommerce"
    "title":             string,
    "current_price":     number,   // seller's current listed price
    "cost":              number,   // seller's cost of goods (COGS) — may be null
    "min_margin_floor":  number,   // absolute minimum safe price (cost + required margin)
    "platform_context":  object    // platform-specific signals (e.g. Amazon Buy Box data)
  },
  "competitors": [
    {
      "price":                    number,
      "is_fulfilled_by_platform": boolean,  // e.g. FBA for Amazon
      "condition":                string,   // "new" | "used" | "refurbished"
      "extra":                    object    // platform-specific fields
    }
  ],
  "market_summary": {
    "competitor_count":  number,
    "lowest_price":      number,
    "highest_price":     number,
    "median_price":      number,
    "buybox_price":      number | null  // Amazon only
  }
}

PRICING STRATEGIES
undercut  — Price slightly below the lowest credible competitor (0.5–3%) to maximise
            sales velocity. Use when competition is tight and the seller needs rank.
match     — Match the market median or Buy Box price. Use when the seller already has
            good rank and wants to hold position without racing to the bottom.
premium   — Price above median when the seller has strong reviews, better fulfilment,
            or a demonstrably superior offering. Justify with a clear reason.
hold      — Keep the current price unchanged. Use when the market is stable, the current
            price is already optimal, or all alternatives would breach the margin floor.

DECISION RULES (apply in strict order)
1. NEVER recommend a price below min_margin_floor. This is an absolute constraint.
2. NEVER recommend a price more than 10× the current price (likely a data error).
3. If fewer than 2 competitors exist, default to "hold" unless the current price is
   clearly above or below any single competitor by more than 15%.
4. If the seller is already the Buy Box winner (Amazon), use "hold" or "premium" —
   do not undercut yourself.
5. Prefer small, incremental adjustments (1–5%) over large swings (>10%).
6. When competitor data is sparse or unreliable, favour "hold" over action.

CONFIDENCE SCORING
Score your confidence in the recommendation 0–100:
  85–100  High — clear market signal, reliable data, strong reasoning
  60–84   Medium — reasonable signal but some uncertainty
  0–59    Low — sparse data, conflicting signals, or edge case

OUTPUT FORMAT
Respond with ONLY valid JSON. No preamble, no explanation outside the JSON, no markdown.

{
  "recommended_price": number,       // two decimal places, e.g. 24.99
  "strategy":          string,       // "undercut" | "match" | "premium" | "hold"
  "action":            string,       // "increase" | "decrease" | "hold"
  "confidence":        integer,      // 0–100
  "reasoning":         string,       // one plain-English sentence, max 200 chars,
                                     // suitable for display directly to a non-technical seller
  "competitor_low":    number,       // lowest competitor price used in the analysis
  "competitor_count":  integer       // number of competitors analysed
}

CRITICAL: If you cannot determine a safe, confident recommendation, set strategy="hold",
action="hold", recommended_price equal to the current price, and confidence below 60.
Never guess. Never hallucinate a price. Never return null or omit fields.
""".strip()

# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class RepricingError(Exception):
    """Base exception for all repricing engine errors."""


class GuardrailError(RepricingError):
    """
    Raised when a recommended price fails a safety guardrail check.

    This is a CRITICAL-level event.  The caller must abort the price update,
    set the job state to FAILED, and surface the failure to the operator.
    """


class BatchParseError(RepricingError):
    """
    Raised when Claude's response cannot be parsed into a RepricingRecommendation.

    This is a CRITICAL-level event — an unparseable response must never result
    in a price being applied.
    """


# ---------------------------------------------------------------------------
# State Machine
# ---------------------------------------------------------------------------


class RepricingJobState(str, Enum):
    """
    Valid states for a repricing job in the database.

    Transitions (ARCHITECTURE.md §3):
        IDLE → BATCH_SUBMITTED → PROCESSING → SYNCED
                     ↓                ↓
                  FAILED           FAILED
                     ↓
              (auto-retry) → IDLE

    Ownership:
        IDLE            — Scheduler resets here after SYNCED or timed-out FAILED.
        BATCH_SUBMITTED — Written exclusively by the Batch Submitter.
        PROCESSING      — Written exclusively by the Batch Poller.
        SYNCED          — Written exclusively by the Price Applicator on success.
        FAILED          — Written by Poller or Applicator on any error.
    """

    IDLE = "IDLE"
    BATCH_SUBMITTED = "BATCH_SUBMITTED"
    PROCESSING = "PROCESSING"
    SYNCED = "SYNCED"
    FAILED = "FAILED"

    @classmethod
    def valid_transition(cls, from_state: "RepricingJobState", to_state: "RepricingJobState") -> bool:
        """
        Return True if the state transition is permitted by the state machine.

        Args:
            from_state: Current job state.
            to_state:   Intended next state.

        Returns:
            True if the transition is valid; False otherwise.
        """
        allowed: dict["RepricingJobState", set["RepricingJobState"]] = {
            cls.IDLE:             {cls.BATCH_SUBMITTED},
            cls.BATCH_SUBMITTED:  {cls.PROCESSING, cls.FAILED},
            cls.PROCESSING:       {cls.SYNCED, cls.FAILED},
            cls.SYNCED:           {cls.IDLE},   # scheduler resets after next cycle gap
            cls.FAILED:           {cls.IDLE},   # manual retry or auto-recovery
        }
        return to_state in allowed.get(from_state, set())


# ---------------------------------------------------------------------------
# Pydantic v2 Data Models
# ---------------------------------------------------------------------------


class CompetitorProduct(BaseModel):
    """
    A single competitor listing on any supported platform.

    All platform connectors map their native data format to this model before
    passing it to the repricing engine.  The engine is platform-agnostic —
    it never imports anything from the platforms/ package.
    """

    price: float = Field(..., gt=0, description="Competitor's listed price (item price only, excluding shipping).")
    platform: SupportedPlatform = Field(..., description="Platform this listing is on.")
    is_fulfilled_by_platform: bool = Field(
        default=False,
        description=(
            "True if the competitor uses platform fulfilment (e.g. FBA for Amazon). "
            "Platform-fulfilled listings typically have delivery advantages and may "
            "warrant a higher recommended price."
        ),
    )
    condition: Literal["new", "used", "refurbished", "unknown"] = Field(
        default="new",
        description="Item condition. Only 'new' condition competitors are used for New listings.",
    )
    extra: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Platform-specific supplementary data passed verbatim to the AI "
            "(e.g. is_buy_box_winner, seller_rating, fba_competitor_count). "
            "Must contain only JSON-serialisable values."
        ),
    )

    @field_validator("price")
    @classmethod
    def price_must_be_finite(cls, v: float) -> float:
        """Reject infinite or NaN prices which would corrupt guardrail math."""
        import math
        if not math.isfinite(v):
            raise ValueError(f"Competitor price must be a finite number, got {v!r}")
        return round(v, 2)

    model_config = {"frozen": True}


class MyProduct(BaseModel):
    """
    The seller's own product on a given platform.

    Populated by the platform connector after pulling the product catalog.
    ``cost`` and ``min_margin_floor`` are set by the seller in their dashboard.
    """

    product_id: str = Field(..., description="PriceBot internal UUID for this product.")
    platform_product_id: str = Field(
        ...,
        description="Platform's native identifier (ASIN, Etsy listing ID, Shopify product ID, etc.).",
    )
    platform_sku: str | None = Field(
        default=None,
        description="Seller's own SKU where the platform supports it (required for Amazon price updates).",
    )
    title: str = Field(..., min_length=1, description="Product title as shown on the platform.")
    platform: SupportedPlatform = Field(..., description="Platform this product is listed on.")
    current_price: float = Field(..., gt=0, description="Seller's current listed price.")
    cost: float = Field(
        default=0.0,
        ge=0,
        description=(
            "Seller's cost of goods (COGS).  Used with min_margin_floor to compute "
            "the absolute price floor.  Defaults to 0 if the seller has not set it."
        ),
    )
    min_margin_floor: float = Field(
        default=0.0,
        ge=0,
        description=(
            "Absolute minimum safe price — typically cost × (1 + required_margin_pct). "
            "The guardrail enforces: final_price >= cost + min_margin_floor. "
            "Set by the seller per-product or as a global default in their dashboard."
        ),
    )
    user_id: str = Field(..., description="Supabase auth.users.id of the product owner.")
    platform_context: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Platform-specific signals for the AI (e.g. Amazon Buy Box winner status, "
            "BSR rank, Etsy listing age).  JSON-serialisable values only."
        ),
    )
    metadata: dict[str, Any] = Field(
        default_factory=dict,
        description="Additional connector metadata (condition, marketplace_id, etc.).",
    )

    @field_validator("current_price", "cost", "min_margin_floor")
    @classmethod
    def prices_must_be_finite(cls, v: float) -> float:
        """Reject infinite or NaN values which would break guardrail arithmetic."""
        import math
        if not math.isfinite(v):
            raise ValueError(f"Price/cost value must be finite, got {v!r}")
        return round(v, 2)

    @model_validator(mode="after")
    def floor_must_not_exceed_current_price_by_too_much(self) -> "MyProduct":
        """
        Warn (but do not reject) when the margin floor is already above the current price.
        This is a seller configuration problem, not an engine error.
        """
        absolute_floor = self.cost + self.min_margin_floor
        if absolute_floor > self.current_price * 2:
            logger.warning(
                "Margin floor exceeds 2× current price — seller config may be incorrect",
                extra={
                    "product_id": self.product_id,
                    "current_price": self.current_price,
                    "absolute_floor": absolute_floor,
                    "cost": self.cost,
                    "min_margin_floor": self.min_margin_floor,
                },
            )
        return self

    @property
    def absolute_floor_price(self) -> float:
        """Convenience accessor: the hard minimum price the engine will ever recommend."""
        return round(self.cost + self.min_margin_floor, 2)


class RepricingRecommendation(BaseModel):
    """
    The engine's output for a single product after parsing Claude's JSON response
    and applying all safety guardrails.

    ``final_price`` is the value that will be written to the platform — it is always
    >= ``product.absolute_floor_price`` regardless of what Claude recommended.
    """

    product_id: str = Field(..., description="PriceBot internal UUID — links back to MyProduct.")
    recommended_price: float = Field(
        ...,
        gt=0,
        description="Claude's raw recommended price before guardrail adjustment.",
    )
    final_price: float = Field(
        ...,
        gt=0,
        description=(
            "Price that will be applied to the platform after guardrail enforcement. "
            "Always >= cost + min_margin_floor."
        ),
    )
    guardrail_applied: bool = Field(
        default=False,
        description="True if the guardrail overrode Claude's recommendation.",
    )
    strategy: Literal["undercut", "match", "premium", "hold"] = Field(
        ..., description="Pricing strategy selected by the AI."
    )
    action: Literal["increase", "decrease", "hold"] = Field(
        ..., description="Direction of the price change."
    )
    confidence: int = Field(
        ..., ge=0, le=100, description="AI confidence score 0–100."
    )
    reasoning: str = Field(
        ...,
        max_length=300,
        description=(
            "Plain-English explanation for the seller. "
            "Displayed verbatim in the dashboard — must be jargon-free."
        ),
    )
    competitor_low: float | None = Field(
        default=None,
        description="Lowest competitor price used in this analysis.",
    )
    competitor_count: int = Field(
        default=0,
        ge=0,
        description="Number of competitor listings analysed.",
    )
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="UTC timestamp of recommendation generation.",
    )

    @field_validator("final_price", "recommended_price")
    @classmethod
    def must_be_positive_finite(cls, v: float) -> float:
        import math
        if not math.isfinite(v) or v <= 0:
            raise ValueError(f"Price must be a positive finite number, got {v!r}")
        return round(v, 2)

    model_config = {"frozen": True}


class BatchSubmitResult(BaseModel):
    """
    Return value of RepricingEngine.submit_batch().

    Contains the Anthropic batch ID needed by the poller to retrieve results,
    and summary statistics for logging and cost tracking.
    """

    batch_id: str = Field(..., description="Anthropic message batch ID.")
    request_count: int = Field(..., ge=1, description="Number of product requests in this batch.")
    user_id: str = Field(..., description="Owner of this batch — for tenant-scoped DB updates.")
    submitted_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="UTC timestamp of batch submission.",
    )
    estimated_input_tokens: int = Field(
        default=0,
        description="Rough token estimate for cost tracking (before cache discount).",
    )

    model_config = {"frozen": True}


# ---------------------------------------------------------------------------
# Market summary helper
# ---------------------------------------------------------------------------


def _build_market_summary(competitors: list[CompetitorProduct]) -> dict[str, Any]:
    """
    Derive summary statistics from the competitor list.

    Filters to ``condition='new'`` only (most platforms compare New to New).
    Falls back to all conditions if no New listings exist.

    Args:
        competitors: Raw list of CompetitorProduct instances.

    Returns:
        Dict with competitor_count, lowest_price, highest_price, median_price,
        and buybox_price (Amazon-specific, else None).
    """
    new_only = [c for c in competitors if c.condition == "new"]
    working_set = new_only if new_only else competitors

    if not working_set:
        return {
            "competitor_count": 0,
            "lowest_price": None,
            "highest_price": None,
            "median_price": None,
            "buybox_price": None,
        }

    prices = sorted(c.price for c in working_set)
    mid = len(prices) // 2
    median = (prices[mid - 1] + prices[mid]) / 2 if len(prices) % 2 == 0 else prices[mid]

    # Extract Buy Box price from Amazon extra data if present
    buybox_price: float | None = None
    for comp in working_set:
        if comp.extra.get("is_buy_box_winner"):
            buybox_price = comp.price
            break

    return {
        "competitor_count": len(working_set),
        "lowest_price": prices[0],
        "highest_price": prices[-1],
        "median_price": round(median, 2),
        "buybox_price": buybox_price,
    }


def _build_user_message(product: MyProduct, competitors: list[CompetitorProduct]) -> str:
    """
    Serialise product + competitor data into the JSON payload sent to Claude.

    Args:
        product:     The seller's product.
        competitors: Live competitor listings from the platform connector.

    Returns:
        JSON string suitable for use as the ``content`` of a user message.
    """
    summary = _build_market_summary(competitors)

    payload: dict[str, Any] = {
        "product": {
            "platform": product.platform,
            "title": product.title,
            "current_price": product.current_price,
            "cost": product.cost if product.cost > 0 else None,
            "min_margin_floor": product.absolute_floor_price,
            "platform_context": product.platform_context,
        },
        "competitors": [
            {
                "price": c.price,
                "is_fulfilled_by_platform": c.is_fulfilled_by_platform,
                "condition": c.condition,
                "extra": c.extra,
            }
            for c in competitors
        ],
        "market_summary": summary,
    }
    return json.dumps(payload, separators=(",", ":"))


# ---------------------------------------------------------------------------
# Guardrail
# ---------------------------------------------------------------------------


def _apply_guardrail(
    product: MyProduct,
    claude_price: float,
) -> tuple[float, bool]:
    """
    Apply all four safety guardrails and return the final safe price.

    Guardrail hierarchy (SECURITY.md §6):
        1. Claude price must be a positive finite number.
        2. Claude price must be >= cost + min_margin_floor.
        3. Final price must not exceed current_price × PRICE_CEILING_MULTIPLIER.

    Args:
        product:     The seller's product (provides floor and ceiling anchors).
        claude_price: Raw price from Claude's JSON response.

    Returns:
        Tuple of (final_price, guardrail_applied).
        ``guardrail_applied`` is True if the floor overrode Claude's value.

    Raises:
        GuardrailError: If the ceiling check fails (CRITICAL — abort the update).
    """
    import math

    # Guard 1: must be a positive, finite number (already validated by Pydantic,
    # but re-checked here as defence-in-depth against any future bypass)
    if not math.isfinite(claude_price) or claude_price <= 0:
        raise GuardrailError(
            f"Claude returned non-positive or non-finite price: {claude_price!r}"
        )

    # Guard 2: enforce margin floor
    floor = product.absolute_floor_price
    if claude_price < floor:
        logger.warning(
            "Guardrail: margin floor overrides Claude recommendation",
            extra={
                "product_id": product.product_id,
                "user_id": product.user_id,
                "claude_price": claude_price,
                "floor_price": floor,
                "cost": product.cost,
                "min_margin_floor": product.min_margin_floor,
            },
        )
        final_price = floor
        guardrail_applied = True
    else:
        final_price = claude_price
        guardrail_applied = False

    # Guard 3: price ceiling — a 10× spike almost certainly means bad data
    ceiling = round(product.current_price * PRICE_CEILING_MULTIPLIER, 2)
    if final_price > ceiling:
        logger.critical(
            "Guardrail: recommended price exceeds ceiling — aborting update",
            extra={
                "product_id": product.product_id,
                "user_id": product.user_id,
                "final_price": final_price,
                "ceiling": ceiling,
                "current_price": product.current_price,
            },
        )
        raise GuardrailError(
            f"Price {final_price} exceeds ceiling {ceiling} "
            f"(10× current price {product.current_price})"
        )

    # Round to 2 d.p. using ROUND_HALF_UP (standard retail rounding)
    final_price = float(
        Decimal(str(final_price)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    )
    return final_price, guardrail_applied


# ---------------------------------------------------------------------------
# Claude response parser
# ---------------------------------------------------------------------------


def _parse_claude_response(
    raw_text: str,
    product: MyProduct,
) -> RepricingRecommendation:
    """
    Parse Claude's raw text response into a validated RepricingRecommendation.

    Applies guardrails immediately after parsing so the returned model always
    contains a safe ``final_price``.

    Args:
        raw_text: The ``text`` field from the Claude message content block.
        product:  The seller's product — needed for guardrail checks.

    Returns:
        Validated RepricingRecommendation with guardrails applied.

    Raises:
        BatchParseError: If JSON is malformed or required fields are missing.
        GuardrailError:  If the parsed price violates a hard safety constraint.
    """
    # Strip any accidental markdown fences (defensive — prompt says JSON only)
    cleaned = raw_text.strip()
    if cleaned.startswith("```"):
        lines = cleaned.splitlines()
        cleaned = "\n".join(
            line for line in lines if not line.startswith("```")
        ).strip()

    try:
        data: dict[str, Any] = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        logger.critical(
            "Guardrail: Claude response is not valid JSON — aborting",
            extra={
                "product_id": product.product_id,
                "user_id": product.user_id,
                "raw_length": len(raw_text),
                "parse_error": str(exc),
            },
        )
        raise BatchParseError(
            f"Claude response for product {product.product_id} is not valid JSON: {exc}"
        ) from exc

    # Validate required fields exist before touching them
    required_fields = {
        "recommended_price", "strategy", "action", "confidence", "reasoning"
    }
    missing = required_fields - data.keys()
    if missing:
        logger.critical(
            "Guardrail: Claude response missing required fields — aborting",
            extra={
                "product_id": product.product_id,
                "user_id": product.user_id,
                "missing_fields": sorted(missing),
            },
        )
        raise BatchParseError(
            f"Claude response missing required fields {missing} "
            f"for product {product.product_id}"
        )

    raw_price = data.get("recommended_price")
    if not isinstance(raw_price, (int, float)) or raw_price <= 0:
        logger.critical(
            "Guardrail: Claude recommended_price is not a positive number — aborting",
            extra={
                "product_id": product.product_id,
                "user_id": product.user_id,
                "recommended_price": raw_price,
            },
        )
        raise BatchParseError(
            f"Claude recommended_price is invalid ({raw_price!r}) "
            f"for product {product.product_id}"
        )

    # Apply guardrail — may raise GuardrailError (CRITICAL, caller must handle)
    final_price, guardrail_applied = _apply_guardrail(product, float(raw_price))

    return RepricingRecommendation(
        product_id=product.product_id,
        recommended_price=float(raw_price),
        final_price=final_price,
        guardrail_applied=guardrail_applied,
        strategy=data["strategy"],
        action=data["action"],
        confidence=int(data["confidence"]),
        reasoning=str(data["reasoning"])[:300],   # hard cap — never overflow UI field
        competitor_low=data.get("competitor_low"),
        competitor_count=int(data.get("competitor_count", 0)),
    )


# ---------------------------------------------------------------------------
# Main Engine
# ---------------------------------------------------------------------------


class RepricingEngine:
    """
    Platform-agnostic AI repricing engine.

    Wraps the Anthropic Batch API to submit repricing jobs and retrieve results
    asynchronously (up to 15-minute delay).  All requests use:
      - Model:        claude-haiku-4-5 (locked — see COSTS.md §1)
      - Delivery:     Batch API (50% cost saving — see COSTS.md §2.1)
      - Prompt cache: system prompt tagged ephemeral (~90% input savings — COSTS.md §2.2)

    Typical usage pattern (workers/batch_submitter.py → workers/batch_poller.py)::

        engine = RepricingEngine(api_key=settings.ANTHROPIC_API_KEY)

        # Submitter: build batch from platform connector output
        result = engine.submit_batch(
            user_id=user_id,
            products_with_competitors=[
                (product, competitors),
                ...
            ],
        )
        # Persist result.batch_id to DB → state = BATCH_SUBMITTED

        # Poller (15 min later): retrieve and parse results
        if engine.is_batch_complete(result.batch_id):
            recommendations = engine.retrieve_batch_results(
                batch_id=result.batch_id,
                products_by_id={p.product_id: p for p, _ in products_with_competitors},
            )
            for rec in recommendations:
                # Apply rec.final_price to platform via connector
                ...
    """

    def __init__(self, api_key: str) -> None:
        """
        Initialise the engine with an Anthropic API key.

        Args:
            api_key: Anthropic API key from ANTHROPIC_API_KEY env var.
                     Never log or expose this value.
        """
        if not api_key:
            raise ValueError("ANTHROPIC_API_KEY must not be empty")
        self._client = anthropic.Anthropic(api_key=api_key)
        logger.info("RepricingEngine initialised", extra={"model": REPRICING_MODEL})

    # ------------------------------------------------------------------
    # Cached system prompt block (built once, reused on every request)
    # ------------------------------------------------------------------

    @staticmethod
    def _system_prompt_block() -> dict[str, Any]:
        """
        Return the system prompt as a content block with cache_control set.

        The ``cache_control: ephemeral`` tag is what activates Anthropic's
        prompt caching.  It must be present on every request — removing it
        increases input token costs by ~10×.

        Returns:
            Dict matching Anthropic's content block schema with cache_control.
        """
        return {
            "type": "text",
            "text": REPRICING_SYSTEM_PROMPT,
            "cache_control": {"type": "ephemeral"},   # MANDATORY — do not remove
        }

    # ------------------------------------------------------------------
    # Batch submission
    # ------------------------------------------------------------------

    def submit_batch(
        self,
        user_id: str,
        products_with_competitors: list[tuple[MyProduct, list[CompetitorProduct]]],
    ) -> BatchSubmitResult:
        """
        Submit a batch of repricing jobs to the Anthropic Batch API.

        Each (product, competitors) pair becomes one request in the batch.
        All products for a given user are submitted in a single batch to
        minimise API overhead and maximise prompt cache hit rate.

        The ``custom_id`` for each request is ``{user_id}:{product_id}``,
        which allows the poller to match results back to products without
        any additional DB lookups.

        Args:
            user_id:                    Supabase user ID — used as batch owner.
            products_with_competitors:  List of (MyProduct, [CompetitorProduct]) tuples.
                                        Must not be empty.  All products must belong
                                        to user_id (caller's responsibility).

        Returns:
            BatchSubmitResult containing the Anthropic batch_id and summary stats.

        Raises:
            ValueError:          If products_with_competitors is empty.
            anthropic.APIError:  If the Anthropic API request fails (caller should
                                 set job state to FAILED and log ERROR).
        """
        if not products_with_competitors:
            raise ValueError("products_with_competitors must not be empty")

        system_block = self._system_prompt_block()
        requests: list[BatchRequest] = []
        estimated_tokens = 0

        for product, competitors in products_with_competitors:
            user_message = _build_user_message(product, competitors)
            custom_id = f"{user_id}:{product.product_id}"

            requests.append(
                BatchRequest(
                    custom_id=custom_id,
                    params=MessageCreateParamsNonStreaming(
                        model=REPRICING_MODEL,
                        max_tokens=MAX_OUTPUT_TOKENS,
                        system=[system_block],          # type: ignore[arg-type]
                        messages=[
                            {"role": "user", "content": user_message}
                        ],
                    ),
                )
            )
            # Rough token estimate: system_prompt (~800) + user_message (~500)
            estimated_tokens += 1300

        logger.info(
            "Submitting repricing batch to Anthropic",
            extra={
                "user_id": user_id,
                "request_count": len(requests),
                "estimated_input_tokens": estimated_tokens,
                "model": REPRICING_MODEL,
            },
        )

        start_ts = time.monotonic()
        batch = self._client.messages.batches.create(requests=requests)
        elapsed_ms = round((time.monotonic() - start_ts) * 1000)

        logger.info(
            "Anthropic batch submitted successfully",
            extra={
                "user_id": user_id,
                "batch_id": batch.id,
                "request_count": len(requests),
                "elapsed_ms": elapsed_ms,
                "processing_status": batch.processing_status,
            },
        )

        return BatchSubmitResult(
            batch_id=batch.id,
            request_count=len(requests),
            user_id=user_id,
            estimated_input_tokens=estimated_tokens,
        )

    # ------------------------------------------------------------------
    # Batch status check
    # ------------------------------------------------------------------

    def is_batch_complete(self, batch_id: str) -> bool:
        """
        Check whether an Anthropic message batch has finished processing.

        The poller calls this every 5 minutes.  Returns False while the batch
        is in progress so the poller simply skips and retries next cycle.

        Args:
            batch_id: Anthropic batch ID from a prior submit_batch() call.

        Returns:
            True if processing_status == "ended"; False if still in progress.

        Raises:
            anthropic.APIError: On unexpected API failure (caller logs ERROR).
        """
        batch = self._client.messages.batches.retrieve(batch_id)

        logger.info(
            "Batch status check",
            extra={
                "batch_id": batch_id,
                "processing_status": batch.processing_status,
                "request_counts": {
                    "processing": batch.request_counts.processing,
                    "succeeded": batch.request_counts.succeeded,
                    "errored": batch.request_counts.errored,
                    "expired": batch.request_counts.expired,
                    "canceled": batch.request_counts.canceled,
                },
            },
        )

        return batch.processing_status == "ended"

    # ------------------------------------------------------------------
    # Batch result retrieval
    # ------------------------------------------------------------------

    def retrieve_batch_results(
        self,
        batch_id: str,
        products_by_id: dict[str, MyProduct],
    ) -> list[RepricingRecommendation]:
        """
        Stream completed batch results and return parsed RepricingRecommendations.

        Only call this after ``is_batch_complete()`` returns True.

        For each result:
          - Successful responses are parsed and guardrails are applied.
          - Errored responses log CRITICAL and are excluded from the returned list.
          - Parse failures (BatchParseError) log CRITICAL and are excluded.
          - Guardrail ceiling violations (GuardrailError) log CRITICAL and are excluded.

        The caller is responsible for:
          - Setting ``state = SYNCED`` for products present in the returned list.
          - Setting ``state = FAILED`` for products NOT in the returned list
            (meaning they errored, timed out, or failed a guardrail).

        Args:
            batch_id:       Anthropic batch ID to retrieve results for.
            products_by_id: Dict mapping product_id → MyProduct.
                            Used to look up product data for guardrail checks and
                            to map custom_id back to the original product.

        Returns:
            List of RepricingRecommendation — one per successfully parsed result.
            Products that errored or failed guardrails are omitted (caller detects
            the gap and sets those products to FAILED).
        """
        recommendations: list[RepricingRecommendation] = []
        succeeded = 0
        failed = 0

        logger.info(
            "Retrieving batch results",
            extra={"batch_id": batch_id, "expected_products": len(products_by_id)},
        )

        for result in self._client.messages.batches.results(batch_id):
            # custom_id format: "{user_id}:{product_id}"
            parts = result.custom_id.split(":", 1)
            if len(parts) != 2:
                logger.critical(
                    "Batch result has malformed custom_id — skipping",
                    extra={"batch_id": batch_id, "custom_id": result.custom_id},
                )
                failed += 1
                continue

            _user_id, product_id = parts
            product = products_by_id.get(product_id)

            if product is None:
                logger.critical(
                    "Batch result references unknown product_id — skipping",
                    extra={
                        "batch_id": batch_id,
                        "custom_id": result.custom_id,
                        "product_id": product_id,
                    },
                )
                failed += 1
                continue

            # Anthropic result types: "succeeded", "errored", "expired", "canceled"
            if result.result.type != "succeeded":
                logger.critical(
                    "Batch request did not succeed — aborting price update",
                    extra={
                        "batch_id": batch_id,
                        "product_id": product_id,
                        "user_id": product.user_id,
                        "result_type": result.result.type,
                    },
                )
                failed += 1
                continue

            # Extract raw text from the message content blocks
            message = result.result.message
            raw_text: str | None = None
            for block in message.content:
                if block.type == "text":
                    raw_text = block.text
                    break

            if not raw_text:
                logger.critical(
                    "Guardrail: Claude returned empty content — aborting",
                    extra={
                        "batch_id": batch_id,
                        "product_id": product_id,
                        "user_id": product.user_id,
                        "stop_reason": message.stop_reason,
                    },
                )
                failed += 1
                continue

            # Parse and apply guardrails
            try:
                recommendation = _parse_claude_response(raw_text, product)
            except BatchParseError as exc:
                # Already logged CRITICAL inside _parse_claude_response
                logger.critical(
                    "BatchParseError — product excluded from results",
                    extra={
                        "batch_id": batch_id,
                        "product_id": product_id,
                        "user_id": product.user_id,
                        "error": str(exc),
                    },
                )
                failed += 1
                continue
            except GuardrailError as exc:
                # Already logged CRITICAL inside _apply_guardrail
                logger.critical(
                    "GuardrailError — product excluded from results",
                    extra={
                        "batch_id": batch_id,
                        "product_id": product_id,
                        "user_id": product.user_id,
                        "error": str(exc),
                    },
                )
                failed += 1
                continue

            recommendations.append(recommendation)
            succeeded += 1

            logger.info(
                "Repricing recommendation parsed",
                extra={
                    "batch_id": batch_id,
                    "product_id": product_id,
                    "strategy": recommendation.strategy,
                    "action": recommendation.action,
                    "current_price": product.current_price,
                    "recommended_price": recommendation.recommended_price,
                    "final_price": recommendation.final_price,
                    "guardrail_applied": recommendation.guardrail_applied,
                    "confidence": recommendation.confidence,
                },
            )

        logger.info(
            "Batch result retrieval complete",
            extra={
                "batch_id": batch_id,
                "succeeded": succeeded,
                "failed": failed,
                "success_rate_pct": round(succeeded / max(succeeded + failed, 1) * 100, 1),
            },
        )

        return recommendations

    # ------------------------------------------------------------------
    # Synchronous single-product call (dev/onboarding preview ONLY)
    # ------------------------------------------------------------------

    def preview_recommendation(
        self,
        product: MyProduct,
        competitors: list[CompetitorProduct],
    ) -> RepricingRecommendation:
        """
        Get a single repricing recommendation synchronously.

        **THIS METHOD IS NOT FOR PRODUCTION REPRICING.**

        Use only for:
          - Developer testing during connector development.
          - The "preview" feature on the dashboard onboarding wizard
            where a <2 second response is required (not batch-compatible).

        Using this for bulk repricing bypasses the Batch API and doubles Claude
        costs.  Any production repricing must use submit_batch() / retrieve_batch_results().

        Args:
            product:     The seller's product.
            competitors: Live competitor listings.

        Returns:
            RepricingRecommendation with guardrails applied.

        Raises:
            BatchParseError: On unparseable Claude response.
            GuardrailError:  On safety constraint violation.
            anthropic.APIError: On Anthropic API failure.
        """
        logger.warning(
            "preview_recommendation() called — synchronous API, not for bulk repricing",
            extra={"product_id": product.product_id, "user_id": product.user_id},
        )

        user_message = _build_user_message(product, competitors)
        system_block = self._system_prompt_block()

        response = self._client.messages.create(
            model=REPRICING_MODEL,
            max_tokens=MAX_OUTPUT_TOKENS,
            system=[system_block],          # type: ignore[arg-type]
            messages=[{"role": "user", "content": user_message}],
        )

        raw_text: str | None = None
        for block in response.content:
            if block.type == "text":
                raw_text = block.text
                break

        if not raw_text:
            raise BatchParseError(
                f"Claude returned empty content for product {product.product_id}"
            )

        return _parse_claude_response(raw_text, product)