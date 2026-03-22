import math
import random

from tv.game import (
    Position, FLY_TO, POWER_TO,
    ENGINES, SHIELDS, LASERS,
    ASTEROID, SPACESHIP, HOME_BASE,
    MAX_CARGO, MAX_HP, MINING_REWARD, PIRATING_REWARD,
    HOME_BASE_RADIUS, RADAR_RADIUS, ATTACK_RADIUS,
)

# Strategy identifiers
MINE_FAST = "mine_fast"
MINE_SAFE = "mine_safe"
PIRATE = "pirate"
FLEE = "flee"
ENDGAME_HOLD = "endgame_hold"

POWER_PROFILES = {
    MINE_FAST:    {ENGINES: 3, SHIELDS: 0, LASERS: 0},
    MINE_SAFE:    {ENGINES: 2, SHIELDS: 1, LASERS: 0},
    PIRATE:       {ENGINES: 1, SHIELDS: 0, LASERS: 2},
    FLEE:         {ENGINES: 2, SHIELDS: 1, LASERS: 0},
    ENDGAME_HOLD: {ENGINES: 0, SHIELDS: 3, LASERS: 0},
}

STRATEGY_ICONS = {
    MINE_FAST: "()",
    MINE_SAFE: "[]",
    PIRATE:    "><",
    FLEE:      "<<",
    ENDGAME_HOLD: "**",
}

HOME = Position(0, 0)


class BotLogic:
    def initialize(self, map_radius, players, turns, home_base_positions):
        self.map_radius = map_radius
        self.players = players
        self.num_players = len(players)
        self.turns = turns
        self.home_base_positions = home_base_positions
        self.strategy = MINE_FAST
        self.icon = "()"

        # Persistent state
        self.my_name = None
        self.known_asteroid_positions = set()
        self.last_ship_number = 1
        self.explore_target = None
        self.explore_index = 0
        self.last_position = None
        self.stuck_turns = 0
        self.my_last_credits = 0
        self.pending_power_switch = None

        # Pre-compute exploration waypoints in a good spread pattern
        self._compute_explore_waypoints()

    def _compute_explore_waypoints(self):
        """Create a set of waypoints that cover the map efficiently."""
        r = self.map_radius
        self.waypoints = []
        # Ring at ~60% radius, 8 points
        for i in range(8):
            angle = i * math.pi / 4 + math.pi / 8  # offset for variety
            x = int(r * 0.6 * math.cos(angle))
            y = int(r * 0.6 * math.sin(angle))
            self.waypoints.append(Position(x, y))
        # Ring at ~90% radius, 8 points (interleaved)
        for i in range(8):
            angle = i * math.pi / 4
            x = int(r * 0.9 * math.cos(angle))
            y = int(r * 0.9 * math.sin(angle))
            self.waypoints.append(Position(x, y))
        random.shuffle(self.waypoints)

    def turn(self, turn_number, hp, ship_number, cargo, position,
             power_distribution, radar_contacts, leader_board):
        # Identify self by tracking credit changes
        if self.my_name is None:
            self.my_name = self._identify_self(leader_board)

        # Detect respawn
        if ship_number > self.last_ship_number:
            self.last_ship_number = ship_number
            self.known_asteroid_positions.clear()
            self.explore_target = None
            self.explore_index = (self.explore_index + 3) % len(self.waypoints)

        # Stuck detection
        if self.last_position == position:
            self.stuck_turns += 1
        else:
            self.stuck_turns = 0
        self.last_position = position

        # Parse radar
        asteroids = []
        enemies = []
        for pos, obj_type in radar_contacts.items():
            if obj_type == ASTEROID:
                asteroids.append(pos)
            elif obj_type == SPACESHIP:
                enemies.append(pos)

        # Update asteroid memory: clear what we can see, add what's there
        self._update_asteroid_memory(position, asteroids)

        # Analyze situation
        game_progress = turn_number / max(self.turns - 1, 1)
        turns_left = self.turns - turn_number

        my_credits = leader_board.get(self.my_name, 0)
        other_credits = sorted(
            [v for k, v in leader_board.items() if k != self.my_name],
            reverse=True
        )
        max_other = other_credits[0] if other_credits else 0

        immediate_threats = [e for e in enemies
                             if position.distance_to(e) <= ATTACK_RADIUS + 0.5]
        nearby_enemies = [e for e in enemies
                          if position.distance_to(e) <= RADAR_RADIUS]
        in_base = position.distance_to(HOME) <= HOME_BASE_RADIUS

        # --- STRATEGY SELECTION ---
        self.strategy = self._choose_strategy(
            game_progress, turns_left, my_credits, max_other,
            hp, cargo, position, asteroids, enemies,
            nearby_enemies, immediate_threats, in_base,
        )
        self.icon = STRATEGY_ICONS[self.strategy]

        # --- POWER MANAGEMENT ---
        desired_power = POWER_PROFILES[self.strategy]
        if power_distribution != desired_power:
            # If we need to switch power AND move, we lose a turn.
            # Decide if the switch is worth it.
            if self._should_switch_power_now(
                    position, cargo, power_distribution, desired_power,
                    asteroids, immediate_threats, in_base):
                return POWER_TO, desired_power
            # Otherwise keep current power and act with what we have
            desired_power = power_distribution

        # --- EXECUTE ---
        speed = max(desired_power[ENGINES] - cargo, 0)
        self.my_last_credits = my_credits

        return self._execute(
            position, cargo, hp, speed, desired_power,
            asteroids, enemies, nearby_enemies, immediate_threats,
            in_base, game_progress, turns_left, my_credits, max_other,
        )

    def _identify_self(self, leader_board):
        # On first turn, all credits are 0. We can't truly distinguish,
        # so just use the first player name. This is used for leaderboard
        # comparisons which still work directionally even if wrong.
        return self.players[0]

    def _update_asteroid_memory(self, position, visible_asteroids):
        """Update asteroid memory based on what we can currently see."""
        # Remove any remembered asteroids within our radar that aren't there
        to_remove = set()
        for remembered in self.known_asteroid_positions:
            if position.distance_to(remembered) <= RADAR_RADIUS:
                if remembered not in visible_asteroids:
                    to_remove.add(remembered)
        self.known_asteroid_positions -= to_remove
        # Add currently visible ones
        self.known_asteroid_positions.update(visible_asteroids)

    def _should_switch_power_now(self, position, cargo, current_power,
                                 desired_power, asteroids, immediate_threats,
                                 in_base):
        """Decide if spending a turn on power switch is worth it."""
        # Always switch if we're in base (safe, and delivery is automatic)
        if in_base:
            return True

        # Always switch to flee if threatened
        if immediate_threats and desired_power[SHIELDS] > current_power[SHIELDS]:
            return True

        # Don't switch if we already have workable speed for mining
        current_speed = max(current_power[ENGINES] - cargo, 0)
        desired_speed = max(desired_power[ENGINES] - cargo, 0)
        if current_speed > 0 and desired_speed == current_speed:
            return False

        # If we can already move at decent speed, don't waste a turn
        if current_speed >= 2 and self.strategy in (MINE_FAST, MINE_SAFE):
            return False

        return True

    def _choose_strategy(self, game_progress, turns_left, my_credits,
                         max_other, hp, cargo, position, asteroids, enemies,
                         nearby_enemies, immediate_threats, in_base):
        # === CRITICAL: Very low HP with threats, flee ===
        if hp <= 2 and immediate_threats and not in_base:
            return FLEE

        # === ENDGAME: Leading comfortably, protect the lead ===
        if game_progress > 0.8 and my_credits > max_other * 1.2 and my_credits > 200:
            if cargo:
                return FLEE  # deliver cargo first
            if in_base:
                return ENDGAME_HOLD
            # If close to base, go hold. If far, mine if there's time
            if position.distance_to(HOME) <= 4:
                return FLEE
            if turns_left > 10:
                return MINE_SAFE
            return FLEE

        # === ENDGAME: Trailing significantly, be aggressive if enemies near ===
        if game_progress > 0.75 and max_other > 0 and my_credits < max_other * 0.5:
            # Pirating is only worth it if opponents are rich
            # EV of kill = opponent_credits * 0.1
            # Need ~5 hits with 2 lasers to kill (5hp, 2dmg/hit with no shields)
            # That takes ~5 turns in range
            if enemies and max_other > 500 and not cargo:
                return PIRATE
            return MINE_FAST  # mine desperately

        # === CARRYING CARGO: Priority is delivery ===
        if cargo >= MAX_CARGO:
            if immediate_threats:
                return MINE_SAFE  # shields help survive the trip
            return MINE_FAST  # speed for delivery

        if cargo and position.distance_to(HOME) <= 4:
            # Close to base with cargo, just deliver fast
            return MINE_FAST

        # === THREATS with no cargo: consider fighting back ===
        if immediate_threats and not cargo and hp >= 4:
            # Only pirate if EV is good: need opponent to be rich
            # With 2 lasers, 0 shields, we deal 2 dmg but take hits
            # Only worth it in the mid-late game when opponents have credits
            if max_other > 400 and game_progress > 0.3:
                return PIRATE

        # === THREATS with cargo: protect it ===
        if nearby_enemies and cargo:
            return MINE_SAFE

        # === DEFAULT: Fast mining is almost always optimal ===
        # Mining gives guaranteed 100 credits per asteroid delivery.
        # A full 2-asteroid trip = 200 credits.
        # Pirating gives 10% of opponent credits per kill, which requires
        # multiple turns of combat. Mining is nearly always better EV.
        return MINE_FAST

    def _execute(self, position, cargo, hp, speed, power, asteroids,
                 enemies, nearby_enemies, immediate_threats, in_base,
                 game_progress, turns_left, my_credits, max_other):

        if self.strategy == ENDGAME_HOLD:
            if not in_base:
                return self._fly_toward(position, HOME, speed)
            return None

        if self.strategy == FLEE:
            return self._fly_toward(position, HOME, speed)

        if self.strategy == PIRATE:
            return self._execute_pirate(position, speed, enemies,
                                        immediate_threats)

        # MINE_FAST or MINE_SAFE
        return self._execute_mine(position, speed, cargo, asteroids,
                                  turns_left)

    def _execute_mine(self, position, speed, cargo, asteroids, turns_left):
        if speed <= 0:
            return None

        # Full cargo or near end of game with cargo -> deliver
        if cargo >= MAX_CARGO:
            return self._fly_toward(position, HOME, speed)

        # If we have cargo and are close enough to deliver and come back,
        # or game is almost over, deliver now
        dist_home = position.distance_to(HOME)
        if cargo:
            if dist_home <= speed:
                # Can reach base this turn
                return self._fly_toward(position, HOME, speed)
            if turns_left <= dist_home / max(speed, 1) + 2:
                # Won't have time if we don't head back now
                return self._fly_toward(position, HOME, speed)

        # Look for asteroids: prefer ones that form efficient round-trips
        target = self._best_mining_target(position, asteroids, cargo, speed)
        if target:
            return self._fly_toward(position, target, speed)

        # Check remembered asteroids
        if self.known_asteroid_positions:
            target = self._best_mining_target(
                position, list(self.known_asteroid_positions), cargo, speed
            )
            if target:
                return self._fly_toward(position, target, speed)

        # If we have any cargo and nothing else to do, deliver
        if cargo:
            return self._fly_toward(position, HOME, speed)

        # Explore for more asteroids
        return self._explore(position, speed)

    def _best_mining_target(self, position, asteroids, cargo, speed):
        """Pick the asteroid that minimizes total round-trip time."""
        if not asteroids:
            return None

        if cargo == 0:
            # Prefer asteroids closer to the middle of the map
            # (shorter delivery trip after pickup)
            def score(a):
                dist_to_asteroid = position.distance_to(a)
                dist_asteroid_to_home = a.distance_to(HOME)
                # Total trip cost: go there + return
                # With cargo, speed drops by 1 per asteroid
                return dist_to_asteroid + dist_asteroid_to_home * 1.3
            return min(asteroids, key=score)
        else:
            # Already have cargo, pick closest asteroid on the way home
            # or just go home if nothing is convenient
            home_dist = position.distance_to(HOME)

            def score(a):
                detour = (position.distance_to(a) + a.distance_to(HOME)
                          - home_dist)
                if detour <= 2:
                    return -100 + position.distance_to(a)  # very attractive
                return detour
            best = min(asteroids, key=score)
            if score(best) < 4:  # reasonable detour
                return best
            return None  # just go home instead

    def _execute_pirate(self, position, speed, enemies, immediate_threats):
        if speed <= 0:
            # With 1 engine and 0 cargo we have speed 1, this shouldn't happen
            # unless we have cargo. Drop strategy next turn.
            return None

        if immediate_threats:
            # Stay in attack range - move to maintain position
            target = min(immediate_threats,
                         key=lambda e: position.distance_to(e))
            best = self._best_position_near(position, target,
                                            ATTACK_RADIUS, speed)
            if best:
                return FLY_TO, best
            return None

        # Chase nearest enemy
        if enemies:
            closest = min(enemies, key=lambda e: position.distance_to(e))
            return self._fly_toward(position, closest, speed)

        # No enemies visible, go to where enemies might be (away from base)
        return self._explore(position, speed)

    def _fly_toward(self, position, target, speed):
        if speed <= 0:
            return None

        reachable = list(position.positions_in_range(speed))
        if not reachable:
            return None

        # Filter valid map positions
        r = self.map_radius
        reachable = [p for p in reachable
                     if -r <= p.x <= r and -r <= p.y <= r]
        if not reachable:
            return None

        best = min(reachable, key=lambda p: p.distance_to(target))
        return FLY_TO, best

    def _best_position_near(self, position, target, desired_dist, speed):
        if speed <= 0:
            return None

        reachable = list(position.positions_in_range(speed))
        r = self.map_radius
        reachable = [p for p in reachable
                     if -r <= p.x <= r and -r <= p.y <= r]
        if not reachable:
            return None

        # Prefer positions within attack range
        in_range = [p for p in reachable
                    if 0 < p.distance_to(target) <= desired_dist]
        if in_range:
            return min(in_range, key=lambda p: p.distance_to(target))
        return min(reachable, key=lambda p: p.distance_to(target))

    def _explore(self, position, speed):
        if speed <= 0:
            return None

        # Reset explore target if we reached it or got stuck
        if self.explore_target:
            if (position.distance_to(self.explore_target) <= 2
                    or self.stuck_turns > 3):
                self.explore_target = None
                self.stuck_turns = 0

        if self.explore_target is None:
            self.explore_index = (self.explore_index + 1) % len(self.waypoints)
            self.explore_target = self.waypoints[self.explore_index]

        return self._fly_toward(position, self.explore_target, speed)
