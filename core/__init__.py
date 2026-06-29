"""
PriceBot core package.

Exports the primary public interface of the repricing engine so callers
can import directly from ``core`` without knowing internal module layout.

Example::

    from core import (
        RepricingEngine,
        MyProduct,
        CompetitorProduct,
        RepricingRecommendation,
        RepricingJobState,
        BatchSubmitResult,
        RepricingError,
        GuardrailError,
        BatchParseError,
    )
"""

from core.repricing_engine import (
    BatchParseError,
    BatchSubmitResult,
    CompetitorProduct,
    GuardrailError,
    MyProduct,
    RepricingEngine,
    RepricingError,
    RepricingJobState,
    RepricingRecommendation,
)

__all__ = [
    "RepricingEngine",
    "MyProduct",
    "CompetitorProduct",
    "RepricingRecommendation",
    "RepricingJobState",
    "BatchSubmitResult",
    "RepricingError",
    "GuardrailError",
    "BatchParseError",
]