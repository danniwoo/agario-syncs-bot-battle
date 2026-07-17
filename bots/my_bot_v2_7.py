# v2.7 builds on top of my_bot_v2_6.py (Stage 1 - 3g). Merges the 4 isolated
# features (out of 8 ported from v5.py/v5_1.py and tested individually vs
# v2.6, 4-5 headless runs each) that showed a clear improvement head-to-head:
#
#   Stage 3h [C] - Proactive wall/corner repulsion field: instead of only
#     reacting once already touching a wall (_slide_off_walls), start pushing
#     back well before that (quadratic growth as we approach), so the escape
#     path curves away smoothly instead of getting corrected at the last
#     moment. +45% avg mass vs v2.6 in isolation testing, the strongest of
#     the four.
#   Stage 3i [G] - Fragment sniping: when an enemy has been split into
#     multiple blobs (e.g. by a virus), prioritise eating an isolated
#     fragment away from its bigger siblings over normal hunting -- close to
#     a free kill. +41% in isolation.
#   Stage 3j [A] - Split-lunge attack: when within lunge range of a
#     catchable target but not yet close enough to eat it, split toward it if
#     the post-split piece would still safely finish the kill (checked
#     against every visible enemy and virus). Our first use of split=True.
#     +27% in isolation.
#   Stage 3k [D] - Chase cost/risk vetting: before (and while) committing to
#     a hunt, bail out if it would take too many estimated rounds to close
#     the distance, or if the path to the target runs through a cluster of
#     big blobs. +14% in isolation.
#
# Left out (tested individually, no clear win vs v2.6, see conversation notes
# for hypotheses): B (real-blob-vs-centroid targeting fix -- only matters
# once we're actually split, which barely happened without A; worth
# retesting now that A is in), E (split-escape when cornered -- too rare a
# trigger in a 5-run sample to tell signal from noise), F (zigzag evasion --
# costs straight-line retreat speed for a prediction-dodge that didn't pay
# off against this simple a pursuer), H (food density field -- extra travel
# to reach a "denser" cluster didn't beat just grabbing the nearest pellet
# at this food count/map size, and its weights were tuned for a different
# bot's full feature set).
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
# Lower bound on how small a prey is worth hunting at all -- below this mass
# ratio, a prey that reaches a corner is geometrically uncatchable regardless
# of how long we chase it (see my_bot_v2_6.py Stage 3g for the derivation).
MIN_HUNT_MASS_RATIO = (1 - 1 / math.sqrt(2)) ** 2
# Blobs get clamped at the wall rather than hurt by it; the real danger is being
# cornered. This is now mostly a last-resort safety net -- the Stage 3h active
# field below handles the normal case.
BOUNDARY_MARGIN = 5.0
# How far ahead along its naive flee heading we predict a prey's position, so we
# aim at where it's going rather than where it is right now.
INTERCEPT_LOOKAHEAD = 4.0
# Extra margin beyond the exact collision radius to start steering around a
# virus that would actually put us in danger.
VIRUS_AVOIDANCE_BUFFER = 2.0
# lib/config/arena.py -- caps how many pieces a single virus hit can split a
# blob into. Not exposed via game.state, so we keep our own copy.
MAX_BLOB_COUNT = 16
# How many rounds to let a chase run before checking whether it's actually
# closing the distance.
GIVE_UP_WINDOW_ROUNDS = 15
# Once we give up on a target, don't re-lock it for this many rounds, so we
# actually go do something else instead of immediately flip-flopping back.
ABANDON_COOLDOWN_ROUNDS = 30

# --- Stage 3h: proactive wall/corner repulsion ---
WALL_REPULSION_MARGIN = 10.0
WALL_REPULSION_STRENGTH = 5.0
CORNER_REPULSION_MARGIN = 14.0
CORNER_REPULSION_STRENGTH = 9.0

# --- Stage 3i: fragment sniping ---
FRAGMENT_REWARD_MULTIPLIER = 3.0
CORNER_PIN_MARGIN = 3.0
SQRT_2 = 1.41421356

# --- Stage 3j: split-lunge attack ---
SPLIT_MIN_MASS = 2.0
SPLIT_LUNGE_RANGE = 5.0
SPLIT_MIN_RANGE_FACTOR = 1.15
SPLIT_COOLDOWN_FRAMES = 18
BASE_PLAYER_SPEED = 1.1
PLAYER_SPEED_RADIUS_FACTOR = 0.08
MIN_PLAYER_SPEED = 0.25

# --- Stage 3k: chase cost/risk vetting ---
MAX_CHASE_ROUNDS = 30
CHASE_CORRIDOR_WIDTH = 4.0
BIG_BLOB_MASS_RATIO = 0.8
DENSE_BIG_BLOB_COUNT = 2


def _mass(radius: float) -> float:
    return radius * radius


def _movement_speed(radius: float) -> float:
    return max(MIN_PLAYER_SPEED, BASE_PLAYER_SPEED / (1.0 + radius * PLAYER_SPEED_RADIUS_FACTOR))


def _weakest_and_strongest_mass(me) -> tuple[float, float]:
    """Our own blobs' mass range (post-split we may have several).

    Combat is resolved per individual blob, not by total mass -- use the
    weakest blob for "can I be eaten" checks and the strongest for "can I
    eat/survive this" checks.
    """
    blob_masses = [_mass(blob.radius) for blob in me.blobs.values()]
    return min(blob_masses), max(blob_masses)


def _slide_off_walls(dx: float, dy: float, x: float, y: float, size: float) -> tuple[float, float]:
    """Last-resort safety net: if we're still moving into a wall we're
    already touching (the Stage 3h field below should normally prevent this),
    zero that component instead of letting it cancel against another vector
    and cause the "shaking in place" bug -- see my_bot_v1.py Stage 3a.
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
        return (size / 2.0 - x, size / 2.0 - y)
    return (dx, dy)


def _wall_repulsion_vector(x: float, y: float, size: float) -> tuple[float, float]:
    """Push toward the centre starting WALL_REPULSION_MARGIN units from a
    wall, growing quadratically as we get closer, so the flee path curves
    away smoothly instead of getting corrected only once already touching."""
    push_x, push_y = 0.0, 0.0
    if x < WALL_REPULSION_MARGIN:
        w = ((WALL_REPULSION_MARGIN - x) / WALL_REPULSION_MARGIN) ** 2
        push_x += w * WALL_REPULSION_STRENGTH
    elif x > size - WALL_REPULSION_MARGIN:
        w = ((WALL_REPULSION_MARGIN - (size - x)) / WALL_REPULSION_MARGIN) ** 2
        push_x -= w * WALL_REPULSION_STRENGTH
    if y < WALL_REPULSION_MARGIN:
        w = ((WALL_REPULSION_MARGIN - y) / WALL_REPULSION_MARGIN) ** 2
        push_y += w * WALL_REPULSION_STRENGTH
    elif y > size - WALL_REPULSION_MARGIN:
        w = ((WALL_REPULSION_MARGIN - (size - y)) / WALL_REPULSION_MARGIN) ** 2
        push_y -= w * WALL_REPULSION_STRENGTH
    return push_x, push_y


def _corner_repulsion_vector(x: float, y: float, size: float) -> tuple[float, float]:
    """Extra repulsion layered on top of the wall field specifically around
    the four corners, since two wall-field components alone don't grow fast
    enough to reliably keep us out of the worst spot on the map."""
    push_x, push_y = 0.0, 0.0
    for cx, cy in ((0.0, 0.0), (0.0, size), (size, 0.0), (size, size)):
        dist = math.hypot(x - cx, y - cy)
        if dist < CORNER_REPULSION_MARGIN and dist > 0:
            weight = ((CORNER_REPULSION_MARGIN - dist) / CORNER_REPULSION_MARGIN) ** 2
            push_x += (x - cx) / dist * weight * CORNER_REPULSION_STRENGTH
            push_y += (y - cy) / dist * weight * CORNER_REPULSION_STRENGTH
    return push_x, push_y


def _repulsion_vector(me_x: float, me_y: float, danger_positions) -> tuple[float, float]:
    """Sum a weighted repulsion vector away from every position in
    danger_positions (closer ones push harder), instead of reacting to only
    the single nearest one -- avoids the discrete-flip jitter bug."""
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


def _predicted_prey_position(me_x: float, me_y: float, blob, size: float) -> tuple[float, float]:
    """Where a prey blob will likely be if it flees straight away from us,
    clamped to the arena so a prey fleeing into a wall predicts a position
    pinned against that wall -- cuts the corner instead of trailing it."""
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
    me_x: float, me_y: float, my_radius: float, my_strongest_mass: float, my_blob_count: int, viruses, enemies
):
    """Positions of viruses actually worth detouring around right now --
    only if a visible enemy is big enough to eat the resulting fragments."""
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


def _is_corner_pinned(blob, size: float) -> bool:
    near_x_wall = blob.pos[0] <= blob.radius + CORNER_PIN_MARGIN or blob.pos[0] >= size - blob.radius - CORNER_PIN_MARGIN
    near_y_wall = blob.pos[1] <= blob.radius + CORNER_PIN_MARGIN or blob.pos[1] >= size - blob.radius - CORNER_PIN_MARGIN
    return near_x_wall and near_y_wall


def _is_corner_deadlocked(target, my_radius: float, size: float) -> bool:
    """Geometric deadlock check (see my_bot_v2_6.py Stage 3g): if our own
    radius is too far above the target's for our clamped centre to ever reach
    within eating range of a corner-pinned target's centre, it's unreachable."""
    if my_radius <= target.radius:
        return False
    if not _is_corner_pinned(target, size):
        return False
    limit_distance = SQRT_2 * (my_radius - target.radius)
    return limit_distance > my_radius


def _find_fragment_opportunity(me, strongest_mass: float, enemies, size: float, my_radius: float):
    """An isolated fragment of a multi-blob enemy (e.g. post-virus-split),
    away from any of its own siblings big enough to reclaim it -- close to a
    free kill, so worth prioritising over normal prey."""
    groups: dict[int, list] = {}
    for blob in enemies:
        groups.setdefault(blob.player_id, []).append(blob)

    best = None
    best_score = float("-inf")
    for blobs in groups.values():
        if len(blobs) < 2:
            continue
        dangerous_siblings = [b for b in blobs if _mass(b.radius) > strongest_mass * EAT_SIZE_RATIO]
        for frag in blobs:
            frag_mass = _mass(frag.radius)
            if frag_mass >= strongest_mass * HUNT_MASS_RATIO:
                continue
            if _is_corner_deadlocked(frag, my_radius, size):
                continue
            near_dangerous_sibling = any(
                math.hypot(frag.pos[0] - sib.pos[0], frag.pos[1] - sib.pos[1]) < (sib.radius + 5.0)
                for sib in dangerous_siblings
            )
            if near_dangerous_sibling:
                continue
            distance = math.hypot(frag.pos[0] - me.x, frag.pos[1] - me.y)
            score = frag_mass * FRAGMENT_REWARD_MULTIPLIER - distance
            if score > best_score:
                best_score = score
                best = frag
    return best


def _landing_spot_has_exploitable_virus(target, piece_mass: float, my_blob_count: int, viruses, enemies) -> bool:
    landing_x, landing_y = target.pos
    for virus in viruses:
        if piece_mass <= _mass(virus.radius) * EAT_SIZE_RATIO:
            continue
        distance = math.hypot(landing_x - virus.pos[0], landing_y - virus.pos[1])
        if not (distance < virus.radius + VIRUS_AVOIDANCE_BUFFER):
            continue
        sub_piece_mass = _predicted_piece_mass(piece_mass, my_blob_count + 1, virus.radius)
        for enemy in enemies:
            if _mass(enemy.radius) <= sub_piece_mass * EAT_SIZE_RATIO:
                continue
            reach = _movement_speed(enemy.radius) * SPLIT_COOLDOWN_FRAMES
            if math.hypot(enemy.pos[0] - landing_x, enemy.pos[1] - landing_y) < (reach + enemy.radius):
                return True
    return False


def _split_kill_is_safe(me_x: float, me_y: float, piece_mass: float, my_blob_count: int, target, enemies, viruses) -> bool:
    """Would splitting toward target actually land a safe kill: our post-split
    piece must still out-mass the target, and neither the landing spot nor a
    nearby enemy's reach (during our post-split cooldown vulnerability) may
    threaten to eat that weakened piece."""
    if piece_mass < target.radius * target.radius * EAT_SIZE_RATIO:
        return False
    if _landing_spot_has_exploitable_virus(target, piece_mass, my_blob_count, viruses, enemies):
        return False
    landing_x, landing_y = target.pos
    for enemy in enemies:
        if _mass(enemy.radius) <= piece_mass * EAT_SIZE_RATIO:
            continue
        danger_reach = _movement_speed(enemy.radius) * SPLIT_COOLDOWN_FRAMES
        dist_to_us = math.hypot(enemy.pos[0] - me_x, enemy.pos[1] - me_y)
        dist_to_landing = math.hypot(enemy.pos[0] - landing_x, enemy.pos[1] - landing_y)
        if min(dist_to_us, dist_to_landing) < (danger_reach + enemy.radius):
            return False
    return True


def _should_split_hunt(me, my_strongest_mass: float, target, enemies, viruses) -> bool:
    """Lunge-split toward a target that's in range but not yet reachable this
    tick, only if we haven't already split and the kill would be safe."""
    if len(me.blobs) != 1:
        return False
    if my_strongest_mass < SPLIT_MIN_MASS:
        return False
    distance = math.hypot(target.pos[0] - me.x, target.pos[1] - me.y)
    already_in_reach = distance <= me.radius * SPLIT_MIN_RANGE_FACTOR
    if already_in_reach or distance > SPLIT_LUNGE_RANGE:
        return False
    piece_mass = my_strongest_mass / 2.0
    return _split_kill_is_safe(me.x, me.y, piece_mass, len(me.blobs), target, enemies, viruses)


def _estimate_chase_rounds(distance: float, my_strongest_radius: float) -> float:
    speed = _movement_speed(my_strongest_radius)
    if speed <= 0:
        return float("inf")
    return distance / speed


def _path_crosses_big_blob_cluster(my_x: float, my_y: float, target_x: float, target_y: float, enemies, my_strongest_mass: float) -> bool:
    """Even if the target itself is small and safe, a path that threads
    through a cluster of big blobs on the way there isn't worth the risk."""
    seg_dx = target_x - my_x
    seg_dy = target_y - my_y
    seg_len_sq = seg_dx * seg_dx + seg_dy * seg_dy
    if seg_len_sq == 0:
        return False
    big_blob_threshold = my_strongest_mass * BIG_BLOB_MASS_RATIO
    count = 0
    for enemy in enemies:
        if _mass(enemy.radius) < big_blob_threshold:
            continue
        t = ((enemy.pos[0] - my_x) * seg_dx + (enemy.pos[1] - my_y) * seg_dy) / seg_len_sq
        t = min(max(t, 0.0), 1.0)
        closest_x = my_x + t * seg_dx
        closest_y = my_y + t * seg_dy
        dist_to_path = math.hypot(enemy.pos[0] - closest_x, enemy.pos[1] - closest_y)
        if dist_to_path <= CHASE_CORRIDOR_WIDTH + enemy.radius:
            count += 1
            if count >= DENSE_BIG_BLOB_COUNT:
                return True
    return False


def _is_chase_worth_it(my_x: float, my_y: float, target, enemies, my_strongest_radius: float, my_strongest_mass: float) -> bool:
    distance = math.hypot(target.pos[0] - my_x, target.pos[1] - my_y)
    if _estimate_chase_rounds(distance, my_strongest_radius) > MAX_CHASE_ROUNDS:
        return False
    if _path_crosses_big_blob_cluster(my_x, my_y, target.pos[0], target.pos[1], enemies, my_strongest_mass):
        return False
    return True


class HuntTracker:
    """Cross-tick memory for the hunt: which target we're currently locked
    onto, and a cooldown on targets we've given up on."""

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


def choose_direction(game: Game, tracker: HuntTracker) -> tuple[float, float, bool]:
    me = game.state.me
    weakest_mass, strongest_mass = _weakest_and_strongest_mass(me)
    size = game.state.map.size
    round_ = game.state.round
    # visible_blobs includes our own blobs, so filter those out first.
    enemies = [blob for blob in game.state.visible_blobs if blob.player_id != me.player_id]

    # Priority 1: flee anyone who could eat our weakest blob, and steer away
    # from any virus that would actually expose us once split. Stage 3h adds
    # a proactive wall/corner field on top so we curve away well before
    # actually touching a wall.
    threats = [blob for blob in enemies if _mass(blob.radius) > weakest_mass * EAT_SIZE_RATIO]
    virus_positions = _dangerous_virus_positions(
        me.x, me.y, me.radius, strongest_mass, len(me.blobs), game.state.visible_viruses, enemies
    )
    if threats or virus_positions:
        danger_positions = [blob.pos for blob in threats] + virus_positions
        flee_x, flee_y = _repulsion_vector(me.x, me.y, danger_positions)
        wall_x, wall_y = _wall_repulsion_vector(me.x, me.y, size)
        corner_x, corner_y = _corner_repulsion_vector(me.x, me.y, size)
        flee_x += wall_x + corner_x
        flee_y += wall_y + corner_y
        dx, dy = _slide_off_walls(flee_x, flee_y, me.x, me.y, size)
        return (dx, dy, False)

    my_strongest_radius = math.sqrt(strongest_mass)

    # Priority 2: an isolated enemy fragment is close to a free kill --
    # snipe it before normal hunting, if the chase is actually worth it.
    fragment_target = _find_fragment_opportunity(me, strongest_mass, enemies, size, my_strongest_radius)
    if fragment_target is not None and _is_chase_worth_it(
        me.x, me.y, fragment_target, enemies, my_strongest_radius, strongest_mass
    ):
        target_x, target_y = _predicted_prey_position(me.x, me.y, fragment_target, size)
        should_split = _should_split_hunt(me, strongest_mass, fragment_target, enemies, game.state.visible_viruses)
        return (target_x - me.x, target_y - me.y, should_split)

    # Priority 3: chase a locked-on, safe-to-eat, geometrically catchable
    # prey -- give up early on chases that are too slow or too risky.
    prey_blobs = [
        blob
        for blob in enemies
        if strongest_mass * MIN_HUNT_MASS_RATIO <= _mass(blob.radius) < strongest_mass * HUNT_MASS_RATIO
    ]
    target = _select_prey(tracker, prey_blobs, me, round_)
    if target is not None:
        if _is_chase_worth_it(me.x, me.y, target, enemies, my_strongest_radius, strongest_mass):
            target_x, target_y = _predicted_prey_position(me.x, me.y, target, size)
            should_split = _should_split_hunt(me, strongest_mass, target, enemies, game.state.visible_viruses)
            return (target_x - me.x, target_y - me.y, should_split)
        tracker.give_up(target.blob_id, round_)

    # Priority 4: nothing worth fighting nearby, fall back to food.
    if game.state.visible_food:
        food_target = min(
            game.state.visible_food,
            key=lambda food: (food.pos[0] - me.x) ** 2 + (food.pos[1] - me.y) ** 2,
        )
        return (food_target.pos[0] - me.x, food_target.pos[1] - me.y, False)
    return (1.0, 0.0, False)


def main() -> None:
    game = Game()
    tracker = HuntTracker()

    while True:
        query = game.get_next_query()
        match query:
            case QueryMovePlayer():
                dx, dy, should_split = choose_direction(game, tracker)
                game.send_move(
                    MovePlayer(
                        player_id=game.state.me.player_id,
                        direction=DirectionModel(x=dx, y=dy),
                        split=should_split,
                    )
                )
            case _:
                raise RuntimeError(f"Unsupported query type: {type(query)}")


if __name__ == "__main__":
    main()
