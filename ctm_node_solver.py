from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


EPS = 1e-12
MovementKey = tuple[str, str]


@dataclass(frozen=True)
class NodeMovement:
    """Один разрешённый манёвр через CTM-узел без внутреннего накопления.

    `turn_ratio` — это beta_{ij}: доля demand входящего link i, которая хочет
    выйти через исходящий link j. Это не merge-priority. Приоритет слияния —
    отдельный параметр, который используется только в merge-узлах.
    """

    in_link_id: str
    out_link_id: str
    turn_ratio: float
    priority: float = 1.0
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class MovementDiagnostics:
    """Диагностика одного манёвра за один шаг решения узла."""

    desired_flow: float
    actual_flow: float
    restriction_factor: float
    active_constraints: list[str] = field(default_factory=list)


@dataclass
class NodeSolveResult:
    """Результат решения одного узла за один временной шаг."""

    case: str
    flows: dict[MovementKey, float]
    diagnostics: dict[MovementKey, MovementDiagnostics]
    node_inflow: float
    node_outflow: float
    conservation_error: float
    active_constraints: list[str] = field(default_factory=list)


def movement_key(movement: NodeMovement) -> MovementKey:
    return movement.in_link_id, movement.out_link_id


def normalize_turn_ratios(movements: list[NodeMovement]) -> list[NodeMovement]:
    """Нормализует поворотные доли отдельно для каждого входящего link.

    Основной симулятор уже должен валидировать суммы turn ratios. Эта функция
    оставлена как защитный слой, чтобы чистый solver можно было использовать в
    тестах и маленьких экспериментах без полного CTMSimulator.
    """

    totals: dict[str, float] = {}
    for movement in movements:
        totals[movement.in_link_id] = totals.get(movement.in_link_id, 0.0) + max(0.0, movement.turn_ratio)

    normalized: list[NodeMovement] = []
    for movement in movements:
        total = totals.get(movement.in_link_id, 0.0)
        ratio = 0.0 if total <= EPS else max(0.0, movement.turn_ratio) / total
        normalized.append(
            NodeMovement(
                in_link_id=movement.in_link_id,
                out_link_id=movement.out_link_id,
                turn_ratio=ratio,
                priority=max(0.0, movement.priority),
                metadata=dict(movement.metadata),
            )
        )
    return normalized


def solve_ctm_node(
    movements: list[NodeMovement],
    demands: dict[str, float],
    supplies: dict[str, float],
    *,
    fifo_strength: float = 1.0,
) -> NodeSolveResult:
    """Решает CTM-узел с явными частными случаями.

    Поддерживаемые случаи:
    - 1 вход, N выходов: FIFO diverge
          y = min(D_i, S_j / beta_j)
          f_ij = beta_j * y

    - N входов, 1 выход: priority merge
          sum_i f_i <= S_j, f_i <= D_i * beta_ij,
          дефицитная downstream supply распределяется по приоритетам;
          неиспользованные доли перераспределяются.

    - общий many-to-many случай: proportional supply allocation с опциональной
      скалярной FIFO-аппроксимацией для движений с одного входа.

    Важно: общий many-to-many fallback не является полноценной lane-based
    partial-FIFO моделью. Его нужно трактовать как консервативную инженерную
    аппроксимацию для OSM-узлов, где нет данных о полосах и группах манёвров.
    """

    clean = normalize_turn_ratios(movements)
    if not clean:
        return NodeSolveResult(
            case="empty",
            flows={},
            diagnostics={},
            node_inflow=0.0,
            node_outflow=0.0,
            conservation_error=0.0,
        )

    incoming = sorted({m.in_link_id for m in clean})
    outgoing = sorted({m.out_link_id for m in clean})
    fifo_strength = max(0.0, min(1.0, fifo_strength))

    if len(incoming) == 1:
        return solve_diverge_node(clean, demands, supplies)
    if len(outgoing) == 1:
        return solve_merge_node(clean, demands, supplies)
    return solve_general_node(clean, demands, supplies, fifo_strength=fifo_strength)


def solve_diverge_node(
    movements: list[NodeMovement],
    demands: dict[str, float],
    supplies: dict[str, float],
) -> NodeSolveResult:
    """Решает простой расходящийся узел: один вход, несколько выходов.

    Здесь используется полный FIFO: если один из выходов ограничивает движение,
    общий выпуск с входящего link уменьшается для всех поворотных долей. Это
    защищаемая CTM-идея для простого diverge, но она может быть слишком жёсткой
    для реальных перекрёстков с отдельными полосами.
    """

    clean = normalize_turn_ratios(movements)
    in_ids = {m.in_link_id for m in clean}
    if len(in_ids) != 1:
        raise ValueError("solve_diverge_node requires exactly one incoming link")
    in_id = next(iter(in_ids))
    sending = max(0.0, demands.get(in_id, 0.0))

    common_flow = sending
    active_constraints: list[str] = []
    for movement in clean:
        beta = movement.turn_ratio
        if beta <= EPS:
            continue
        out_supply = max(0.0, supplies.get(movement.out_link_id, 0.0))
        allowed = out_supply / beta
        if allowed < common_flow - EPS:
            active_constraints.append(f"supply:{movement.out_link_id}")
        common_flow = min(common_flow, allowed)

    flows: dict[MovementKey, float] = {}
    diagnostics: dict[MovementKey, MovementDiagnostics] = {}
    for movement in clean:
        key = movement_key(movement)
        desired = sending * movement.turn_ratio
        actual = common_flow * movement.turn_ratio
        constraints = [] if desired <= actual + EPS else list(active_constraints)
        flows[key] = actual
        diagnostics[key] = MovementDiagnostics(
            desired_flow=desired,
            actual_flow=actual,
            restriction_factor=1.0 if desired <= EPS else actual / desired,
            active_constraints=constraints,
        )

    total = sum(flows.values())
    return NodeSolveResult(
        case="diverge_fifo",
        flows=flows,
        diagnostics=diagnostics,
        node_inflow=total,
        node_outflow=total,
        conservation_error=0.0,
        active_constraints=sorted(set(active_constraints)),
    )


def solve_merge_node(
    movements: list[NodeMovement],
    demands: dict[str, float],
    supplies: dict[str, float],
) -> NodeSolveResult:
    """Решает простой merge-узел: несколько входов, один выход.

    Если суммарный желаемый поток превышает downstream supply, поток делится по
    priority. Приоритеты должны приходить извне. Если данных нет, вызывающий код
    должен передавать равные приоритеты, а не подменять их turn ratios.
    """

    clean = normalize_turn_ratios(movements)
    out_ids = {m.out_link_id for m in clean}
    if len(out_ids) != 1:
        raise ValueError("solve_merge_node requires exactly one outgoing link")
    out_id = next(iter(out_ids))
    supply = max(0.0, supplies.get(out_id, 0.0))

    desired: dict[MovementKey, float] = {
        movement_key(m): max(0.0, demands.get(m.in_link_id, 0.0)) * m.turn_ratio
        for m in clean
    }
    priorities: dict[MovementKey, float] = {
        movement_key(m): max(m.priority, EPS) for m in clean
    }
    flows = allocate_by_priority(desired, priorities, supply)

    diagnostics: dict[MovementKey, MovementDiagnostics] = {}
    active_constraints = []
    if sum(desired.values()) > supply + EPS:
        active_constraints.append(f"supply:{out_id}")
    for key, desired_flow in desired.items():
        actual = flows.get(key, 0.0)
        diagnostics[key] = MovementDiagnostics(
            desired_flow=desired_flow,
            actual_flow=actual,
            restriction_factor=1.0 if desired_flow <= EPS else actual / desired_flow,
            active_constraints=list(active_constraints) if actual < desired_flow - EPS else [],
        )

    total = sum(flows.values())
    return NodeSolveResult(
        case="merge_priority",
        flows=flows,
        diagnostics=diagnostics,
        node_inflow=total,
        node_outflow=total,
        conservation_error=0.0,
        active_constraints=active_constraints,
    )


def solve_general_node(
    movements: list[NodeMovement],
    demands: dict[str, float],
    supplies: dict[str, float],
    *,
    fifo_strength: float = 0.0,
) -> NodeSolveResult:
    """Решает произвольный many-to-many узел через пропорциональный fallback.

    Алгоритм сначала считает желаемые movement flows, затем для каждого выхода
    вычисляет supply factor. При fifo_strength > 0 часть ограничения переносится
    на все движения с того же входящего link.

    Это не полноценная модель полос и не настоящая partial FIFO из литературы:
    нет lane groups, conflict areas и movement influence intervals. Поэтому для
    защищаемого базового сценария лучше использовать fifo_strength=0, а значения
    выше нуля рассматривать как анализ чувствительности.
    """

    clean = normalize_turn_ratios(movements)
    desired: dict[MovementKey, float] = {
        movement_key(m): max(0.0, demands.get(m.in_link_id, 0.0)) * m.turn_ratio
        for m in clean
    }

    out_total: dict[str, float] = {}
    for movement in clean:
        out_total[movement.out_link_id] = out_total.get(movement.out_link_id, 0.0) + desired[movement_key(movement)]

    nonfifo_factor: dict[MovementKey, float] = {}
    for movement in clean:
        key = movement_key(movement)
        total = out_total.get(movement.out_link_id, 0.0)
        supply = max(0.0, supplies.get(movement.out_link_id, 0.0))
        nonfifo_factor[key] = 1.0 if total <= EPS else min(1.0, supply / total)

    fifo_by_in: dict[str, float] = {}
    for movement in clean:
        key = movement_key(movement)
        current = fifo_by_in.get(movement.in_link_id, 1.0)
        fifo_by_in[movement.in_link_id] = min(current, nonfifo_factor[key])

    flows: dict[MovementKey, float] = {}
    diagnostics: dict[MovementKey, MovementDiagnostics] = {}
    active_constraints: list[str] = []
    for movement in clean:
        key = movement_key(movement)
        factor = (1.0 - fifo_strength) * nonfifo_factor[key] + fifo_strength * fifo_by_in[movement.in_link_id]
        actual = desired[key] * factor
        flows[key] = actual
        constraints: list[str] = []
        if nonfifo_factor[key] < 1.0 - EPS:
            constraints.append(f"supply:{movement.out_link_id}")
        if fifo_strength > 0.0 and fifo_by_in[movement.in_link_id] < nonfifo_factor[key] - EPS:
            constraints.append(f"fifo:{movement.in_link_id}")
        active_constraints.extend(constraints)
        diagnostics[key] = MovementDiagnostics(
            desired_flow=desired[key],
            actual_flow=actual,
            restriction_factor=1.0 if desired[key] <= EPS else actual / desired[key],
            active_constraints=constraints,
        )

    total = sum(flows.values())
    return NodeSolveResult(
        case="general_proportional_partial_fifo",
        flows=flows,
        diagnostics=diagnostics,
        node_inflow=total,
        node_outflow=total,
        conservation_error=0.0,
        active_constraints=sorted(set(active_constraints)),
    )


def allocate_by_priority(
    desired: dict[MovementKey, float],
    priorities: dict[MovementKey, float],
    supply: float,
) -> dict[MovementKey, float]:
    """Распределяет дефицитную merge supply по приоритетам.

    Это небольшой water-filling алгоритм: каждое активное движение получает
    priority-weighted долю оставшейся supply; движения, чей desired flow меньше
    этой доли, удовлетворяются полностью, а неиспользованный остаток
    перераспределяется между остальными.
    """

    flows = {key: 0.0 for key in desired}
    remaining = {key for key, value in desired.items() if value > EPS}
    remaining_supply = max(0.0, supply)

    while remaining and remaining_supply > EPS:
        weight_sum = sum(max(priorities.get(key, 1.0), EPS) for key in remaining)
        if weight_sum <= EPS:
            break
        allocated_this_round = 0.0
        satisfied: set[MovementKey] = set()
        for key in list(remaining):
            weight = max(priorities.get(key, 1.0), EPS)
            share = remaining_supply * weight / weight_sum
            need = desired[key] - flows[key]
            add = min(need, share)
            flows[key] += add
            allocated_this_round += add
            if need <= share + EPS:
                satisfied.add(key)
        if allocated_this_round <= EPS:
            break
        remaining_supply -= allocated_this_round
        remaining -= satisfied
        if not satisfied and remaining_supply <= EPS:
            break

    return flows
