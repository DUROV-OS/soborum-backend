import pytest
from app.agents.autonomy.priority import (
    PriorityState,
    next_priority,
    scan_order,
    MIN_WEIGHT,
    MAX_WEIGHT,
    QUIET_DECAY_AFTER,
)


def test_first_signal_does_not_change_weight():
    """Первый сигнал не меняет вес."""
    state = PriorityState(weight=5, signal_streak=0, quiet_streak=0)
    new_state = next_priority(state, has_signal=True)
    assert new_state.weight == 5
    assert new_state.signal_streak == 1
    assert new_state.quiet_streak == 0


def test_second_consecutive_signal_increases_weight():
    """Второй подряд сигнал повышает вес."""
    state = PriorityState(weight=5, signal_streak=1, quiet_streak=0)
    new_state = next_priority(state, has_signal=True)
    assert new_state.weight == 6
    assert new_state.signal_streak == 2
    assert new_state.quiet_streak == 0


def test_weight_capped_at_max():
    """Вес не превышает MAX_WEIGHT."""
    state = PriorityState(weight=MAX_WEIGHT, signal_streak=5, quiet_streak=0)
    new_state = next_priority(state, has_signal=True)
    assert new_state.weight == MAX_WEIGHT
    assert new_state.signal_streak == 6


def test_quiet_1_2_does_not_change_weight():
    """Тишина 1-2 раза не меняет вес."""
    state_1 = PriorityState(weight=5, signal_streak=0, quiet_streak=0)
    new_state_1 = next_priority(state_1, has_signal=False)
    assert new_state_1.weight == 5
    assert new_state_1.quiet_streak == 1

    state_2 = PriorityState(weight=5, signal_streak=0, quiet_streak=1)
    new_state_2 = next_priority(state_2, has_signal=False)
    assert new_state_2.weight == 5
    assert new_state_2.quiet_streak == 2


def test_quiet_3_decreases_weight():
    """На 3-й тишине вес снижается."""
    state = PriorityState(weight=5, signal_streak=0, quiet_streak=2)
    new_state = next_priority(state, has_signal=False)
    assert new_state.weight == 4
    assert new_state.quiet_streak == 3


def test_weight_floored_at_min():
    """Вес не ниже MIN_WEIGHT."""
    state = PriorityState(weight=MIN_WEIGHT, signal_streak=0, quiet_streak=5)
    new_state = next_priority(state, has_signal=False)
    assert new_state.weight == MIN_WEIGHT
    assert new_state.quiet_streak == 6


def test_signal_after_quiet_resets_quiet_streak():
    """Сигнал после тишины обнуляет quiet_streak."""
    state = PriorityState(weight=5, signal_streak=0, quiet_streak=3)
    new_state = next_priority(state, has_signal=True)
    assert new_state.quiet_streak == 0
    assert new_state.signal_streak == 1
    assert new_state.weight == 5


def test_scan_order_sorting_and_tie_break():
    """scan_order сортирует по убыванию веса, при равенстве по алфавиту."""
    weights = {"b": 5, "a": 5, "c": 10, "d": 2}
    order = scan_order(weights)
    assert order == ["c", "a", "b", "d"]
