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
# How far ahead along its naive flee heading we predict a prey's position, so we
# aim at where it's going rather than where it is right now.
INTERCEPT_LOOKAHEAD = 4.0
# Extra margin beyond the exact collision radius to start steering around a
# virus we're big enough to be split by (VirusModel exposes its own radius, so
# we don't need to hardcode VIRUS_SIZE here).
VIRUS_AVOIDANCE_BUFFER = 2.0
# How many rounds to let a chase run before checking whether it's actually
# closing the distance.
GIVE_UP_WINDOW_ROUNDS = 15
# Once we give up on a target, don't re-lock it for this many rounds, so we
# actually go do something else instead of immediately flip-flopping back.
ABANDON_COOLDOWN_ROUNDS = 30


def _mass(radius: float) -> float:
    return radius * radius


def _slide_off_walls(dx: float, dy: float, x: float, y: float, size: float) -> tuple[float, float]:
    """Redirect a direction vector so it never fights a wall we're already near.

    The engine normalises our (dx, dy) but treats an exact (0, 0) as "don't move
    this tick" -- so adding a separate push-away-from-wall vector on top of the
    flee vector can partially or fully cancel it out whenever a threat happens to
    be pushing us toward the wall we're already hugging, leaving us stuck
    oscillating in place. Zeroing the wall-ward component instead (rather than
    opposing it with another vector) avoids that cancellation and just slides us
    along the wall using whatever component was already available.
    """
    if x < BOUNDARY_MARGIN and dx < 0:
        dx = 0.0
    elif x > size - BOUNDARY_MARGIN and dx > 0:
        dx = 0.0
    if y < BOUNDARY_MARGIN and dy < 0:
        dy = 0.0
    elif y > size - BOUNDARY_MARGIN and dy > 0:
        dy = 0.0

    if dx == 0.0 and dy == 0.0:
        # Pinned against two walls at once with no lateral component left: the
        # only way out is back toward the centre.
        return (size / 2.0 - x, size / 2.0 - y)
    return (dx, dy)


def _flee_vector(me_x: float, me_y: float, threat_blobs) -> tuple[float, float]:
    """Sum a repulsion vector from every threat instead of fleeing the single
    nearest one.

    Picking just the nearest threat means the identity of "nearest" can flip
    between two similarly-distant threats from one tick to the next, flipping
    our flee angle ~180 degrees and cancelling the previous tick's movement --
    this is what caused the "shaking in place" behaviour. Summing weighted
    repulsion from all of them varies continuously instead.
    """
    flee_x, flee_y = 0.0, 0.0
    for blob in threat_blobs:
        away_x, away_y = me_x - blob.pos[0], me_y - blob.pos[1]
        distance = math.hypot(away_x, away_y)
        if distance == 0:
            continue
        weight = 1.0 / distance
        flee_x += away_x / distance * weight
        flee_y += away_y / distance * weight
    return flee_x, flee_y


def _predicted_prey_position(
    me_x: float, me_y: float, blob, size: float
) -> tuple[float, float]:
    """Where a prey blob will likely be if it flees straight away from us.

    Clamped to the arena bounds, so a prey fleeing into a wall predicts a
    position pinned against that wall -- aiming here instead of at its current
    position naturally cuts the corner instead of trailing directly behind.
    """
    away_x, away_y = blob.pos[0] - me_x, blob.pos[1] - me_y
    distance = math.hypot(away_x, away_y)
    if distance == 0:
        return blob.pos
    unit_x, unit_y = away_x / distance, away_y / distance
    predicted_x = blob.pos[0] + unit_x * INTERCEPT_LOOKAHEAD
    predicted_y = blob.pos[1] + unit_y * INTERCEPT_LOOKAHEAD
    predicted_x = min(max(predicted_x, blob.radius), size - blob.radius)
    predicted_y = min(max(predicted_y, blob.radius), size - blob.radius)
    return (predicted_x, predicted_y)


def _virus_danger_vector(me_x: float, me_y: float, my_mass: float, my_radius: float, viruses) -> tuple[float, float]:
    """Repel away from any nearby virus we're big enough to be split by.

    A virus only splits a blob whose mass clears the same EAT_SIZE_RATIO rule
    used for eating other blobs (engine/state/state_mutator.py
    _can_consume_virus); below that we can safely sit on top of one. The
    collision check itself only triggers once the virus's centre is inside our
    own radius, so we start steering away a bit before that (radius + buffer)
    to leave room to manoeuvre.
    """
    push_x, push_y = 0.0, 0.0
    for virus in viruses:
        if my_mass <= _mass(virus.radius) * EAT_SIZE_RATIO:
            continue
        away_x, away_y = me_x - virus.pos[0], me_y - virus.pos[1]
        distance = math.hypot(away_x, away_y)
        danger_radius = my_radius + VIRUS_AVOIDANCE_BUFFER
        if distance == 0 or distance >= danger_radius:
            continue
        weight = 1.0 / distance
        push_x += away_x / distance * weight
        push_y += away_y / distance * weight
    return push_x, push_y


class HuntTracker:
    """Cross-tick memory for the hunt: which target we're currently locked
    onto, and a cooldown on targets we've given up on.

    Re-picking "nearest prey" from scratch every tick has the same jitter bug
    the flee logic used to have: when two prey are similarly distant, "nearest"
    can flip between them tick to tick, flipping our aim direction and
    cancelling the previous tick's movement. Locking onto one target until it's
    gone (or proven uncatchable) fixes that, and doubles as the bookkeeping
    needed to give up on a chase that isn't actually closing the distance.
    """

    def __init__(self) -> None:
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


def _select_prey(tracker: HuntTracker, prey_blobs, me, round_: int):
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


def choose_direction(game: Game, tracker: HuntTracker) -> tuple[float, float]:
    me = game.state.me
    my_mass = _mass(me.radius)
    size = game.state.map.size
    round_ = game.state.round
    # visible_blobs includes our own blobs, so filter those out first.
    enemies = [blob for blob in game.state.visible_blobs if blob.player_id != me.player_id]

    # Priority 1: flee anyone who could eat us, regardless of anything else.
    threats = [blob for blob in enemies if _mass(blob.radius) > my_mass * EAT_SIZE_RATIO]
    if threats:
        flee_x, flee_y = _flee_vector(me.x, me.y, threats)
        return _slide_off_walls(flee_x, flee_y, me.x, me.y, size)

    # Priority 2: no direct threat, but don't blunder into a virus we're too
    # big to safely touch.
    virus_x, virus_y = _virus_danger_vector(me.x, me.y, my_mass, me.radius, game.state.visible_viruses)
    if virus_x != 0.0 or virus_y != 0.0:
        return _slide_off_walls(virus_x, virus_y, me.x, me.y, size)

    # Priority 3: chase a locked-on, safe-to-eat prey, giving up on chases that
    # aren't actually closing the distance.
    prey_blobs = [blob for blob in enemies if _mass(blob.radius) < my_mass * HUNT_MASS_RATIO]
    target = _select_prey(tracker, prey_blobs, me, round_)
    if target is not None:
        target_x, target_y = _predicted_prey_position(me.x, me.y, target, size)
        return (target_x - me.x, target_y - me.y)

    # Priority 4: nothing worth fighting nearby, fall back to food.
    if game.state.visible_food:
        food_target = min(
            game.state.visible_food,
            key=lambda food: (food.pos[0] - me.x) ** 2 + (food.pos[1] - me.y) ** 2,
        )
        return (food_target.pos[0] - me.x, food_target.pos[1] - me.y)
    return (1.0, 0.0)


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
