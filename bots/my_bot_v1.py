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


def _mass(radius: float) -> float:
    return radius * radius


def choose_direction(game: Game) -> tuple[float, float]:
    me = game.state.me
    my_mass = _mass(me.radius)
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
        return (-nearest[0], -nearest[1])

    # Priority 2: no immediate threat, so chase the nearest safe-to-eat prey.
    prey = [
        (blob.pos[0] - me.x, blob.pos[1] - me.y)
        for blob in enemies
        if _mass(blob.radius) < my_mass * HUNT_MASS_RATIO
    ]
    if prey:
        nearest = min(prey, key=lambda pos: pos[0] ** 2 + pos[1] ** 2)
        return nearest

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
