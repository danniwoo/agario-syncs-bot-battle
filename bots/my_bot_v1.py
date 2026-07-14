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


def choose_direction(game: Game) -> tuple[float, float]:
    me = game.state.me
    my_mass = _mass(me.radius)
    size = game.state.map.size
    # visible_blobs includes our own blobs, so filter those out first.
    enemies = [blob for blob in game.state.visible_blobs if blob.player_id != me.player_id]

    # Priority 1: flee anyone who could eat us, regardless of anything else.
    threats = [
        (blob.pos[0] - me.x, blob.pos[1] - me.y)
        for blob in enemies
        if _mass(blob.radius) > my_mass * EAT_SIZE_RATIO
    ]
    if threats:
        nearest = min(threats, key=lambda pos: pos[0] ** 2 + pos[1] ** 2)
        flee_x, flee_y = -nearest[0], -nearest[1]
        push_x, push_y = _boundary_push(me.x, me.y, size)
        return (flee_x + push_x, flee_y + push_y)

    # Priority 2: no immediate threat, so chase the nearest safe-to-eat prey,
    # aiming at its predicted position rather than where it is right now.
    prey_blobs = [
        blob for blob in enemies if _mass(blob.radius) < my_mass * HUNT_MASS_RATIO
    ]
    if prey_blobs:
        nearest_blob = min(
            prey_blobs,
            key=lambda blob: (blob.pos[0] - me.x) ** 2 + (blob.pos[1] - me.y) ** 2,
        )
        target_x, target_y = _predicted_prey_position(me.x, me.y, nearest_blob, size)
        return (target_x - me.x, target_y - me.y)

    # Priority 3: nothing worth fighting nearby, fall back to food.
    if game.state.visible_food:
        target = min(
            game.state.visible_food,
            key=lambda food: (food.pos[0] - me.x) ** 2 + (food.pos[1] - me.y) ** 2,
        )
        return (target.pos[0] - me.x, target.pos[1] - me.y)
    return (1.0, 0.0)


def main() -> None:
    game = Game()

    while True:
        query = game.get_next_query()
        match query:
            case QueryMovePlayer():
                dx, dy = choose_direction(game)
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
