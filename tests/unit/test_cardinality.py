"""The cardinality guard.

The failure it prevents is specific: a metric labelled with `tenant_id` looks
harmless in a code review and takes out the metrics backend for everybody on a
busy day. Every test here is about refusing that metric before it exists.
"""

from __future__ import annotations

import pytest

from llmops.cardinality.budget import (
    DEFAULT_LABELS,
    KNOWN_UNBOUNDED,
    LabelRegistry,
    bounded,
    unbounded,
)
from llmops.errors import CardinalityError

pytestmark = pytest.mark.unit


@pytest.fixture
def registry() -> LabelRegistry:
    return LabelRegistry(DEFAULT_LABELS)


class TestWhatIsAccepted:
    def test_bounded_labels_are_accepted_with_their_series_count(self, registry: LabelRegistry):
        spec = registry.check("llm_requests", ["provider", "operation"])
        # 20 providers times 4 operations.
        assert spec.series == 80

    def test_a_metric_with_no_labels_is_one_series(self, registry: LabelRegistry):
        assert registry.check("llm_requests_total", []).series == 1

    def test_a_newly_registered_label_becomes_usable(self, registry: LabelRegistry):
        registry.register(bounded("tenant_tier", 4))
        assert registry.check("llm_requests", ["tenant_tier"]).series == 4


class TestWhatIsRefused:
    @pytest.mark.parametrize("label", sorted(KNOWN_UNBOUNDED))
    def test_every_known_unbounded_label_is_refused(self, registry: LabelRegistry, label: str):
        with pytest.raises(CardinalityError, match="unbounded label"):
            registry.check("llm_requests", [label])

    def test_an_unknown_label_is_refused_because_the_default_fails_closed(
        self, registry: LabelRegistry
    ):
        # The cost of wrongly refusing a bounded label is one line registering
        # it. The cost of wrongly accepting an unbounded one is an outage.
        with pytest.raises(CardinalityError, match="unbounded label"):
            registry.check("llm_requests", ["something_nobody_declared"])

    def test_the_remedy_names_the_bucketed_alternative(self, registry: LabelRegistry):
        # A rule that only says no gets disabled.
        with pytest.raises(CardinalityError) as caught:
            registry.check("llm_requests", ["tenant_id"])
        assert "tenant_tier" in caught.value.remedy

    def test_content_is_named_as_content_rather_than_offered_a_bucket(
        self, registry: LabelRegistry
    ):
        # There is no bucketed form of a completion. Suggesting one would be
        # worse than saying nothing.
        with pytest.raises(CardinalityError) as caught:
            registry.check("llm_requests", ["completion"])
        assert "cannot be a label" in caught.value.remedy

    def test_an_unknown_label_is_told_how_to_declare_itself(self, registry: LabelRegistry):
        with pytest.raises(CardinalityError) as caught:
            registry.check("llm_requests", ["shard"])
        assert "bounded(name, cardinality)" in caught.value.remedy

    def test_a_repeated_label_is_refused(self, registry: LabelRegistry):
        with pytest.raises(CardinalityError, match="repeats label"):
            registry.check("llm_requests", ["provider", "provider"])

    def test_too_many_series_from_bounded_labels_is_still_refused(self, registry: LabelRegistry):
        # Every label bounded, and the product is not. This is the failure that
        # slips past a reviewer checking labels one at a time.
        with pytest.raises(CardinalityError, match="can produce"):
            registry.check("llm_requests", ["provider", "model", "route", "service"])

    def test_the_remedy_names_the_largest_dimension(self, registry: LabelRegistry):
        with pytest.raises(CardinalityError) as caught:
            registry.check("llm_requests", ["provider", "model", "route", "service"])
        assert "'model'" in caught.value.remedy or "'route'" in caught.value.remedy

    def test_the_budget_can_be_raised_deliberately(self, registry: LabelRegistry):
        spec = registry.check(
            "llm_requests", ["provider", "model", "route"], series_budget=10_000_000
        )
        assert spec.series == 20 * 200 * 200


class TestTheRegistry:
    def test_a_label_cannot_be_registered_twice_with_different_answers(self):
        # It is how two parts of a codebase come to disagree about whether a
        # label is safe, and the optimistic one always wins by accident.
        registry = LabelRegistry([])
        registry.register(bounded("tier", 4))
        with pytest.raises(CardinalityError, match="already registered"):
            registry.register(bounded("tier", 400))

    def test_registering_the_same_answer_twice_is_fine(self):
        registry = LabelRegistry([])
        registry.register(bounded("tier", 4))
        registry.register(bounded("tier", 4))
        assert len(registry) == 1

    def test_an_unbounded_label_can_be_registered_explicitly(self):
        registry = LabelRegistry([])
        registry.register(unbounded("prompt_hash"))
        assert registry.describe("prompt_hash").bounded is False

    def test_a_zero_cardinality_is_refused(self):
        # `pytest.raises(match=)` reads the message, not the remedy, so the
        # assertion has to name the half it is actually checking.
        with pytest.raises(CardinalityError, match="cardinality 0") as caught:
            bounded("tier", 0)
        assert "at least one value" in caught.value.remedy

    def test_an_unregistered_label_describes_as_unbounded(self):
        assert LabelRegistry([]).describe("anything").bounded is False

    def test_the_default_labels_are_the_ones_this_tool_emits(self):
        names = {label.name for label in DEFAULT_LABELS}
        # Every span field that could reasonably become a dimension.
        assert {"provider", "model", "operation", "outcome"} <= names
