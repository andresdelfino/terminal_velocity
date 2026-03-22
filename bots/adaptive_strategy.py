import math
import random

from tv.game import (
    Position, POWER_TO, FLY_TO, ENGINES, SHIELDS, LASERS,
    ASTEROID, SPACESHIP, HOME_BASE,
    MAX_HP, MAX_CARGO, HOME_BASE_RADIUS, RADAR_RADIUS, ATTACK_RADIUS,
    MINING_REWARD, PIRATING_REWARD,
)

# Strategy identifiers
STRATEGY_SPEED_MINE = "speed_mine"
STRATEGY_ARMORED_MINE = "armored_mine"
STRATEGY_HUNT = "hunt"
STRATEGY_FLEE = "flee"
STRATEGY_AMBUSH = "ambush"
STRATEGY_ENDGAME_SAFE = "endgame_safe"

# Power distributions for each strategy
POWER_CONFIGS = {
    STRATEGY_SPEED_MINE: {ENGINES: 3, SHIELDS: 0, LASERS: 0},
    STRATEGY_ARMORED_MINE: {ENGINES: 2, SHIELDS: 1, LASERS: 0},
    STRATEGY_HUNT: {ENGINES: 1, SHIELDS: 0, LASERS: 2},
    STRATEGY_FLEE: {ENGINES: 3, SHIELDS: 0, LASERS: 0},
    STRATEGY_AMBUSH: {ENGINES: 1, SHIELDS: 1, LASERS: 1},
    STRATEGY_ENDGAME_SAFE: {ENGINES: 2, SHIELDS: 1, LASERS: 0},
}


class BotLogic:
    """
    Adaptive strategy bot that dynamically switches between mining, hunting,
    fleeing, and ambush strategies based on game state analysis.
    """

    def initialize(self, player_name, map_radius, players, turns, home_base_positions):
        self.name = player_name
        self.map_radius = map_radius
        self.players = players
        self.total_turns = turns
        self.home_base_positions = home_base_positions
        self.home = Position(0, 0)
        self.num_enemies = len(players) - 1

        # Tracking state across turns
        self.strategy = STRATEGY_SPEED_MINE
        self.last_position = None
        self.last_hp = MAX_HP
        self.deaths = 0
        self.last_ship_number = 1
        self.explore_target = None
        self.turns_without_asteroid = 0
        self.known_enemy_positions = {}  # name-agnostic: position -> last_seen_turn
        self.enemy_sightings = []  # list of (turn, position) for all enemies
        self.times_attacked = 0  # turns where we took damage
        self.last_enemy_near_base = 0  # last turn we saw enemy near our delivery path
        self.consecutive_mine_turns = 0

        # Exploration: divide map into sectors for systematic search
        self.explored_sectors = set()
        self._generate_explore_targets()

        self.icon = "{}"

    def _generate_explore_targets(self):
        """Generate a list of exploration waypoints covering the map."""
        step = max(RADAR_RADIUS * 2 - 1, 3)
        targets = []
        for x in range(-self.map_radius, self.map_radius + 1, step):
            for y in range(-self.map_radius, self.map_radius + 1, step):
                pos = Position(x, y)
                if self.home.distance_to(pos) <= self.map_radius:
                    targets.append(pos)
        # Sort by distance from home for efficient exploration loops
        targets.sort(key=lambda p: self.home.distance_to(p))
        self.explore_targets = targets

    def turn(self, turn_number, hp, ship_number, cargo, position, power_distribution,
             radar_contacts, leader_board):
        # Update tracking
        self._update_tracking(turn_number, hp, ship_number, position, radar_contacts)

        # Analyze the situation
        analysis = self._analyze_situation(
            turn_number, hp, ship_number, cargo, position,
            power_distribution, radar_contacts, leader_board,
        )

        # Choose strategy
        new_strategy = self._choose_strategy(analysis)

        # Update icon based on strategy
        self._update_icon(new_strategy, cargo)

        # If strategy changed, we may need to reconfigure power first
        desired_power = POWER_CONFIGS[new_strategy]
        if power_distribution != desired_power:
            self.strategy = new_strategy
            return POWER_TO, desired_power

        self.strategy = new_strategy

        # Execute the chosen strategy
        return self._execute_strategy(
            new_strategy, analysis, turn_number, hp, cargo, position,
            power_distribution, radar_contacts, leader_board,
        )

    def _update_tracking(self, turn_number, hp, ship_number, position, radar_contacts):
        """Track game state changes across turns."""
        if ship_number > self.last_ship_number:
            self.deaths += 1
            self.last_ship_number = ship_number
            self.explore_target = None
            self.turns_without_asteroid = 0

        if hp < self.last_hp and self.last_hp > 0:
            self.times_attacked += 1

        self.last_hp = hp
        self.last_position = position

        # Track enemy positions
        for pos, contact_type in radar_contacts.items():
            if contact_type == SPACESHIP:
                self.enemy_sightings.append((turn_number, pos))
                self.known_enemy_positions[pos] = turn_number
                if self.home.distance_to(pos) <= HOME_BASE_RADIUS + 3:
                    self.last_enemy_near_base = turn_number

        # Prune old sightings (keep last 20 turns)
        cutoff = turn_number - 20
        self.enemy_sightings = [
            (t, p) for t, p in self.enemy_sightings if t >= cutoff
        ]
        self.known_enemy_positions = {
            p: t for p, t in self.known_enemy_positions.items() if t >= cutoff
        }

    def _analyze_situation(self, turn_number, hp, ship_number, cargo, position,
                           power_distribution, radar_contacts, leader_board):
        """Produce a comprehensive analysis of the current game state."""
        my_credits = leader_board.get(self.name, 0)
        enemy_credits = {
            name: credits for name, credits in leader_board.items()
            if name != self.name
        }
        max_enemy_credits = max(enemy_credits.values()) if enemy_credits else 0

        # Nearby objects
        nearby_asteroids = [
            pos for pos, t in radar_contacts.items() if t == ASTEROID
        ]
        nearby_enemies = [
            pos for pos, t in radar_contacts.items() if t == SPACESHIP
        ]

        # Distances
        dist_to_home = position.distance_to(self.home)
        in_base = dist_to_home <= HOME_BASE_RADIUS

        # Threats: enemies within attack range or close
        threats = [e for e in nearby_enemies if position.distance_to(e) <= ATTACK_RADIUS + 1]
        immediate_threats = [e for e in nearby_enemies if position.distance_to(e) <= ATTACK_RADIUS]

        # Are threats between us and home?
        threats_blocking_home = [
            e for e in nearby_enemies
            if e.distance_to(self.home) < dist_to_home
            and position.distance_to(e) <= ATTACK_RADIUS + 1
        ]

        # Game phase
        game_progress = turn_number / max(self.total_turns, 1)
        if game_progress < 0.25:
            phase = "early"
        elif game_progress < 0.7:
            phase = "mid"
        else:
            phase = "late"

        # Score position
        if max_enemy_credits == 0:
            score_ratio = 1.0 if my_credits > 0 else 0.5
        else:
            score_ratio = my_credits / max_enemy_credits

        # How dangerous is the environment?
        recent_attacks = sum(
            1 for t, _ in self.enemy_sightings
            if t >= turn_number - 5
        )
        danger_level = min(recent_attacks / max(self.num_enemies, 1), 3.0)

        # Potential pirating reward from killing richest nearby enemy
        pirate_value = max_enemy_credits * PIRATING_REWARD if nearby_enemies else 0

        return {
            "my_credits": my_credits,
            "max_enemy_credits": max_enemy_credits,
            "enemy_credits": enemy_credits,
            "nearby_asteroids": nearby_asteroids,
            "nearby_enemies": nearby_enemies,
            "threats": threats,
            "immediate_threats": immediate_threats,
            "threats_blocking_home": threats_blocking_home,
            "dist_to_home": dist_to_home,
            "in_base": in_base,
            "phase": phase,
            "game_progress": game_progress,
            "score_ratio": score_ratio,
            "danger_level": danger_level,
            "pirate_value": pirate_value,
        }

    def _choose_strategy(self, analysis):
        """Choose the best strategy given current analysis."""
        hp = self.last_hp
        phase = analysis["phase"]
        in_base = analysis["in_base"]
        nearby_asteroids = analysis["nearby_asteroids"]
        nearby_enemies = analysis["nearby_enemies"]
        immediate_threats = analysis["immediate_threats"]
        score_ratio = analysis["score_ratio"]
        pirate_value = analysis["pirate_value"]
        game_progress = analysis["game_progress"]
        dist_to_home = analysis["dist_to_home"]

        # FLEE: Low HP and enemies in attack range - get to safety
        if immediate_threats and hp <= 2:
            return STRATEGY_FLEE

        # ENDGAME SAFE: We're winning in the late game, play it safe
        if phase == "late" and score_ratio > 1.3:
            return STRATEGY_ENDGAME_SAFE

        # Prioritize mining when no immediate threats - this is the core
        # income source. Only deviate for very compelling reasons.
        if not immediate_threats:
            # HUNT only when very behind and enemy is extremely rich
            if (nearby_enemies
                    and score_ratio < 0.5
                    and pirate_value >= MINING_REWARD * 3
                    and hp >= 4
                    and not in_base):
                return STRATEGY_HUNT

            # ARMORED MINE: We've been dying a lot, add protection
            if self.deaths >= 3 and game_progress < 0.6:
                return STRATEGY_ARMORED_MINE

            # Default: pure speed mining
            return STRATEGY_SPEED_MINE

        # We have immediate threats - decide how to handle

        # HUNT: Enemy is very rich and nearby, worth the fight
        if (pirate_value >= MINING_REWARD * 2
                and hp >= 4
                and not in_base):
            return STRATEGY_HUNT

        # ARMORED MINE: threats around but we're healthy, mine with shields
        if hp >= 3:
            return STRATEGY_ARMORED_MINE

        # Default with threats: speed mine (outrun them)
        return STRATEGY_SPEED_MINE

    def _execute_strategy(self, strategy, analysis, turn_number, hp, cargo,
                          position, power_distribution, radar_contacts, leader_board):
        """Execute the chosen strategy's movement logic."""
        if strategy == STRATEGY_FLEE:
            return self._execute_flee(position, power_distribution, cargo, radar_contacts)
        elif strategy == STRATEGY_HUNT:
            return self._execute_hunt(position, power_distribution, cargo, radar_contacts, analysis)
        elif strategy == STRATEGY_AMBUSH:
            return self._execute_ambush(position, power_distribution, cargo, radar_contacts, analysis)
        elif strategy == STRATEGY_ENDGAME_SAFE:
            return self._execute_endgame_safe(position, power_distribution, cargo, radar_contacts)
        else:
            # SPEED_MINE or ARMORED_MINE share the same movement logic
            return self._execute_mine(position, power_distribution, cargo, radar_contacts, turn_number)

    def _execute_mine(self, position, power_distribution, cargo, radar_contacts, turn_number):
        """Mine asteroids efficiently with systematic exploration."""
        speed = max(power_distribution[ENGINES] - cargo, 0)
        if speed <= 0:
            return None

        nearby_asteroids = sorted(
            [pos for pos, t in radar_contacts.items() if t == ASTEROID],
            key=lambda a: position.distance_to(a),
        )

        if cargo >= MAX_CARGO:
            # Head home to deliver
            return self._fly_toward(position, self.home, speed, radar_contacts)

        if nearby_asteroids:
            self.turns_without_asteroid = 0

            if cargo == 0 and len(nearby_asteroids) >= 2:
                # Two asteroids visible: pick the one that minimizes total
                # round-trip (asteroid1 -> asteroid2 -> home)
                best_target = None
                best_cost = float("inf")
                for i, a1 in enumerate(nearby_asteroids):
                    # Cost: go to a1, then to nearest other asteroid, then home
                    others = [a for j, a in enumerate(nearby_asteroids) if j != i]
                    nearest_other = min(others, key=lambda a: a1.distance_to(a))
                    cost = (position.distance_to(a1)
                            + a1.distance_to(nearest_other)
                            + nearest_other.distance_to(self.home))
                    if cost < best_cost:
                        best_cost = cost
                        best_target = a1
                # Compare with: grab closest, go home, come back
                closest = nearby_asteroids[0]
                single_trip_cost = (position.distance_to(closest)
                                    + closest.distance_to(self.home)) * 2
                if best_cost < single_trip_cost:
                    return self._fly_toward(position, best_target, speed, radar_contacts)

            # Default: go to closest asteroid
            target = nearby_asteroids[0]
            return self._fly_toward(position, target, speed, radar_contacts)

        self.turns_without_asteroid += 1

        # If carrying 1 asteroid and haven't found another quickly, deliver
        if cargo == 1 and self.turns_without_asteroid > 4:
            self.turns_without_asteroid = 0
            return self._fly_toward(position, self.home, speed, radar_contacts)

        # Systematic exploration
        return self._explore(position, speed, radar_contacts, turn_number)

    def _execute_flee(self, position, power_distribution, cargo, radar_contacts):
        """Flee toward home base as fast as possible."""
        speed = max(power_distribution[ENGINES] - cargo, 0)
        if speed <= 0:
            # Can't move, just survive
            return None
        return self._fly_toward(position, self.home, speed, radar_contacts)

    def _execute_hunt(self, position, power_distribution, cargo, radar_contacts, analysis):
        """Pursue and attack enemy ships."""
        speed = max(power_distribution[ENGINES] - cargo, 0)
        nearby_enemies = analysis["nearby_enemies"]

        # If carrying cargo, drop it off first (don't risk losing it)
        if cargo > 0:
            if position.distance_to(self.home) <= HOME_BASE_RADIUS:
                # We're at base, cargo will auto-deliver, now go hunt
                pass
            else:
                return self._fly_toward(position, self.home, speed, radar_contacts)

        if nearby_enemies:
            # Chase the closest enemy, trying to stay within attack range
            target = min(nearby_enemies, key=lambda e: position.distance_to(e))
            target_dist = position.distance_to(target)

            if target_dist <= ATTACK_RADIUS:
                # Already in range, stay close but don't overlap
                # Try to match their position to maintain attack range
                if speed > 0:
                    return self._fly_toward(position, target, speed, radar_contacts)
                return None
            else:
                # Close the gap
                if speed > 0:
                    return self._fly_toward(position, target, speed, radar_contacts)
                return None

        # No enemies visible, roam looking for targets
        if speed > 0:
            return self._explore(position, speed, radar_contacts, 0)
        return None

    def _execute_ambush(self, position, power_distribution, cargo, radar_contacts, analysis):
        """Balanced approach: mine when clear, fight when threatened."""
        speed = max(power_distribution[ENGINES] - cargo, 0)
        if speed <= 0:
            return None

        nearby_enemies = analysis["nearby_enemies"]
        nearby_asteroids = [
            pos for pos, t in radar_contacts.items() if t == ASTEROID
        ]

        # If enemies are close, engage
        if nearby_enemies:
            closest_enemy = min(nearby_enemies, key=lambda e: position.distance_to(e))
            # If carrying cargo, head home
            if cargo > 0:
                return self._fly_toward(position, self.home, speed, radar_contacts)
            # Otherwise approach for combat
            return self._fly_toward(position, closest_enemy, speed, radar_contacts)

        # No enemies visible, mine
        if cargo >= MAX_CARGO:
            return self._fly_toward(position, self.home, speed, radar_contacts)

        if nearby_asteroids:
            target = min(nearby_asteroids, key=lambda a: position.distance_to(a))
            return self._fly_toward(position, target, speed, radar_contacts)

        return self._explore(position, speed, radar_contacts, 0)

    def _execute_endgame_safe(self, position, power_distribution, cargo, radar_contacts):
        """Play safe in the endgame - mine efficiently with protection."""
        speed = max(power_distribution[ENGINES] - cargo, 0)
        if speed <= 0:
            return None

        nearby_enemies = [
            pos for pos, t in radar_contacts.items() if t == SPACESHIP
        ]
        nearby_asteroids = [
            pos for pos, t in radar_contacts.items() if t == ASTEROID
        ]

        # If carrying anything, go home immediately
        if cargo > 0:
            return self._fly_toward(position, self.home, speed, radar_contacts)

        # Only mine if no enemies nearby
        if nearby_asteroids and not nearby_enemies:
            target = min(nearby_asteroids, key=lambda a: position.distance_to(a))
            return self._fly_toward(position, target, speed, radar_contacts)

        # If enemies nearby and no cargo, head to safety
        if nearby_enemies:
            return self._fly_toward(position, self.home, speed, radar_contacts)

        # Explore cautiously (close to home)
        if position.distance_to(self.home) > self.map_radius * 0.5:
            return self._fly_toward(position, self.home, speed, radar_contacts)

        return self._explore(position, speed, radar_contacts, 0)

    def _fly_toward(self, position, target, speed, radar_contacts):
        """Fly toward a target position, picking the best reachable tile."""
        if speed <= 0:
            return None

        reachable = list(position.positions_in_range(speed))
        if not reachable:
            return None

        # Filter out positions occupied by other ships
        enemy_positions = {
            pos for pos, t in radar_contacts.items() if t == SPACESHIP
        }
        safe_reachable = [p for p in reachable if p not in enemy_positions]
        if not safe_reachable:
            safe_reachable = reachable

        # Filter out of bounds
        valid = [
            p for p in safe_reachable
            if -self.map_radius <= p.x <= self.map_radius
            and -self.map_radius <= p.y <= self.map_radius
        ]
        if not valid:
            valid = safe_reachable

        # Pick the position closest to our target
        best = min(valid, key=lambda p: p.distance_to(target))
        return FLY_TO, best

    def _explore(self, position, speed, radar_contacts, turn_number):
        """Systematic exploration of the map to find asteroids."""
        # If we have an explore target, continue toward it
        if self.explore_target:
            if position.distance_to(self.explore_target) <= RADAR_RADIUS:
                # We've reached/scanned this area
                self.explored_sectors.add(self.explore_target)
                self.explore_target = None
            else:
                return self._fly_toward(position, self.explore_target, speed, radar_contacts)

        # Pick a new exploration target - prefer unexplored sectors
        unexplored = [
            t for t in self.explore_targets
            if t not in self.explored_sectors
        ]

        if not unexplored:
            # Reset exploration
            self.explored_sectors.clear()
            unexplored = self.explore_targets[:]

        # Pick the closest unexplored sector that's not too close to home
        # (asteroids don't spawn near home base)
        candidates = [
            t for t in unexplored
            if self.home.distance_to(t) > HOME_BASE_RADIUS + 1
        ]
        if not candidates:
            candidates = unexplored

        # Pick closest one for efficiency
        self.explore_target = min(candidates, key=lambda t: position.distance_to(t))
        return self._fly_toward(position, self.explore_target, speed, radar_contacts)

    def _update_icon(self, strategy, cargo):
        """Visual indicator of current strategy."""
        icons = {
            STRATEGY_SPEED_MINE: ">>" if cargo == 0 else "<>",
            STRATEGY_ARMORED_MINE: "[]" if cargo == 0 else "[>",
            STRATEGY_HUNT: "><",
            STRATEGY_FLEE: "!!",
            STRATEGY_AMBUSH: "<]",
            STRATEGY_ENDGAME_SAFE: "()",
        }
        self.icon = icons.get(strategy, "{}")
