"""Adaptive Strategist Bot for Terminal Velocity.

Picks between strategies dynamically based on game phase, leaderboard
position, HP, cargo, and what the radar sees.

Strategies
----------
MINE    – engines=3, greedy asteroid farming (default)
ATTACK  – engines=1 shields=0 lasers=2, hunt nearby rich enemies
DELIVER – engines=3 (or whatever is set), emergency cargo run

Strategy-selection highlights
------------------------------
- MINE is the default for the whole game.  It matches the best pure-mining
  bot in efficiency (engines=3, speed up to 3).
- We commit to ATTACK for at least ATTACK_COMMIT_TURNS turns before switching
  back, to amortise the one-turn cost of the power-config change.
- We enter ATTACK only when:
    • an enemy is visible outside the home base, AND
    • the game is past 40% of turns, AND
    • we are not strictly leading (gap ≤ 0), AND
    • we have no cargo (avoids getting stuck at speed 0).
- DELIVER overrides everything when HP ≤ 2 with cargo or cargo is full.
- Power config only changes when the strategy actually changes (sticky power).

Self-rank estimation
--------------------
The turn() API gives no "your name" parameter.  We track our own credit
balance manually: +100 × delivered asteroids, ×0.9 per death.  We compare
this estimate to the leader-board to detect whether we are strictly ahead.

Turn-time budget
----------------
All inner loops are O(k) where k is the number of radar contacts (≤ ~30).
The bot returns well within any reasonable per-turn time limit.
"""

import math
import random

from tv.game import (
    ASTEROID,
    ENGINES,
    FLY_TO,
    HOME_BASE_RADIUS,
    LASERS,
    MAX_CARGO,
    MINING_REWARD,
    POWER_TO,
    SHIELDS,
    SPACESHIP,
    Position,
)

# ─── Strategies ───────────────────────────────────────────────────────────────
MINE = "mine"
ATTACK = "attack"
DELIVER = "deliver"

# ─── Power presets ────────────────────────────────────────────────────────────
POWER_MINE = {ENGINES: 3, SHIELDS: 0, LASERS: 0}      # speed 3/2/1 with 0/1/2 cargo
POWER_ASSAULT = {ENGINES: 1, SHIELDS: 0, LASERS: 2}   # speed 1, 2 dmg (no cargo!)

HOME = Position(0, 0)

# Minimum turns we stay in ATTACK mode once committed (amortises power change).
ATTACK_COMMIT_TURNS = 5


class BotLogic:
    # ─── Lifecycle ────────────────────────────────────────────────────────────

    def __init__(self):
        self.icon = "()"

        self.map_radius: int = 12
        self.total_turns: int = 100
        self.home_base_positions: set = set()

        # Self credit tracking
        self.estimated_credits: int = 0
        self._prev_cargo: int = 0
        self._prev_ship_number: int = 1

        # Map memory
        self.known_asteroids: set = set()   # positions we have seen

        # Exploration state
        self._explore_target: "Position | None" = None
        self._explore_set_turn: int = -999

        # Strategy / power state
        self.strategy: str = MINE
        self._attack_committed_until: int = -1   # turn until which we stay in ATTACK
        self._current_power: dict = POWER_MINE.copy()

    def initialize(self, map_radius, players, turns, home_base_positions):
        self.map_radius = map_radius
        self.total_turns = turns
        self.home_base_positions = home_base_positions

    # ─── Credit / rank tracking ───────────────────────────────────────────────

    def _update_credits(self, cargo: int, ship_number: int) -> None:
        if ship_number > self._prev_ship_number:
            for _ in range(ship_number - self._prev_ship_number):
                self.estimated_credits = int(self.estimated_credits * 0.9)
            self._prev_ship_number = ship_number

        if ship_number == self._prev_ship_number and cargo < self._prev_cargo:
            self.estimated_credits += (self._prev_cargo - cargo) * MINING_REWARD

        self._prev_cargo = cargo

    def _credit_gap(self, leader_board: dict) -> int:
        """Our credits minus the leader's credits.  Negative ⟹ we are behind."""
        if not leader_board:
            return 0
        return self.estimated_credits - max(leader_board.values())

    def _num_richer_than_us(self, leader_board: dict) -> int:
        """How many leaderboard entries exceed our estimated credits."""
        return sum(1 for v in leader_board.values() if v > self.estimated_credits)

    # ─── Geometry helpers ─────────────────────────────────────────────────────

    @staticmethod
    def _at_base(pos: "Position") -> bool:
        return HOME.distance_to(pos) <= HOME_BASE_RADIUS

    @staticmethod
    def _effective_speed(power_distribution: dict, cargo: int) -> int:
        return max(power_distribution[ENGINES] - cargo, 0)

    @staticmethod
    def _reachable(position: "Position", speed: int) -> list:
        if speed == 0:
            return []
        return list(position.positions_in_range(speed))

    def _step_toward(self, position: "Position", target: "Position",
                     speed: int) -> "Position | None":
        candidates = self._reachable(position, speed)
        if not candidates:
            return None
        return min(candidates, key=lambda p: p.distance_to(target))

    def _step_away(self, position: "Position", threats: list,
                   speed: int) -> "Position | None":
        candidates = self._reachable(position, speed)
        if not candidates:
            return None

        def score(p: "Position") -> float:
            min_td = min(p.distance_to(t) for t in threats) if threats else 999.0
            # Small bias toward home base for safety
            return min_td - 0.2 * p.distance_to(HOME)

        return max(candidates, key=score)

    def _nearest_asteroid(self, position: "Position",
                          radar_contacts: dict) -> "Position | None":
        rocks = [p for p, t in radar_contacts.items() if t == ASTEROID]
        if not rocks:
            return None
        return min(rocks, key=lambda a: position.distance_to(a))

    def _best_asteroid_on_way_home(self, position: "Position",
                                   radar_contacts: dict) -> "Position | None":
        """
        When carrying 1 asteroid and heading home, check if any visible asteroid
        is close enough that a detour is profitable.

        The extra distance of the detour is:
            d(pos→ast) + d(ast→home) − d(pos→home)

        This is worthwhile when extra_distance / speed < 100 / mining_rate,
        but in practice we use a simple threshold: detour is worth it if it
        adds fewer than 10 tiles of travel (generous, since 100 creds is a lot).
        """
        rocks = [p for p, t in radar_contacts.items() if t == ASTEROID]
        if not rocks:
            return None

        d_home = position.distance_to(HOME)
        best_rock = None
        best_extra = float("inf")

        for rock in rocks:
            extra = position.distance_to(rock) + rock.distance_to(HOME) - d_home
            if extra < 10 and extra < best_extra:
                best_extra = extra
                best_rock = rock

        return best_rock

    def _nearest_outside_enemy(self, position: "Position",
                                radar_contacts: dict) -> "Position | None":
        ships = [
            p for p, t in radar_contacts.items()
            if t == SPACESHIP and not self._at_base(p)
        ]
        if not ships:
            return None
        return min(ships, key=lambda s: position.distance_to(s))

    def _explore_step(self, position: "Position", turn_number: int,
                      speed: int) -> "Position | None":
        """
        Move toward a remembered or freshly chosen exploration target.
        Refreshes the target when:
          – we are within (speed+1) tiles of the current target, or
          – the target is stale (> 15 turns old).
        Prefers remembered asteroid positions over random map quadrants.
        """
        if speed == 0:
            return None

        need_new = (
            self._explore_target is None
            or turn_number - self._explore_set_turn > 15
            or position.distance_to(self._explore_target) <= speed + 1
        )

        if need_new:
            if self.known_asteroids:
                # Head toward the nearest known (but not currently visible) asteroid
                self._explore_target = min(
                    self.known_asteroids,
                    key=lambda a: position.distance_to(a),
                )
            else:
                # Pick a point at ~75% of map radius in a random direction
                angle = random.uniform(0, 2 * math.pi)
                r = self.map_radius * 0.75
                tx = max(-self.map_radius, min(self.map_radius,
                                               int(round(r * math.cos(angle)))))
                ty = max(-self.map_radius, min(self.map_radius,
                                               int(round(r * math.sin(angle)))))
                self._explore_target = Position(tx, ty)
            self._explore_set_turn = turn_number

        return self._step_toward(position, self._explore_target, speed)

    # ─── Strategy selection ───────────────────────────────────────────────────

    def _choose_strategy(self, turn_number: int, hp: int, cargo: int,
                         position: "Position", radar_contacts: dict,
                         leader_board: dict) -> str:
        phase = turn_number / max(self.total_turns, 1)
        gap = self._credit_gap(leader_board)

        enemies_outside = [
            p for p, t in radar_contacts.items()
            if t == SPACESHIP and not self._at_base(p)
        ]

        # ── Critical overrides ────────────────────────────────────────────────
        if hp <= 1:
            return DELIVER                          # Never die with cargo
        if cargo >= MAX_CARGO:
            return DELIVER                          # Full hold → cash in now
        if hp <= 2 and cargo > 0:
            return DELIVER                          # Don't risk losing 10%

        # ── Honour ATTACK commitment ──────────────────────────────────────────
        if (self.strategy == ATTACK
                and turn_number < self._attack_committed_until
                and enemies_outside
                and cargo == 0):
            return ATTACK

        # ── Decide whether to enter ATTACK ────────────────────────────────────
        # Conditions for attack:
        #   1. Game is past 40% of turns (enough credits on the board to steal)
        #   2. We are not strictly leading (there are richer players to rob)
        #   3. An enemy is visible outside base
        #   4. We have no cargo (POWER_ASSAULT with cargo=1 → speed=0, stuck!)
        if (phase >= 0.40
                and gap <= 0
                and enemies_outside
                and cargo == 0):
            # Only attack if the closest enemy is within a reasonable chase range
            nearest_e = min(enemies_outside,
                            key=lambda s: position.distance_to(s))
            if position.distance_to(nearest_e) <= 5:
                self._attack_committed_until = turn_number + ATTACK_COMMIT_TURNS
                return ATTACK

        return MINE

    # ─── Main turn ────────────────────────────────────────────────────────────

    def turn(self, turn_number, hp, ship_number, cargo, position,
             power_distribution, radar_contacts, leader_board):

        # ── Update internal state ─────────────────────────────────────────────
        self._update_credits(cargo, ship_number)

        for pos, thing in radar_contacts.items():
            if thing == ASTEROID:
                self.known_asteroids.add(pos)
        self.known_asteroids.discard(position)   # We just grabbed it

        # ── Choose strategy ───────────────────────────────────────────────────
        strategy = self._choose_strategy(
            turn_number, hp, cargo, position, radar_contacts, leader_board
        )
        self.strategy = strategy

        self.icon = {MINE: "()", ATTACK: "><", DELIVER: "=>"}.get(strategy, "::")

        # ── Desired power config ──────────────────────────────────────────────
        desired_power = POWER_ASSAULT if strategy == ATTACK else POWER_MINE

        # Change power only when strictly necessary, and NOT in an urgent DELIVER
        urgent_deliver = strategy == DELIVER and cargo > 0 and hp <= 2
        if power_distribution != desired_power and not urgent_deliver:
            return POWER_TO, desired_power

        # ── Movement ─────────────────────────────────────────────────────────
        speed = self._effective_speed(power_distribution, cargo)

        enemies_outside = [
            p for p, t in radar_contacts.items()
            if t == SPACESHIP and not self._at_base(p)
        ]

        # ── DELIVER ───────────────────────────────────────────────────────────
        if strategy == DELIVER:
            dest = self._step_toward(position, HOME, speed)
            return (FLY_TO, dest) if dest else None

        # ── MINE ──────────────────────────────────────────────────────────────
        if strategy == MINE:
            if cargo > 0:
                # Try to fill the hold before going home
                if cargo < MAX_CARGO:
                    detour_rock = self._best_asteroid_on_way_home(
                        position, radar_contacts
                    )
                    if detour_rock is not None:
                        dest = self._step_toward(position, detour_rock, speed)
                        if dest:
                            return FLY_TO, dest

                # Head home to deliver
                dest = self._step_toward(position, HOME, speed)
                return (FLY_TO, dest) if dest else None

            # No cargo: look for the nearest asteroid
            asteroid = self._nearest_asteroid(position, radar_contacts)
            if asteroid is not None:
                dest = self._step_toward(position, asteroid, speed)
                if dest:
                    return FLY_TO, dest

            # Nothing visible – explore
            dest = self._explore_step(position, turn_number, speed)
            return (FLY_TO, dest) if dest else None

        # ── ATTACK ────────────────────────────────────────────────────────────
        if strategy == ATTACK:
            enemy = self._nearest_outside_enemy(position, radar_contacts)
            if enemy is not None:
                dest = self._step_toward(position, enemy, speed)
                return (FLY_TO, dest) if dest else None

            # Lost the target – fall back to exploration
            dest = self._explore_step(position, turn_number, speed)
            return (FLY_TO, dest) if dest else None

        return None
