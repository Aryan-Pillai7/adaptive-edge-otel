"""Flood logic: the shape of the simulated incident.

These are the tests that protect the experiment. If the flood stops producing
byte-identical logs, logdedup has nothing to collapse; if it stops producing unique
label values, there is no cardinality to strip -- and in both cases Phase 4 would
report a spectacular reduction of nothing at all.
"""

from __future__ import annotations

import pytest

from edgeapp.flood import (
    DUPLICATE_LOG_ATTRIBUTES,
    DUPLICATE_LOG_MESSAGE,
    FloodInProgressError,
    FloodProfile,
    FloodRegistry,
    tick_plan,
    unique_label_values,
)


class TestFloodProfile:
    def test_totals_are_duration_times_rate(self):
        profile = FloodProfile(
            duration_seconds=30, logs_per_second=500, unique_labels_per_second=200
        )
        assert profile.total_log_records == 15_000
        assert profile.total_unique_labels == 6_000

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"duration_seconds": 0, "logs_per_second": 1, "unique_labels_per_second": 1},
            {"duration_seconds": -1, "logs_per_second": 1, "unique_labels_per_second": 1},
            {"duration_seconds": 1, "logs_per_second": -1, "unique_labels_per_second": 1},
            {"duration_seconds": 1, "logs_per_second": 1, "unique_labels_per_second": -1},
        ],
    )
    def test_rejects_nonsense_profiles(self, kwargs):
        with pytest.raises(ValueError):
            FloodProfile(**kwargs)

    def test_zero_rate_is_allowed(self):
        """Lets a run isolate one failure mode: log spam without cardinality, or vice versa."""
        profile = FloodProfile(duration_seconds=5, logs_per_second=0, unique_labels_per_second=10)
        assert profile.total_log_records == 0
        assert profile.total_unique_labels == 50

    def test_describe_reports_the_message_that_will_be_repeated(self):
        profile = FloodProfile(duration_seconds=1, logs_per_second=1, unique_labels_per_second=1)
        assert profile.describe()["duplicate_log_message"] == DUPLICATE_LOG_MESSAGE


class TestDuplicateLogShape:
    def test_message_contains_no_variable_content(self):
        """logdedup collapses only byte-identical records.

        A timestamp, counter or id interpolated into the body would make every record
        unique and silently defeat the Phase 4 dedup processor -- the pipeline would
        still "work", it would just stop reducing anything.
        """
        for marker in ("%s", "{", "}", "%d"):
            assert marker not in DUPLICATE_LOG_MESSAGE

    def test_attributes_are_bounded(self):
        """The attributes riding along with the duplicate log must be fixed values."""
        assert DUPLICATE_LOG_ATTRIBUTES
        for value in DUPLICATE_LOG_ATTRIBUTES.values():
            assert isinstance(value, str)
        # None of the unbounded keys belong on the duplicated record.
        assert not {"user_id", "request_id", "session_id"} & set(DUPLICATE_LOG_ATTRIBUTES)


class TestUniqueLabelValues:
    def test_every_value_is_distinct(self):
        """One distinct attribute set == one new metric time series."""
        batches = list(unique_label_values(200))
        assert len(batches) == 200
        assert len({b["user_id"] for b in batches}) == 200
        assert len({b["request_id"] for b in batches}) == 200

    def test_carries_every_attribute_phase_4_strips(self):
        """Guards against drift between the generator and the processor config."""
        from edgeapp.telemetry.instruments import HIGH_CARDINALITY_ATTRIBUTES

        attrs = next(unique_label_values(1))
        assert set(HIGH_CARDINALITY_ATTRIBUTES) <= set(attrs)

    def test_zero_yields_nothing(self):
        assert list(unique_label_values(0)) == []


class TestTickPlan:
    def test_one_entry_per_second(self):
        profile = FloodProfile(
            duration_seconds=30, logs_per_second=500, unique_labels_per_second=200
        )
        plan = tick_plan(profile)
        assert len(plan) == 30
        assert all(tick == (500, 200) for tick in plan)

    def test_plan_totals_match_the_profile(self):
        """The paced plan must not quietly emit more or less than the profile promises."""
        profile = FloodProfile(duration_seconds=7, logs_per_second=13, unique_labels_per_second=5)
        plan = tick_plan(profile)
        assert sum(logs for logs, _ in plan) == profile.total_log_records
        assert sum(labels for _, labels in plan) == profile.total_unique_labels


class TestFloodRegistry:
    def test_rejects_a_second_concurrent_flood(self):
        """Overlapping floods would make measured volume unattributable to a profile."""
        registry = FloodRegistry()
        profile = FloodProfile(duration_seconds=5, logs_per_second=1, unique_labels_per_second=1)
        registry.start(profile)
        with pytest.raises(FloodInProgressError):
            registry.start(profile)

    def test_a_new_flood_is_allowed_once_the_previous_finished(self):
        registry = FloodRegistry()
        profile = FloodProfile(duration_seconds=5, logs_per_second=1, unique_labels_per_second=1)
        first = registry.start(profile)
        registry.finish(first)
        assert registry.active is None
        second = registry.start(profile)
        assert second.flood_id != first.flood_id

    def test_cancel_marks_state_and_frees_the_slot(self):
        registry = FloodRegistry()
        profile = FloodProfile(duration_seconds=60, logs_per_second=1, unique_labels_per_second=1)
        registry.start(profile)
        cancelled = registry.cancel()
        assert cancelled is not None
        assert cancelled.cancelled is True
        assert registry.active is None
        assert registry.start(profile) is not None

    def test_cancel_with_nothing_running_is_a_noop(self):
        assert FloodRegistry().cancel() is None

    def test_last_returns_most_recent_even_after_finishing(self):
        registry = FloodRegistry()
        profile = FloodProfile(duration_seconds=1, logs_per_second=1, unique_labels_per_second=1)
        state = registry.start(profile)
        registry.finish(state)
        assert registry.last() is state
        assert registry.last().snapshot()["running"] is False
