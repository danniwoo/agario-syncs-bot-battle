import math

from helper.game import Game
from lib.interface.events.moves.move_player import MovePlayer
from lib.interface.queries.query_move import QueryMovePlayer
from lib.models.penguin_model import DirectionModel

# Engine eats by mass (radius^2), not raw radius (see engine/state/state_mutator.py
# _can_eat_blob: eater.mass must exceed target.mass * EAT_SIZE_RATIO). Comparing
# radius directly would misjudge threats by a factor of sqrt(1.2) ~= 1.1.
EAT_SIZE_RATIO = 1.2
# Hunt only prey well below the bare "can eat" threshold so we're not chasing
# something that might out-eat us if it grows before we catch it.
HUNT_MASS_RATIO = 0.7
# Blobs get clamped at the wall rather than hurt by it; the real danger is being
# cornered (pinned against two walls with no escape heading). Start pushing back
# once we're within this many units of a wall.
BOUNDARY_MARGIN = 5.0
# How many ticks ahead we extrapolate a prey's position, using its measured
# velocity once we've seen it move, or a naive "fleeing straight away from us"
# guess the first tick we spot it.
INTERCEPT_LOOKAHEAD_TICKS = 4.0
# Movement speed is max(MIN_PLAYER_SPEED, BASE_PLAYER_SPEED / (1 + radius * k)),
# strictly decreasing in radius -- so anything under HUNT_MASS_RATIO is smaller
# AND always faster than us. A tail chase in open space mathematically never
# closes the gap; the only way we ever catch prey on open ground is by cutting
# an angle rather than racing it in a straight line. This is how far to the
# side of the predicted position we aim instead of trailing directly behind.
FLANK_OFFSET = 2.0
# How many rounds to let a chase run before checking whether it's actually
# closing the distance.
GIVE_UP_WINDOW_ROUNDS = 15
# Once we give up on a target, don't re-lock it for this many rounds, so we
# actually go do something else instead of immediately flip-flopping back.
ABANDON_COOLDOWN_ROUNDS = 30


def _mass(radius: float) -> float:
    return radius * radius


def _boundary_push(x: float, y: float, size: float) -> tuple[float, float]:
    """Vector pointing inward, away from any wall within BOUNDARY_MARGIN; zero if clear.

    Magnitude grows the closer we are to a wall, so it naturally combines into a
    diagonal push when we're near a corner (close to two walls at once).
    """
    push_x = 0.0
    if x < BOUNDARY_MARGIN:
        push_x = BOUNDARY_MARGIN - x
    elif x > size - BOUNDARY_MARGIN:
        push_x = (size - BOUNDARY_MARGIN) - x

    push_y = 0.0
    if y < BOUNDARY_MARGIN:
        push_y = BOUNDARY_MARGIN - y
    elif y > size - BOUNDARY_MARGIN:
        push_y = (size - BOUNDARY_MARGIN) - y

    return push_x, push_y


class HuntTracker:
    """Cross-tick memory: last-seen prey positions (to measure their actual
    velocity), which target we're currently committed to, and a cooldown on
    targets we've already given up on so we don't immediately re-lock them."""

    def __init__(self) -> None:
        self.last_positions: dict[int, tuple[float, float]] = {}
        self.target_id: int | None = None
        self.lock_distance: float = 0.0
        self.lock_round: int = 0
        self.abandoned_until: dict[int, int] = {}

    def give_up(self, blob_id: int, round_: int) -> None:
        self.abandoned_until[blob_id] = round_ + ABANDON_COOLDOWN_ROUNDS
        if self.target_id == blob_id:
            self.target_id = None

    def is_on_cooldown(self, blob_id: int, round_: int) -> bool:
        return round_ < self.abandoned_until.get(blob_id, -1)


def _select_target(tracker: HuntTracker, prey_blobs, me, round_: int):
    """Stick with the locked target as long as it's still around, giving up if
    we haven't closed the distance over the last GIVE_UP_WINDOW_ROUNDS.
    Otherwise lock onto the nearest eligible prey not currently on cooldown."""
    by_id = {blob.blob_id: blob for blob in prey_blobs}

    if tracker.target_id is not None and tracker.target_id in by_id:
        blob = by_id[tracker.target_id]
        distance = math.hypot(blob.pos[0] - me.x, blob.pos[1] - me.y)
        if round_ - tracker.lock_round >= GIVE_UP_WINDOW_ROUNDS:
            if distance >= tracker.lock_distance:
                tracker.give_up(tracker.target_id, round_)
                return None
            tracker.lock_distance = distance
            tracker.lock_round = round_
        return blob

    candidates = [blob for blob in prey_blobs if not tracker.is_on_cooldown(blob.blob_id, round_)]
    if not candidates:
        return None

    nearest = min(candidates, key=lambda blob: (blob.pos[0] - me.x) ** 2 + (blob.pos[1] - me.y) ** 2)
    tracker.target_id = nearest.blob_id
    tracker.lock_distance = math.hypot(nearest.pos[0] - me.x, nearest.pos[1] - me.y)
    tracker.lock_round = round_
    return nearest


def _intercept_point(
    tracker: HuntTracker, blob, me_x: float, me_y: float, size: float
) -> tuple[float, float]:
    """Predict where the prey is heading (from its measured velocity, or a
    "flees straight away from us" guess if we've just spotted it) and aim from
    whichever side lands closer to the arena centre, instead of trailing it
    head-on."""
    last_pos = tracker.last_positions.get(blob.blob_id)
    if last_pos is not None:
        vx, vy = blob.pos[0] - last_pos[0], blob.pos[1] - last_pos[1]
    else:
        vx, vy = 0.0, 0.0

    if vx == 0.0 and vy == 0.0:
        vx, vy = blob.pos[0] - me_x, blob.pos[1] - me_y

    speed = math.hypot(vx, vy)
    if speed == 0.0:
        return blob.pos

    unit_x, unit_y = vx / speed, vy / speed
    predicted_x = blob.pos[0] + unit_x * INTERCEPT_LOOKAHEAD_TICKS
    predicted_y = blob.pos[1] + unit_y * INTERCEPT_LOOKAHEAD_TICKS
    predicted_x = min(max(predicted_x, blob.radius), size - blob.radius)
    predicted_y = min(max(predicted_y, blob.radius), size - blob.radius)

    perp_x, perp_y = -unit_y, unit_x
    center = size / 2.0
    option_a = (predicted_x + perp_x * FLANK_OFFSET, predicted_y + perp_y * FLANK_OFFSET)
    option_b = (predicted_x - perp_x * FLANK_OFFSET, predicted_y - perp_y * FLANK_OFFSET)
    dist_a = (option_a[0] - center) ** 2 + (option_a[1] - center) ** 2
    dist_b = (option_b[0] - center) ** 2 + (option_b[1] - center) ** 2
    return option_a if dist_a < dist_b else option_b


def choose_direction(game: Game, tracker: HuntTracker) -> tuple[float, float]:
    me = game.state.me
    my_mass = _mass(me.radius)
    size = game.state.map.size
    round_ = game.state.round
    # visible_blobs includes our own blobs, so filter those out first.
    enemies = [blob for blob in game.state.visible_blobs if blob.player_id != me.player_id]

    threats = [
        (blob.pos[0] - me.x, blob.pos[1] - me.y)
        for blob in enemies
        if _mass(blob.radius) > my_mass * EAT_SIZE_RATIO
    ]
    if threats:
        nearest = min(threats, key=lambda pos: pos[0] ** 2 + pos[1] ** 2)
        flee_x, flee_y = -nearest[0], -nearest[1]
        push_x, push_y = _boundary_push(me.x, me.y, size)
        result = (flee_x + push_x, flee_y + push_y)
    else:
        prey_blobs = [blob for blob in enemies if _mass(blob.radius) < my_mass * HUNT_MASS_RATIO]
        target = _select_target(tracker, prey_blobs, me, round_)
        if target is not None:
            target_x, target_y = _intercept_point(tracker, target, me.x, me.y, size)
            result = (target_x - me.x, target_y - me.y)
        elif game.state.visible_food:
            target_food = min(
                game.state.visible_food,
                key=lambda food: (food.pos[0] - me.x) ** 2 + (food.pos[1] - me.y) ** 2,
            )
            result = (target_food.pos[0] - me.x, target_food.pos[1] - me.y)
        else:
            result = (1.0, 0.0)

    # Must happen last: _intercept_point above needs last tick's positions to
    # measure velocity, so only overwrite them once we're done using them.
    tracker.last_positions = {blob.blob_id: blob.pos for blob in enemies}
    return result


def main() -> None:
    game = Game()
    tracker = HuntTracker()

    while True:
        query = game.get_next_query()
        match query:
            case QueryMovePlayer():
                dx, dy = choose_direction(game, tracker)
                game.send_move(
                    MovePlayer(
                        player_id=game.state.me.player_id,
                        direction=DirectionModel(x=dx, y=dy),
                    )
                )
            case _:
                raise RuntimeError(f"Unsupported query type: {type(query)}")


if __name__ == "__main__":
    main()
