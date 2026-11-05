"""
simulate.py — runs full games of The Conspirators of Rome using only the
rules in bot_logic.py (no UI, no dialogue/flavor text), and prints win
rates per difficulty. This is the thing to run to see the rules "in
action" and sanity-check what they produce.

Usage:
    python simulate.py                  # default: 2000 games/cell, 5-8 players
    python simulate.py --games 500 --counts 5 6
"""

from __future__ import annotations
import argparse
import random
from bot_logic import (
    Player, GameState, bot_choose_nominee, bot_vote_choice, bot_discard_choice,
    bot_enact_choice, bot_veto_choice, bot_investigate_choice, bot_proscribe_choice,
    forced_enactment_hurts_team, consul_values_cover, update_public_suspicion,
    record_nay_votes, update_vote_trust, apply_proscribe_trust, mark_confirmed_enemy,
    SUSPICION_START, SLEEPER_PROBABILITY_CONSPIRATOR, SLEEPER_PROBABILITY_IMPERATOR,
)

ROMAN_NAMES = ["Marcus", "Gaius", "Lucius", "Quintus", "Titus", "Brutus",
               "Cassius", "Cicero", "Cato", "Drusus"]

# Role counts for a standard-size game (5-8 players) — ALWAYS exactly one
# Imperator in the deal; only who gets it is random. See README.
ROLE_COUNTS = {
    5: {"senator": 3, "conspirator": 1, "imperator": 1},
    6: {"senator": 4, "conspirator": 1, "imperator": 1},
    7: {"senator": 4, "conspirator": 2, "imperator": 1},
    8: {"senator": 5, "conspirator": 2, "imperator": 1},
}

# Which legislative-term triggers which power, by table size.
POWER_TABLE = {
    "small": {3: "investigate", 4: "proscribe", 5: "proscribe"},  # 5-6p
    "mid": {2: "investigate", 3: "special_election", 4: "proscribe", 5: "proscribe"},  # 7-8
}


def power_tier(count: int) -> str:
    return "small" if count <= 6 else "mid"


def fresh_deck() -> list:
    deck = ["RP"] * 7 + ["IM"] * 14
    random.shuffle(deck)
    return deck


def build_players(count: int, difficulty: str) -> list:
    counts = ROLE_COUNTS[count]
    roles = (["senator"] * counts["senator"] +
             ["conspirator"] * counts["conspirator"] +
             ["imperator"] * counts["imperator"])
    random.shuffle(roles)
    names = random.sample(ROMAN_NAMES, count)
    players = [Player(id=i, name=names[i], role=roles[i]) for i in range(count)]
    players[0].is_reference = True  # seat 0 always plays at full skill

    conspirators = [p for p in players if p.team == "conspiracy"]
    for p in players:
        if p.team == "conspiracy":
            p.allies = [c.id for c in conspirators if c.id != p.id]
            prob = SLEEPER_PROBABILITY_IMPERATOR if p.role == "imperator" else SLEEPER_PROBABILITY_CONSPIRATOR
            p.sleeper = random.random() < prob
        for other in players:
            if other.id != p.id:
                p.suspicion[other.id] = SUSPICION_START
    return players


def play_game(count: int, difficulty: str, force_role: str | None = None) -> dict | None:
    """Plays one full game. If force_role is set and that role isn't in the
    deal (never happens for 5-8p — see README), returns None so the caller
    can redraw and keep the sample size exact."""
    players = build_players(count, difficulty)
    if force_role:
        target = next((p for p in players if p.role == force_role), None)
        if target is None:
            return None
        players[0].role, target.role = target.role, players[0].role
        players[0].team = "senate" if players[0].role == "senator" else "conspiracy"
        target.team = "senate" if target.role == "senator" else "conspiracy"

    state = GameState(players=players, count=count, difficulty=difficulty)
    deck, discard = fresh_deck(), []
    election_tracker = 0
    tier = power_tier(count)
    im_threshold = state.im_win_threshold()
    rounds_guard = 0

    def ensure_deck(n):
        nonlocal deck, discard
        if len(deck) < n:
            deck = deck + discard
            random.shuffle(deck)
            discard = []

    while rounds_guard < 500:
        rounds_guard += 1
        princeps = state.current_princeps()
        nominee_id = bot_choose_nominee(princeps, state)
        if nominee_id is None:
            return {"winner": "senate", "reason": "no eligible nominee"}

        votes = {p.id: bot_vote_choice(p, nominee_id, state) for p in state.alive_players()}
        update_vote_trust(state, nominee_id, princeps.id, votes)
        record_nay_votes(state, nominee_id, votes)
        ayes = sum(1 for v in votes.values() if v)
        passed = ayes > len(votes) - ayes

        if not passed:
            election_tracker += 1
            if election_tracker >= 3:
                ensure_deck(1)
                card = deck.pop(0)
                if card == "RP":
                    state.rp += 1
                else:
                    state.im += 1
                election_tracker = 0
                state.previous_consul_id = state.previous_princeps_id = None
                if state.rp >= 5:
                    return {"winner": "senate", "reason": "rp_track"}
                if state.im >= im_threshold:
                    return {"winner": "conspiracy", "reason": "im_track"}
            state.princeps_index = (state.princeps_index + 1) % count
            while not state.players[state.princeps_index].alive:
                state.princeps_index = (state.princeps_index + 1) % count
            continue

        # Confirmed Consul: instant Conspiracy win if the Imperator gets
        # elected Consul once caution mode (im>=3) has kicked in.
        nominee = state.by_id(nominee_id)
        if state.im >= 3 and nominee.role == "imperator":
            return {"winner": "conspiracy", "reason": "imperator_elected_consul"}

        state.previous_consul_id = nominee_id
        state.previous_princeps_id = princeps.id
        state.consul_id = nominee_id
        election_tracker = 0
        # --- legislative session ---
        ensure_deck(3)
        three = [deck.pop(0), deck.pop(0), deck.pop(0)]
        d_idx = bot_discard_choice(princeps, three, state)
        discard.append(three.pop(d_idx))
        two = three
        hand_mixed = two[0] != two[1]
        consul = state.by_id(state.consul_id)

        veto_threshold = 4 if state.small_game() else 5
        veto_happened = False
        if state.im >= veto_threshold:
            truly_forced = forced_enactment_hurts_team(consul, two)
            wants_cover_veto = (not truly_forced and consul.team != "senate" and hand_mixed
                                 and consul_values_cover(consul, state) and random.random() < 0.25)
            if truly_forced or wants_cover_veto:
                accept = bot_veto_choice(princeps, two, consul.id, state)
                if accept:
                    discard.extend(two)
                    veto_happened = True

        if not veto_happened:
            e_idx = bot_enact_choice(consul, two, state)
            enacted = two[e_idx]
            discard.append(two[1 - e_idx])
            rp_before, im_before = state.rp, state.im
            if enacted == "RP":
                state.rp += 1
            else:
                state.im += 1
            update_public_suspicion(state, princeps.id, consul.id, enacted, rp_before, im_before, hand_mixed)

            if state.rp >= 5:
                return {"winner": "senate", "reason": "rp_track"}
            if state.im >= im_threshold:
                return {"winner": "conspiracy", "reason": "im_track"}

            if enacted == "IM":
                power = POWER_TABLE[tier].get(state.im)
                if power == "investigate":
                    target_id = bot_investigate_choice(princeps, state)
                    target = state.by_id(target_id)
                    if target.team != princeps.team:
                        mark_confirmed_enemy(princeps, target)
                    else:
                        princeps.intel[target_id] = "loyal"
                elif power == "proscribe":
                    target_id = bot_proscribe_choice(princeps, state)
                    target = state.by_id(target_id)
                    apply_proscribe_trust(state, princeps, target)
                    target.alive = False
                    if target.role == "imperator":
                        return {"winner": "senate", "reason": "imperator_proscribed"}
                    survivors = state.alive_players()
                    if len(survivors) == 1:
                        winner = "senate" if survivors[0].team == "senate" else "conspiracy"
                        return {"winner": winner, "reason": "last_standing"}
                elif power == "special_election":
                    pool = [p for p in state.alive_players() if p.id != princeps.id]
                    pick = random.choice(pool)
                    state.princeps_index = state.players.index(pick)
                    continue  # special election bypasses the normal rotation once
                # 'peek' has no mechanical effect in this simulation.

        state.princeps_index = (state.princeps_index + 1) % count
        while not state.players[state.princeps_index].alive:
            state.princeps_index = (state.princeps_index + 1) % count

    return {"winner": "senate", "reason": "round_limit"}

def run_batch(games: int, count: int, difficulty: str, force_role: str | None) -> dict:
    results = {"senate": 0, "conspiracy": 0, "skipped": 0}
    played = 0
    while played < games:
        outcome = play_game(count, difficulty, force_role)
        if outcome is None:
            results["skipped"] += 1
            continue
        results[outcome["winner"]] += 1
        played += 1
    return results


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--games", type=int, default=2000, help="games per cell")
    ap.add_argument("--counts", type=int, nargs="+", default=[5, 6, 7, 8])
    args = ap.parse_args()

    difficulties = ["easy", "medium", "hard", "impossible"]
    print(f"{'count':>6} {'difficulty':>10} {'senate %':>10} {'conspiracy %':>13}")
    for count in args.counts:
        for diff in difficulties:
            r = run_batch(args.games, count, diff, force_role=None)
            total = r["senate"] + r["conspiracy"]
            print(f"{count:>6} {diff:>10} {100*r['senate']/total:>9.1f}% {100*r['conspiracy']/total:>12.1f}%")


if __name__ == "__main__":
    main()
