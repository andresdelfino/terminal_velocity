import math
import random

from tv.game import (
    Position, POWER_TO, FLY_TO, ENGINES, SHIELDS, LASERS,
    ASTEROID, SPACESHIP, HOME_BASE,
    MAX_HP, MAX_CARGO, HOME_BASE_RADIUS, RADAR_RADIUS, ATTACK_RADIUS,
    MINING_REWARD, PIRATING_REWARD,
)

# Strategies - all maintain at least 1 engine for mobility
STRATEGY_HUNT = "hunt"         # 1E/0S/2L - pursue and destroy visible enemies
STRATEGY_PATROL = "patrol"     # 2E/0S/1L - sweep for targets, still dangerous
STRATEGY_RETREAT = "retreat"   # 2E/1S/0L - low HP, get to safety fast

POWER_CONFIGS = {
    STRATEGY_HUNT: {ENGINES: 1, SHIELDS: 0, LASERS: 2},
    STRATEGY_PATROL: {ENGINES: 2, SHIELDS: 0, LASERS: 1},
    STRATEGY_RETREAT: {ENGINES: 2, SHIELDS: 1, LASERS: 0},
}


class BotLogic:
    """
    Kill-maximizing adaptive bot. Camps the base boundary where all enemies
    must pass (to deliver cargo and after respawning), pursues visible enemies
    aggressively, and patrols approach corridors during lulls. Never sacrifices
    mobility - always maintains at least 1 engine.
    """

    def initialize(self, player_name, map_radius, players, turns, home_base_positions):
        self.name = player_name
        self.map_radius = map_radius
        self.total_turns = turns
        self.home = Position(0, 0)
        self.num_enemies = len(players) - 1

        # Enemy tracking
        self.all_sightings = []
        self.turns_since_enemy = 0

        # State
        self.strategy = STRATEGY_PATROL
        self.last_hp = MAX_HP
        self.last_ship_number = 1
        self.patrol_target = None
        self.patrol_index = 0

        # Pre-compute the "kill ring": positions just outside sanctuary
        # where we can attack anyone entering/leaving base
        self.kill_ring = []
        for pos in self.home.positions_in_range(HOME_BASE_RADIUS + ATTACK_RADIUS):
            d = self.home.distance_to(pos)
            # Just outside sanctuary but within attack range of the boundary
            if HOME_BASE_RADIUS < d <= HOME_BASE_RADIUS + ATTACK_RADIUS:
                self.kill_ring.append(pos)

        # Patrol waypoints: ring around base to intercept all approach vectors
        self._build_patrol_route()

        self.icon = "><"

    def _build_patrol_route(self):
        """Build a circular patrol route around the base, covering all
        approach directions. Stays close enough to quickly engage."""
        waypoints = []
        # Tight patrol just outside sanctuary: enemies MUST cross this zone
        # to deliver cargo or after respawning. Radar (3) covers the boundary.
        for ring_r in [HOME_BASE_RADIUS + 2, HOME_BASE_RADIUS + 4]:
            n = max(6, int(2 * math.pi * ring_r / (RADAR_RADIUS * 1.5)))
            for i in range(n):
                angle = 2 * math.pi * i / n
                x = int(round(ring_r * math.cos(angle)))
                y = int(round(ring_r * math.sin(angle)))
                if abs(x) <= self.map_radius and abs(y) <= self.map_radius:
                    waypoints.append(Position(x, y))
        # Sort by angle for a coherent circular sweep
        waypoints.sort(key=lambda p: math.atan2(p.y, p.x))
        self.patrol_waypoints = waypoints

    def turn(self, turn_number, hp, ship_number, cargo, position,
             power_distribution, radar_contacts, leader_board):
        # Track state
        self._update_tracking(turn_number, hp, ship_number, radar_contacts)

        # Classify contacts
        nearby_enemies = []
        nearby_asteroids = []
        for pos, kind in radar_contacts.items():
            if kind == SPACESHIP:
                nearby_enemies.append(pos)
            elif kind == ASTEROID:
                nearby_asteroids.append(pos)

        # Enemies we can actually fight (outside sanctuary)
        targetable = [
            e for e in nearby_enemies
            if self.home.distance_to(e) > HOME_BASE_RADIUS
        ]
        in_attack_range = [
            e for e in targetable
            if position.distance_to(e) <= ATTACK_RADIUS
        ]
        dist_to_home = position.distance_to(self.home)
        in_sanctuary = dist_to_home <= HOME_BASE_RADIUS

        # Choose strategy
        new_strategy = self._choose_strategy(
            hp, targetable, in_attack_range, in_sanctuary, cargo,
            nearby_asteroids, turn_number,
        )

        # Update icon
        self._update_icon(new_strategy, hp, in_attack_range)

        # Apply power if needed (costs one turn)
        desired_power = POWER_CONFIGS[new_strategy]
        if power_distribution != desired_power:
            self.strategy = new_strategy
            return POWER_TO, desired_power

        self.strategy = new_strategy

        # Execute movement
        speed = max(power_distribution[ENGINES] - cargo, 0)
        if speed <= 0:
            return None

        if new_strategy == STRATEGY_RETREAT:
            return self._move_toward(position, self.home, speed, radar_contacts)

        if new_strategy == STRATEGY_HUNT:
            return self._execute_hunt(
                position, speed, targetable, in_attack_range,
                in_sanctuary, cargo, nearby_asteroids, radar_contacts,
            )

        # PATROL
        return self._execute_patrol(
            position, speed, cargo, nearby_asteroids, radar_contacts,
        )

    def _update_tracking(self, turn_number, hp, ship_number, radar_contacts):
        if ship_number > self.last_ship_number:
            self.last_ship_number = ship_number
            self.patrol_target = None

        self.last_hp = hp

        saw_enemy = False
        for pos, kind in radar_contacts.items():
            if kind == SPACESHIP:
                self.all_sightings.append((turn_number, pos))
                saw_enemy = True

        self.turns_since_enemy = 0 if saw_enemy else self.turns_since_enemy + 1

        # Prune old data
        cutoff = turn_number - 20
        self.all_sightings = [(t, p) for t, p in self.all_sightings if t >= cutoff]

    def _choose_strategy(self, hp, targetable, in_attack_range, in_sanctuary,
                         cargo, nearby_asteroids, turn_number):
        # RETREAT: critically low HP, no targets in range
        if hp <= 1 and not in_attack_range:
            return STRATEGY_RETREAT

        # HUNT: enemies visible and attackable - engage!
        if targetable:
            return STRATEGY_HUNT

        # HUNT config even without visible enemies if we saw one very recently
        # (they might reappear next turn, avoid wasting a turn on power switch)
        if self.turns_since_enemy <= 1:
            return STRATEGY_HUNT

        # PATROL: no enemies visible, sweep to find them
        return STRATEGY_PATROL

    def _execute_hunt(self, position, speed, targetable, in_attack_range,
                      in_sanctuary, cargo, nearby_asteroids, radar_contacts):
        """Pursue and destroy enemies. If already in range, stay close.
        If in sanctuary, step out to be able to attack."""

        # If we're in sanctuary, step out so auto-attacks activate
        if in_sanctuary and targetable:
            # Move toward the nearest targetable enemy, which is outside sanctuary
            target = min(targetable, key=lambda e: position.distance_to(e))
            return self._move_toward_combat(position, target, speed, radar_contacts)

        if in_attack_range:
            # Already shooting. Optimal play: stay in range of the most targets.
            # If only one, track them to prevent escape.
            if len(in_attack_range) == 1:
                target = in_attack_range[0]
                return self._move_toward_combat(position, target, speed, radar_contacts)
            else:
                # Multiple targets in range - find position that keeps max in range
                return self._maximize_targets(position, speed, in_attack_range, radar_contacts)

        # Enemies visible but not in range - close the gap
        if targetable:
            target = min(targetable, key=lambda e: position.distance_to(e))
            return self._move_toward_combat(position, target, speed, radar_contacts)

        # No targetable enemies, but still in hunt mode (turns_since_enemy <= 1)
        # Move toward last known position or patrol kill ring
        if self.all_sightings:
            last_pos = self.all_sightings[-1][1]
            return self._move_toward_combat(position, last_pos, speed, radar_contacts)

        # Fallback: head to kill ring
        return self._move_to_kill_ring(position, speed, radar_contacts)

    def _execute_patrol(self, position, speed, cargo, nearby_asteroids,
                        radar_contacts):
        """Patrol around base to find enemies. Pick up asteroids on the way
        for supplemental income."""
        # Deliver cargo if carrying any (from scavenging)
        if cargo > 0 and position.distance_to(self.home) <= HOME_BASE_RADIUS + 2:
            return self._move_toward(position, self.home, speed, radar_contacts)

        # Grab a nearby asteroid if it's on our path (don't go far out of way)
        if cargo < MAX_CARGO and nearby_asteroids:
            closest_asteroid = min(nearby_asteroids,
                                   key=lambda a: position.distance_to(a))
            if position.distance_to(closest_asteroid) <= speed:
                return self._move_toward(position, closest_asteroid, speed, radar_contacts)

        # If we haven't seen enemies in a long time, do a wider sweep
        # (they might be mining far from base)
        if self.turns_since_enemy > 15:
            # Head to a random outer position to flush out distant miners
            if not self.patrol_target or position.distance_to(self.patrol_target) <= RADAR_RADIUS:
                angle = random.uniform(0, 2 * math.pi)
                r = HOME_BASE_RADIUS + 7
                x = max(-self.map_radius, min(self.map_radius, int(round(r * math.cos(angle)))))
                y = max(-self.map_radius, min(self.map_radius, int(round(r * math.sin(angle)))))
                self.patrol_target = Position(x, y)
            return self._move_toward(position, self.patrol_target, speed, radar_contacts)

        # Follow patrol route (tight ring around base)
        if self.patrol_target and position.distance_to(self.patrol_target) > RADAR_RADIUS:
            return self._move_toward(position, self.patrol_target, speed, radar_contacts)

        # Advance to next waypoint
        if self.patrol_waypoints:
            self.patrol_index = (self.patrol_index + 1) % len(self.patrol_waypoints)
            self.patrol_target = self.patrol_waypoints[self.patrol_index]
            return self._move_toward(position, self.patrol_target, speed, radar_contacts)

        return self._move_to_kill_ring(position, speed, radar_contacts)

    def _move_to_kill_ring(self, position, speed, radar_contacts):
        """Move to a position on the kill ring (just outside base)."""
        if self.kill_ring:
            target = min(self.kill_ring, key=lambda p: position.distance_to(p))
            return self._move_toward(position, target, speed, radar_contacts)
        return None

    def _maximize_targets(self, position, speed, targets, radar_contacts):
        """Find reachable position that keeps maximum targets in attack range."""
        reachable = self._get_valid_positions(position, speed, radar_contacts)
        if not reachable:
            return None

        def score(p):
            # Count how many targets are in attack range from this position
            in_range = sum(1 for t in targets if p.distance_to(t) <= ATTACK_RADIUS)
            # Penalize sanctuary (can't attack from there)
            if self.home.distance_to(p) <= HOME_BASE_RADIUS:
                in_range = 0
            return -in_range  # minimize = maximize targets

        best = min(reachable, key=score)
        return FLY_TO, best

    def _move_toward(self, position, target, speed, radar_contacts):
        """Basic movement toward a target."""
        reachable = self._get_valid_positions(position, speed, radar_contacts)
        if not reachable:
            return None
        best = min(reachable, key=lambda p: p.distance_to(target))
        return FLY_TO, best

    def _move_toward_combat(self, position, target, speed, radar_contacts):
        """Movement toward target with combat awareness - avoid sanctuary."""
        reachable = self._get_valid_positions(position, speed, radar_contacts)
        if not reachable:
            return None

        def combat_score(p):
            dist = p.distance_to(target)
            # Heavy penalty for entering sanctuary (disables our attacks)
            if self.home.distance_to(p) <= HOME_BASE_RADIUS:
                return 50 + dist
            # Prefer being within attack range
            if dist <= ATTACK_RADIUS:
                return dist * 0.5  # Bonus for attack range
            return dist

        best = min(reachable, key=combat_score)
        return FLY_TO, best

    def _get_valid_positions(self, position, speed, radar_contacts):
        """Get reachable, valid positions (in bounds, not occupied)."""
        reachable = list(position.positions_in_range(speed))
        enemy_positions = {
            pos for pos, t in radar_contacts.items() if t == SPACESHIP
        }
        valid = [
            p for p in reachable
            if p not in enemy_positions
            and -self.map_radius <= p.x <= self.map_radius
            and -self.map_radius <= p.y <= self.map_radius
        ]
        return valid if valid else reachable

    def _update_icon(self, strategy, hp, in_attack_range):
        if in_attack_range:
            self.icon = "><"  # Engaging
        elif strategy == STRATEGY_RETREAT:
            self.icon = "!!"
        elif strategy == STRATEGY_HUNT:
            self.icon = "->"  # Chasing
        else:
            self.icon = ">>"  # Patrolling
