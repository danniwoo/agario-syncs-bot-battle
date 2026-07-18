# v3 builds on my_bot_v2_7.py (Stage 1 - 3o) and attacks the late-game mass
# ceiling head-on. The diagnosis (all three are math, not vibes):
#
#   1. Food economics cap out around mass 3-5: break-even needs mass/11.25
#      pellets per tick (pellet = 0.0225 mass, decay = 0.002*mass/tick), and
#      pellet spacing/travel time makes that unreachable past mass ~5. Our
#      leaderboard average (~3.6) sat exactly at the food-only equilibrium.
#   2. The split-lunge trigger window (radius*1.15, 5.0] became EMPTY at
#      radius >= 4.35 (mass ~19), silently disabling our only burst weapon
#      right when walking speed (0.79 at r=5 vs prey ~1.0) makes ordinary
#      chases hopeless. -> Stage 4a scales the window with radius.
#   3. Viruses are +2.25 mass each (100 pellets), 6 on the map, respawn on
#      consumption -- the densest renewable income in the game, and we only
#      ever treated them as hazards. The top leaderboard bot visibly "walks
#      into" viruses on purpose: at its size the 16 scatter pieces are too
#      big for anyone to punish, so it's free income. -> Stage 4b feeds on
#      viruses when we're big enough and nobody visible can punish it.
#
# Also, decay half-life is ~347 ticks, so final mass mostly reflects income
# in the last few hundred rounds -- sustained late-game income (kills +
# viruses) is what the final number measures, not mid-game peak.
#
# --- Inherited from v2.7 (Stage 1 - 3o) ---
# Merged the 4 isolated features (out of 8 ported from v5.py/v5_1.py and
# tested individually vs v2.6, 4-5 headless runs each) that showed a clear
# improvement head-to-head:
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
#
# Post-merge bug fixes (found by watching replays, not in the isolation tests):
#   Stage 3l - Food corner-deadlock filter. _is_corner_deadlocked assumes the
#     target actively retreats to the worst-case corner position, which is
#     right for fleeing prey/fragments but wrong for stationary food -- food
#     just sits at its actual spawn position. Reusing the prey version
#     over-excluded a lot of genuinely reachable food near walls (regression:
#     v2.6 started winning again). Replaced with _is_food_unreachable, which
#     checks food's real position against our own wall-clamp gap instead of
#     assuming a worst case.
#   Stage 3m - Mass-weighted threat/virus repulsion. _repulsion_vector only
#     weighted by 1/distance, but the Stage 3h wall/corner field has a flat
#     strength (5-9) regardless of danger -- a real threat 5 units away only
#     contributed ~0.2, roughly 10x weaker than a nearby wall's push, so the
#     flee vector could end up dominated by "away from wall" while barely
#     responding to an actual predator closing in (observed directly: bot
#     kept foraging or ran a wrong-looking direction while being chased).
#     Switched to mass/distance (closer AND bigger things push harder),
#     matching the scale the wall constants were effectively tuned against
#     in the source lineage.
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
# something that might out-eat us if it grows before we catch it. A prey too
# small to ever be caught in a corner (my_bot_v2_6.py Stage 3g) is excluded
# too, but per-target by actual position (_is_corner_deadlocked) rather than
# a blanket ratio -- see Stage 3o below for why that distinction matters.
HUNT_MASS_RATIO = 0.7
# Blobs get clamped at the wall rather than hurt by it; the real danger is being
# cornered. This is now mostly a last-resort safety net -- the Stage 3h active
# field below handles the normal case.
BOUNDARY_MARGIN = 5.0
# Stage 3n: fixed-magnitude, position-signed nudge added along the wall
# whenever a component gets zeroed above, so the slide direction is never
# left to a near-zero/noisy remainder that could flip sign tick to tick.
WALL_SLIDE_BIAS = 1.0
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

# --- Stage 3l: food corner-deadlock filter ---

# --- Stage 3j: split-lunge attack ---
SPLIT_MIN_MASS = 2.0
# Stage 4a: the lunge trigger window used to be (radius*1.15, 5.0] -- an
# EMPTY interval once radius >= 4.35 (mass ~19), which silently disabled our
# only burst weapon exactly when base speed (0.79 at r=5 vs prey's ~1.0)
# made walking chases hopeless. True kill reach is the child-spawn offset
# (~sqrt(2)*r ahead of our centre, see engine _apply_split geometry) plus
# the eject glide (1.6/(1-0.82) ~= 8.9); scale the trigger with radius and
# keep a conservative slice of the glide.
SPLIT_SPAWN_REACH = 1.41421356
SPLIT_LUNGE_EXTRA = 5.0
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

# --- Stage 4b: virus feeding ---
# A virus is +virus.mass (2.25 = 100 food pellets) of renewable income; the
# only cost is being scattered into up to 16 pieces that re-merge on their
# own. Food income mathematically cannot outpace decay past mass ~5
# (break-even needs mass/11.25 pellets per tick), so past this size the only
# sustainable income sources are kills and viruses. Only feed when big
# enough that the post-split pieces aren't eatable by anyone visible.
VIRUS_FEED_MIN_MASS = 16.0


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

    Stage 3n: zeroing alone isn't enough when the threat pushing us is nearly
    perpendicular to the wall -- the surviving tangential (along-wall)
    component can be tiny and flip sign tick to tick as the threat's exact
    position shifts, causing the same left-right dithering while making no
    real progress (observed directly: pinned against a wall with a threat
    almost dead ahead, jittering in place until eaten). Whenever an axis gets
    zeroed, add a small bias along the other axis toward the map centre --
    its sign only depends on our own (smoothly-changing) position, not the
    threat's, so it can't flip frame to frame the way the natural remainder
    can, guaranteeing a stable escape heading even when the real signal is
    near zero. It's small enough not to override a genuinely strong
    tangential component that's already there.
    """
    center = size / 2.0
    if x < BOUNDARY_MARGIN and dx < 0:
        dx = 0.0
        dy += WALL_SLIDE_BIAS if y < center else -WALL_SLIDE_BIAS
    elif x > size - BOUNDARY_MARGIN and dx > 0:
        dx = 0.0
        dy += WALL_SLIDE_BIAS if y < center else -WALL_SLIDE_BIAS
    if y < BOUNDARY_MARGIN and dy < 0:
        dy = 0.0
        dx += WALL_SLIDE_BIAS if x < center else -WALL_SLIDE_BIAS
    elif y > size - BOUNDARY_MARGIN and dy > 0:
        dy = 0.0
        dx += WALL_SLIDE_BIAS if x < center else -WALL_SLIDE_BIAS

    if dx == 0.0 and dy == 0.0:
        return (center - x, center - y)
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


def _repulsion_vector(me_x: float, me_y: float, danger_items) -> tuple[float, float]:
    """Sum a weighted repulsion vector away from every (pos, mass) danger
    item (closer AND bigger things push harder), instead of reacting to only
    distance -- avoids the discrete-flip jitter bug, and stops a huge nearby
    predator being under-weighted relative to the flat-strength wall/corner
    field below (weighting by distance alone made a threat's push roughly
    10x weaker than a nearby wall's, so the flee vector could end up pointing
    mostly away from the wall and barely away from the actual threat)."""
    push_x, push_y = 0.0, 0.0
    for (pos_x, pos_y), mass in danger_items:
        away_x, away_y = me_x - pos_x, me_y - pos_y
        distance = math.hypot(away_x, away_y)
        if distance == 0:
            continue
        weight = mass / distance
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


def _dangerous_viruses(
    me_x: float, me_y: float, my_radius: float, my_strongest_mass: float, my_blob_count: int, viruses, enemies
):
    """Viruses actually worth detouring around right now -- only if a
    visible enemy is big enough to eat the resulting fragments. Returns the
    virus objects (not just positions) so callers can weight the repulsion
    by the virus's real mass."""
    danger_radius = my_radius + VIRUS_AVOIDANCE_BUFFER
    dangerous = []
    for virus in viruses:
        if my_strongest_mass <= _mass(virus.radius) * EAT_SIZE_RATIO:
            continue
        distance = math.hypot(me_x - virus.pos[0], me_y - virus.pos[1])
        if not (0 < distance < danger_radius):
            continue
        piece_mass = _predicted_piece_mass(my_strongest_mass, my_blob_count, virus.radius)
        exploitable = any(_mass(enemy.radius) > piece_mass * EAT_SIZE_RATIO for enemy in enemies)
        if exploitable:
            dangerous.append(virus)
    return dangerous


def _find_feedable_virus(me, my_strongest_mass: float, my_blob_count: int, viruses, enemies):
    """Stage 4b: the nearest virus we can profitably eat right now.

    Mirrors _dangerous_viruses exactly inverted: we must be big enough to
    consume it, and NO visible enemy may be big enough to eat the pieces the
    split would scatter us into. Same check both ways keeps the two
    behaviours consistent -- a virus is either a hazard, food, or neutral,
    never both."""
    if my_strongest_mass < VIRUS_FEED_MIN_MASS:
        return None
    best = None
    best_dist = float("inf")
    for virus in viruses:
        if my_strongest_mass <= _mass(virus.radius) * EAT_SIZE_RATIO:
            continue
        piece_mass = _predicted_piece_mass(my_strongest_mass, my_blob_count, virus.radius)
        if any(_mass(enemy.radius) > piece_mass * EAT_SIZE_RATIO for enemy in enemies):
            continue
        distance = math.hypot(me.x - virus.pos[0], me.y - virus.pos[1])
        if distance < best_dist:
            best_dist = distance
            best = virus
    return best


def _is_corner_pinned(pos: tuple[float, float], radius: float, size: float) -> bool:
    near_x_wall = pos[0] <= radius + CORNER_PIN_MARGIN or pos[0] >= size - radius - CORNER_PIN_MARGIN
    near_y_wall = pos[1] <= radius + CORNER_PIN_MARGIN or pos[1] >= size - radius - CORNER_PIN_MARGIN
    return near_x_wall and near_y_wall


def _is_corner_deadlocked(target_pos: tuple[float, float], target_radius: float, my_radius: float, size: float) -> bool:
    """Geometric deadlock check (see my_bot_v2_6.py Stage 3g) for a target
    that actively retreats to the worst-case corner position to save itself
    (fleeing prey, fragments): if our own radius is too far above the
    target's for our clamped centre to ever reach within eating range of a
    corner-pinned target's centre, it's unreachable no matter how long we
    chase. NOT suitable for stationary food -- see _is_food_unreachable,
    which checks food's actual position instead of assuming worst-case.
    """
    if my_radius <= target_radius:
        return False
    if not _is_corner_pinned(target_pos, target_radius, size):
        return False
    limit_distance = SQRT_2 * (my_radius - target_radius)
    return limit_distance > my_radius


def _is_food_unreachable(food_pos: tuple[float, float], my_radius: float, size: float) -> bool:
    """Unlike fleeing prey/fragments, food doesn't retreat to the worst-case
    corner -- it just sits wherever it spawned. Our own centre can get to
    (clamp(food.x, my_radius, size - my_radius), clamp(food.y, ...)), so the
    only gap that matters is how far food's *actual* position sits inside the
    margin our own radius can't reach past. Zero on either axis unless food
    is closer to that wall than our own radius allows."""
    gap_x = max(0.0, my_radius - food_pos[0], food_pos[0] - (size - my_radius))
    gap_y = max(0.0, my_radius - food_pos[1], food_pos[1] - (size - my_radius))
    return gap_x * gap_x + gap_y * gap_y > my_radius * my_radius


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
            if _is_corner_deadlocked(frag.pos, frag.radius, my_radius, size):
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
    lunge_range = me.radius * SPLIT_SPAWN_REACH + SPLIT_LUNGE_EXTRA
    if already_in_reach or distance > lunge_range:
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

    # Priority 1 [Stage 3m]: flee anyone who could eat our weakest blob, and
    # steer away from any virus that would actually expose us once split --
    # weighted by mass (Stage 3m) so a genuinely huge threat properly
    # dominates the flat-strength wall/corner field (Stage 3h) added below,
    # instead of a nearby wall drowning out the actual danger signal.
    threats = [blob for blob in enemies if _mass(blob.radius) > weakest_mass * EAT_SIZE_RATIO]
    dangerous_viruses = _dangerous_viruses(
        me.x, me.y, me.radius, strongest_mass, len(me.blobs), game.state.visible_viruses, enemies
    )
    if threats or dangerous_viruses:
        danger_items = [(blob.pos, _mass(blob.radius)) for blob in threats] + [
            (virus.pos, _mass(virus.radius)) for virus in dangerous_viruses
        ]
        flee_x, flee_y = _repulsion_vector(me.x, me.y, danger_items)
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

    # Priority 3 [Stage 3o]: chase a locked-on, safe-to-eat, geometrically
    # catchable prey -- give up early on chases that are too slow or too
    # risky. "Catchable" is checked per-target's actual position
    # (_is_corner_deadlocked), not a blanket mass-ratio floor: the old
    # MIN_HUNT_MASS_RATIO cutoff grows with our own size and started
    # wrongly excluding small prey sitting in the open, well within reach,
    # just because they'd be uncatchable *if* they were cornered (which they
    # weren't) -- observed directly as a huge blob ignoring an easy nearby kill.
    prey_blobs = [
        blob
        for blob in enemies
        if _mass(blob.radius) < strongest_mass * HUNT_MASS_RATIO
        and not _is_corner_deadlocked(blob.pos, blob.radius, my_strongest_radius, size)
    ]
    target = _select_prey(tracker, prey_blobs, me, round_)
    if target is not None:
        if _is_chase_worth_it(me.x, me.y, target, enemies, my_strongest_radius, strongest_mass):
            target_x, target_y = _predicted_prey_position(me.x, me.y, target, size)
            should_split = _should_split_hunt(me, strongest_mass, target, enemies, game.state.visible_viruses)
            return (target_x - me.x, target_y - me.y, should_split)
        tracker.give_up(target.blob_id, round_)

    # Priority 4 [Stage 4b]: no prey worth chasing -- feed on a virus if
    # we're big enough that food can no longer outpace decay (a virus is
    # worth ~100 pellets) and nobody visible can punish the split.
    feed_virus = _find_feedable_virus(me, strongest_mass, len(me.blobs), game.state.visible_viruses, enemies)
    if feed_virus is not None:
        return (feed_virus.pos[0] - me.x, feed_virus.pos[1] - me.y, False)

    # Priority 5 [Stage 3l]: nothing worth fighting nearby, fall back to food
    # -- skipping any pellet sitting in a corner we're too big to ever reach
    # (same geometry as _is_corner_deadlocked above; without this we could
    # beeline for a corner pellet forever and never actually get it).
    reachable_food = [
        food for food in game.state.visible_food if not _is_food_unreachable(food.pos, my_strongest_radius, size)
    ]
    if reachable_food:
        food_target = min(
            reachable_food,
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
