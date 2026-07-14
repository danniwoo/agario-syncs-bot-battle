# v2.5 builds on top of my_bot_v2.py (Stage 1 - 3e). New in this file:
#   Stage 3f - 病毒閃避改成「風險加權」而不是無條件觸發。原本只要大到會被
#              炸就無條件閃，會造成「想追獵物 -> 靠近病毒觸發閃避 -> 退開 ->
#              沒有威脅又想追 -> 再次靠近觸發閃避」的來回卡死（親眼在 replay
#              裡看到 P4 這樣卡住吃不到 P0）。實際上炸開本身不一定危險：只要
#              附近沒有人能吃掉炸開後的碎片，碎片會自動慢慢合體，繞路反而是
#              浪費。用引擎的碎片數公式（MAX_BLOB_COUNT - 目前 blob 數 + 1）
#              預測炸開後每一塊大概多重，只有「炸開後真的有能撿便宜的敵人在
#              視野內」才觸發閃避，否則直接穿過去。
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
# virus that would actually put us in danger.
VIRUS_AVOIDANCE_BUFFER = 2.0
# lib/config/arena.py -- caps how many pieces a single virus hit can split a
# blob into (engine/state/state_mutator.py _resolve_viruses:
# piece_count = MAX_BLOB_COUNT - len(player.blobs) + 1). Not exposed via
# game.state, so we keep our own copy the same way we do for EAT_SIZE_RATIO.
MAX_BLOB_COUNT = 16
# How many rounds to let a chase run before checking whether it's actually
# closing the distance.
GIVE_UP_WINDOW_ROUNDS = 15
# Once we give up on a target, don't re-lock it for this many rounds, so we
# actually go do something else instead of immediately flip-flopping back.
ABANDON_COOLDOWN_ROUNDS = 30


def _mass(radius: float) -> float:
    return radius * radius


def _weakest_and_strongest_mass(me) -> tuple[float, float]:
    """Our own blobs' mass range (post-split we may have several).

    Combat is resolved per individual blob, not by total mass -- use the
    weakest blob for "can I be eaten" checks and the strongest for "can I
    eat/survive this" checks.
    """
    blob_masses = [_mass(blob.radius) for blob in me.blobs.values()]
    return min(blob_masses), max(blob_masses)


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


def _repulsion_vector(me_x: float, me_y: float, danger_positions) -> tuple[float, float]:
    """Sum a weighted repulsion vector away from every position in
    danger_positions (closer ones push harder), instead of reacting to only
    the single nearest one.

    Picking just the nearest danger means its identity can flip between two
    similarly-distant ones from one tick to the next, flipping our direction
    ~180 degrees and cancelling the previous tick's movement -- this is what
    caused the "shaking in place" behaviour. A continuous weighted sum doesn't
    have that discrete flip.
    """
    push_x, push_y = 0.0, 0.0
    for pos_x, pos_y in danger_positions:
        away_x, away_y = me_x - pos_x, me_y - pos_y
        distance = math.hypot(away_x, away_y)
        if distance == 0:
            continue
        weight = 1.0 / distance
        push_x += away_x / distance * weight
        push_y += away_y / distance * weight
    return push_x, push_y


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


def _predicted_piece_mass(my_strongest_mass: float, my_blob_count: int, virus_radius: float) -> float:
    """Mass of the smallest resulting fragment if our biggest blob hits this
    virus right now (engine splits the combined mass evenly across pieces)."""
    piece_count = max(1, MAX_BLOB_COUNT - my_blob_count + 1)
    total_mass = my_strongest_mass + _mass(virus_radius)
    return total_mass / piece_count


def _dangerous_virus_positions(
    me_x: float,
    me_y: float,
    my_radius: float,
    my_strongest_mass: float,
    my_blob_count: int,
    viruses,
    enemies,
):
    """Positions of viruses actually worth detouring around right now.

    A virus only splits a blob whose mass clears the same EAT_SIZE_RATIO rule
    used for eating other blobs; below that we can safely sit on top of one.
    But being *able* to be split isn't the same as it being *dangerous* --
    if no visible enemy is big enough to eat the resulting fragments, getting
    hit just scatters us into pieces that regroup on their own, and detouring
    around it only wastes time (this was causing a stuck back-and-forth when
    a much bigger blob wanted to chase prey on the other side of a virus it
    could easily have just walked through).
    """
    danger_radius = my_radius + VIRUS_AVOIDANCE_BUFFER
    positions = []
    for virus in viruses:
        if my_strongest_mass <= _mass(virus.radius) * EAT_SIZE_RATIO:
            continue
        distance = math.hypot(me_x - virus.pos[0], me_y - virus.pos[1])
        if not (0 < distance < danger_radius):
            continue
        piece_mass = _predicted_piece_mass(my_strongest_mass, my_blob_count, virus.radius)
        exploitable = any(_mass(enemy.radius) > piece_mass * EAT_SIZE_RATIO for enemy in enemies)
        if exploitable:
            positions.append(virus.pos)
    return positions


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
    weakest_mass, strongest_mass = _weakest_and_strongest_mass(me)
    size = game.state.map.size
    round_ = game.state.round
    # visible_blobs includes our own blobs, so filter those out first.
    enemies = [blob for blob in game.state.visible_blobs if blob.player_id != me.player_id]

    # Priority 1 [Stage 1 + 3a + 3d + 3e + 3f]: flee anyone who could eat our
    # weakest blob, and steer away from any virus that would actually expose
    # us to a nearby predator once split -- both blended into one vector so
    # escaping a threat can't blindly run us into a virus (and vice versa).
    threats = [blob for blob in enemies if _mass(blob.radius) > weakest_mass * EAT_SIZE_RATIO]
    virus_positions = _dangerous_virus_positions(
        me.x, me.y, me.radius, strongest_mass, len(me.blobs), game.state.visible_viruses, enemies
    )
    if threats or virus_positions:
        danger_positions = [blob.pos for blob in threats] + virus_positions
        flee_x, flee_y = _repulsion_vector(me.x, me.y, danger_positions)
        return _slide_off_walls(flee_x, flee_y, me.x, me.y, size)

    # Priority 2 [Stage 2 + Stage 3 + Stage 3c + 3e]: chase a locked-on,
    # safe-to-eat prey (using our strongest blob's mass, since only that blob
    # can actually land the kill), giving up on chases that aren't closing.
    prey_blobs = [blob for blob in enemies if _mass(blob.radius) < strongest_mass * HUNT_MASS_RATIO]
    target = _select_prey(tracker, prey_blobs, me, round_)
    if target is not None:
        target_x, target_y = _predicted_prey_position(me.x, me.y, target, size)
        return (target_x - me.x, target_y - me.y)

    # Priority 3 [Stage 1 baseline]: nothing worth fighting nearby, fall back to food.
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
