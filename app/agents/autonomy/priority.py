from dataclasses import dataclass

MIN_WEIGHT = 1
MAX_WEIGHT = 10
QUIET_DECAY_AFTER = 3


@dataclass(frozen=True)
class PriorityState:
    """Состояние приоритета раздела."""
    weight: int
    signal_streak: int
    quiet_streak: int


def next_priority(state: PriorityState, has_signal: bool) -> PriorityState:
    """
    Вычисляет новое состояние приоритета.
    - При наличии сигнала: увеличивается серия сигналов, обнуляется тишина.
      Вес увеличивается, если серия сигналов >= 2.
    - При отсутствии сигнала: увеличивается серия тишины, обнуляется сигналы.
      Вес уменьшается, если серия тишины >= QUIET_DECAY_AFTER.
    """
    if has_signal:
        new_signal_streak = state.signal_streak + 1
        new_quiet_streak = 0
        if new_signal_streak >= 2:
            new_weight = min(state.weight + 1, MAX_WEIGHT)
        else:
            new_weight = state.weight
    else:
        new_quiet_streak = state.quiet_streak + 1
        new_signal_streak = 0
        if new_quiet_streak >= QUIET_DECAY_AFTER:
            new_weight = max(state.weight - 1, MIN_WEIGHT)
        else:
            new_weight = state.weight

    return PriorityState(
        weight=new_weight,
        signal_streak=new_signal_streak,
        quiet_streak=new_quiet_streak
    )


def scan_order(weights: dict[str, int]) -> list[str]:
    """
    Возвращает список разделов, отсортированный по убыванию веса.
    При равных весах сортировка по алфавиту.
    """
    return sorted(weights.keys(), key=lambda k: (-weights[k], k))
