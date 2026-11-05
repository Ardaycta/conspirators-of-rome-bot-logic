"""
bot_logic.py — The Conspirators of Rome: bot decision rules.

A faithful Python port of the bot AI from the game's JS implementation —
not the UI, not the dialogue/flavor-text layer, just the actual rules bots
use to decide what to do. Ported so the logic can be read, reviewed, and
run independently of the browser game.

THE CORE IDEA
--------------
Bots never see hidden roles directly. Everything a bot "knows" is either:
  1. PUBLIC evidence anyone at the table could reconstruct (who served as
     Princeps/Consul, what card came out, how people voted), tracked as a
     running 0-100 suspicion score per player ("how Conspiracy-like do they
     look"), or
  2. PRIVATE certainty: an honest Investigate Loyalty result, or catching a
     Consul who had a real choice (a mixed RP/IM hand) but enacted Imperium
     anyway — that's provable, not probabilistic.

DIFFICULTY = HOW OFTEN THE STRATEGIC READ IS USED
---------------------------------------------------
Each bot decision first rolls against `skill_prob(bot, state)` — the
probability it uses the real strategic logic below. On a failed roll, it
falls back to a simpler, still team-consistent default (never a coin flip
against its own side). Card choice itself (which decree to keep) is the
one exception: every bot always favors its own team there, regardless of
skill — skill only governs whether a Conspirator plays an out-of-character
"cover" card to look innocent.
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional
import random
import math


# ---------------------------------------------------------------------------
# Skill tiers & difficulty
# ---------------------------------------------------------------------------

SKILL_TIERS = {
    "perfect": 1.00,  # "impossible" opponents — plays almost perfectly
    "great": 0.90,    # "easy" teammates / "hard" opponents
    "average": 0.65,  # "medium" teammates and opponents
    "bad": 0.30,      # "easy" opponents / "hard" teammates
    "poor": 0.12,     # "impossible" teammates — not so sensible
}

DIFFICULTY_TABLE = {
    "easy": {"teammate": "great", "opponent": "bad"},
    "medium": {"teammate": "average", "opponent": "average"},
    "hard": {"teammate": "bad", "opponent": "great"},
    "impossible": {"teammate": "poor", "opponent": "perfect"},
}
# ---------------------------------------------------------------------------
# Suspicion-model constants (see docstrings on the functions that use them)
# ---------------------------------------------------------------------------

SUSPICION_START = 50
BASE_IM_SUSPICION = 18
BASE_RP_TRUST = 14
PRINCEPS_WEIGHT = 0.45
MIN_FORCED_FACTOR = 0.15
TEST_GRAY_LOW, TEST_GRAY_HIGH = 35, 65
TEST_MAX_SAMPLE = 1
TEST_PROBABILITY = 0.6
NAIVE_SENATE_AYE_PROBABILITY = 0.75
SENATE_NAY_THRESHOLD = 70
SENATE_NAY_THRESHOLD_CAUTION = 62
SENATE_NAY_THRESHOLD_CAUTION_LARGE = 56
CONSPIRACY_OBSTRUCTION_RP_PROGRESS = 0.6
CONSPIRACY_OBSTRUCTION_TRUST_THRESHOLD = 40
CONSPIRACY_OBSTRUCTION_PROBABILITY = 0.4
VOTE_TRUST_SHIFT = 5
VOTE_TRUST_RISKY = 60
VOTE_TRUST_SAFE = 40
RETRO_VINDICATION_SHIFT = 10
PROSCRIBE_TRUST_MAX_SHIFT = 12
PROSCRIBE_TIE_BAND = 5
CAUTION_IM_CARDS = 3
CAUTION_TOTAL_CARDS = 5
SLEEPER_PROBABILITY_CONSPIRATOR = 0.30
SLEEPER_PROBABILITY_IMPERATOR = 0.50
SLEEPER_COVER_THRESHOLD = 58
SLEEPER_BASE_COVER_CHANCE = 0.9
NON_SLEEPER_BASE_COVER_CHANCE = 0.18
SLEEPER_LATE_DEFECT_CHANCE = 0.15
SLEEPER_EXPOSED_DEFECT_BONUS = 0.25
RUSH_IM_PROGRESS = 0.7
EMERGENCY_RP = 4
URGENT_IM_MARGIN = 1
URGENT_IM_MARGIN_SMALL = 1
URGENT_PROSCRIBE_TIE_BAND = 1
FRIENDLY_FIRE_HEDGE_BAND = 10


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class Player:
    id: int
    name: str
    role: str          # 'senator' | 'conspirator' | 'imperator'
    team: str = ""      # 'senate' | 'conspiracy'
    alive: bool = True
    is_reference: bool = False  # the one seat always treated as fully skilled
                                 # (mirrors the human seat in the real game)
    intel: dict = field(default_factory=dict)       # target_id -> 'loyal'|'enemy'
    suspicion: dict = field(default_factory=dict)    # target_id -> 0-100
    observed_terms: dict = field(default_factory=dict)
    allies: list = field(default_factory=list)
    sleeper: bool = False
    exposed_to: list = field(default_factory=list)
    nay_votes_received: list = field(default_factory=list)

    def __post_init__(self):
        self.team = "senate" if self.role == "senator" else "conspiracy"


@dataclass
class GameState:
    players: list
    count: int
    difficulty: str = "medium"
    princeps_index: int = 0
    consul_id: Optional[int] = None
    previous_consul_id: Optional[int] = None
    previous_princeps_id: Optional[int] = None
    rp: int = 0
    im: int = 0

    def alive_players(self):
        return [p for p in self.players if p.alive]

    def by_id(self, pid):
        return next((p for p in self.players if p.id == pid), None)

    def current_princeps(self):
        return self.players[self.princeps_index]

    def small_game(self):
        return self.count <= 4

    def im_win_threshold(self):
        return 5 if self.small_game() else 6

# ---------------------------------------------------------------------------
# Skill gate
# ---------------------------------------------------------------------------

def skill_prob(bot: Player, state: GameState) -> float:
    """The probability `bot` uses its real strategic logic on this decision.

    The reference seat (is_reference=True, mirroring the human player in the
    real game) always plays at full skill — difficulty never touches it.
    Everyone else's tier depends on whether they share the reference seat's
    team ("teammate") or not ("opponent").
    """
    reference = next((p for p in state.players if p.is_reference), None)
    if reference is None:
        return 1.0
    tiers = DIFFICULTY_TABLE.get(state.difficulty, DIFFICULTY_TABLE["medium"])
    tier_name = tiers["teammate"] if bot.team == reference.team else tiers["opponent"]
    return SKILL_TIERS.get(tier_name, 1.0)


def smart_roll(bot: Player, state: GameState) -> bool:
    return random.random() < skill_prob(bot, state)


# ---------------------------------------------------------------------------
# Suspicion-model helpers
# ---------------------------------------------------------------------------

def clamp100(v: float) -> float:
    return max(0.0, min(100.0, v))


def get_effective_suspicion(observer: Player, target_id: int) -> float:
    """An observer's working suspicion of target: private certainty
    (Investigate Loyalty result) overrides the fuzzy public score."""
    intel = observer.intel.get(target_id)
    if intel == "enemy":
        return 96
    if intel == "loyal":
        return 4
    return observer.suspicion.get(target_id, SUSPICION_START)


def average_public_suspicion(bot: Player, state: GameState) -> float:
    others = [p for p in state.alive_players() if p.id != bot.id]
    if not others:
        return SUSPICION_START
    total = sum(o.suspicion.get(bot.id, SUSPICION_START) for o in others)
    return total / len(others)


def in_caution(state: GameState) -> bool:
    """Once the game is this far along, bots stop testing ambiguous players
    and commit to their safe default — a wrong guess this late can hand
    over the win outright."""
    return state.im >= CAUTION_IM_CARDS or (state.rp + state.im) >= CAUTION_TOTAL_CARDS


def im_progress(state: GameState) -> float:
    return state.im / state.im_win_threshold()


def rp_progress(state: GameState) -> float:
    return state.rp / 5


def n_choose_k(n: int, k: int) -> float:
    if k < 0 or k > n:
        return 0
    return math.comb(n, k)


def prob_all_k_of_type(of_type: int, total: int, k: int) -> float:
    """Probability that k cards drawn at random from `total` unseen cards
    (of which `of_type` are the type in question) are ALL that type —
    the card-counting math behind "how avoidable was this outcome"."""
    if total < k:
        return 0
    return n_choose_k(of_type, k) / n_choose_k(total, k)


def known_allies(player: Player, small_game: bool) -> list:
    """What a player LEGITIMATELY knows about their own team right now.
    Standard-size Conspirators know their team from the deal; everyone
    else only learns a teammate by personally confirming them "loyal"."""
    if not small_game and player.team == "conspiracy":
        return list(player.allies)
    return [pid for pid, v in player.intel.items() if v == "loyal"]


def alive_exposers(bot: Player, state: GameState) -> list:
    """Living players who hold confirmed proof `bot` is an enemy."""
    return [p for p in (state.by_id(i) for i in bot.exposed_to) if p and p.alive]


def test_candidates(bot: Player, pool: list, state: GameState) -> list:
    """Players still 'worth testing' — ambiguous suspicion AND a small
    sample size, so the next outcome actually teaches something. Nobody
    gets tested once the game reaches caution mode."""
    if in_caution(state):
        return []
    out = []
    for p in pool:
        if bot.intel.get(p.id) in ("loyal", "enemy"):
            continue
        s = get_effective_suspicion(bot, p.id)
        seen = bot.observed_terms.get(p.id, 0)
        if TEST_GRAY_LOW <= s <= TEST_GRAY_HIGH and seen <= TEST_MAX_SAMPLE:
            out.append(p)
    return out


def narrow_by_nay_votes(candidates: list, want_fewest: bool) -> list:
    """Among tied proscribe candidates, narrow toward whoever has the
    fewest (Conspiracy's 'safest-looking') or most (Senate's 'most
    corroborated') public Nay votes ever received as a nominee."""
    if len(candidates) <= 1:
        return candidates
    counts = [len(p.nay_votes_received) for p in candidates]
    target = min(counts) if want_fewest else max(counts)
    narrowed = [p for p, c in zip(candidates, counts) if c == target]
    return narrowed or candidates


# ---------------------------------------------------------------------------
# Public-evidence updates (called once per real legislative session)
# ---------------------------------------------------------------------------

def update_public_suspicion(state: GameState, princeps_id: int, consul_id: int,
                             enacted: str, rp_before: int, im_before: int,
                             hand_was_mixed: bool) -> None:
    """Everyone's suspicion of that round's Princeps & Consul moves, scaled
    by how AVOIDABLE the outcome plausibly was — using real card-counting
    on the public discarded/remaining counts. A bad decree when the deck
    was already stacked with Imperium is barely suspicious; the same bad
    decree with plenty of Res Publica still unseen is very suspicious.
    Consul moves further than Princeps (final say between two decrees).
    """
    rp_unseen, im_unseen = 7 - rp_before, 14 - im_before
    total_unseen = rp_unseen + im_unseen

    if enacted == "IM":
        p_consul_forced = prob_all_k_of_type(im_unseen, total_unseen, 2)
        p_princeps_forced = prob_all_k_of_type(im_unseen, total_unseen, 3)
        consul_delta = BASE_IM_SUSPICION * max(MIN_FORCED_FACTOR, 1 - p_consul_forced)
        princeps_delta = BASE_IM_SUSPICION * PRINCEPS_WEIGHT * max(MIN_FORCED_FACTOR, 1 - p_princeps_forced)
        sign = 1
    else:
        p_consul_forced = prob_all_k_of_type(rp_unseen, total_unseen, 2)
        p_princeps_forced = prob_all_k_of_type(rp_unseen, total_unseen, 3)
        consul_delta = BASE_RP_TRUST * max(MIN_FORCED_FACTOR, 1 - p_consul_forced)
        princeps_delta = BASE_RP_TRUST * PRINCEPS_WEIGHT * max(MIN_FORCED_FACTOR, 1 - p_princeps_forced)
        sign = -1

    for observer in state.players:
        if observer.id != consul_id:
            cur = observer.suspicion.get(consul_id, SUSPICION_START)
            observer.suspicion[consul_id] = clamp100(cur + sign * consul_delta)
        if observer.id != princeps_id:
            cur2 = observer.suspicion.get(princeps_id, SUSPICION_START)
            observer.suspicion[princeps_id] = clamp100(cur2 + sign * princeps_delta)
        observer.observed_terms[princeps_id] = observer.observed_terms.get(princeps_id, 0) + 1
        observer.observed_terms[consul_id] = observer.observed_terms.get(consul_id, 0) + 1

    # Ground truth: the Princeps personally handed the Consul these two
    # cards. If that hand had one of each type and the Consul still chose
    # Imperium, that PROVES the Consul is an enemy — no Senate-aligned bot
    # ever passes up an available Res Publica. Standard games only (see
    # README — small games don't grant this free certainty).
    if enacted == "IM" and hand_was_mixed and not state.small_game():
        princeps, consul = state.by_id(princeps_id), state.by_id(consul_id)
        if princeps and consul:
            mark_confirmed_enemy(princeps, consul)


def apply_vindication(observer: Player, target: Player) -> None:
    """The moment `observer` confirms `target` is really an enemy, anyone
    who EVER voted Nay on `target` looks a little more trustworthy to
    `observer` now — they were right to be wary, even without proof then."""
    for voter_id in target.nay_votes_received:
        if voter_id == observer.id:
            continue
        cur = observer.suspicion.get(voter_id, SUSPICION_START)
        observer.suspicion[voter_id] = clamp100(cur - RETRO_VINDICATION_SHIFT)


def mark_confirmed_enemy(observer: Player, target: Player) -> None:
    if observer.intel.get(target.id) == "enemy":
        return
    observer.intel[target.id] = "enemy"
    if observer.id not in target.exposed_to:
        target.exposed_to.append(observer.id)
    apply_vindication(observer, target)


def record_nay_votes(state: GameState, nominee_id: int, votes: dict) -> None:
    """Votes are cast in full view — who voted Nay on a nominee is an
    objective, permanent public fact, regardless of anyone's private
    suspicion at the time."""
    nominee = state.by_id(nominee_id)
    for voter in state.alive_players():
        if votes.get(voter.id) is False and voter.id not in nominee.nay_votes_received:
            nominee.nay_votes_received.append(voter.id)


def update_vote_trust(state: GameState, nominee_id: int, princeps_id: int, votes: dict) -> None:
    """A vote relative to how risky the pairing actually looked is itself a
    tell: waving through an obviously risky pairing, or blocking an
    obviously safe one, nudges how trustworthy the VOTER looks."""
    for observer in state.alive_players():
        for voter in state.alive_players():
            if voter.id == observer.id or voter.id not in votes:
                continue
            voted = votes[voter.id]
            threat = (get_effective_suspicion(observer, nominee_id) +
                      get_effective_suspicion(observer, princeps_id)) / 2
            cur = observer.suspicion.get(voter.id, SUSPICION_START)
            if voted and threat >= VOTE_TRUST_RISKY:
                observer.suspicion[voter.id] = clamp100(cur + VOTE_TRUST_SHIFT)
            elif not voted and threat <= VOTE_TRUST_SAFE:
                observer.suspicion[voter.id] = clamp100(cur + VOTE_TRUST_SHIFT)


def apply_proscribe_trust(state: GameState, princeps: Player, target: Player) -> None:
    """Proscribing someone who looked suspicious earns the Princeps trust,
    scaled by how suspicious THAT OBSERVER privately rated the target."""
    for observer in state.alive_players():
        if observer.id in (princeps.id, target.id):
            continue
        target_susp = get_effective_suspicion(observer, target.id)
        if target_susp <= SUSPICION_START:
            continue
        shift = PROSCRIBE_TRUST_MAX_SHIFT * ((target_susp - SUSPICION_START) / (100 - SUSPICION_START))
        cur = observer.suspicion.get(princeps.id, SUSPICION_START)
        observer.suspicion[princeps.id] = clamp100(cur - shift)


# ---------------------------------------------------------------------------
# Decisions
# ---------------------------------------------------------------------------

def choose(items: list):
    return random.choice(items)


def forced_enactment_hurts_team(bot: Player, two: list) -> bool:
    """If a veto on THIS hand were refused, would the forced enactment hurt
    `bot`'s own team? Senate is only ever forced by IM+IM; Conspiracy/
    Imperator only ever forced by RP+RP."""
    has_rp, has_im = "RP" in two, "IM" in two
    return (not has_rp) if bot.team == "senate" else (not has_im)


def eligible_consuls(state: GameState) -> list:
    alive = state.alive_players()
    n = len(alive)
    out = []
    for p in alive:
        if p.id == state.current_princeps().id:
            continue
        if n <= 4:
            out.append(p)  # small Senates: term limits waived
            continue
        if p.id == state.previous_consul_id:
            continue
        if n > 5 and p.id == state.previous_princeps_id:
            continue
        out.append(p)
    return out


def bot_choose_nominee(bot: Player, state: GameState) -> int:
    elig = eligible_consuls(state)
    if not elig:
        return None
    if not smart_roll(bot, state):
        # Unskilled still isn't random against your own side.
        if bot.team == "conspiracy":
            allies = known_allies(bot, state.small_game())
            ally_choices = [p for p in elig if p.id in allies]
            if ally_choices:
                return choose(ally_choices).id
        return choose(elig).id

    if bot.team == "conspiracy":
        allies = known_allies(bot, state.small_game())
        ally_choices = [p for p in elig if p.id in allies]
        if ally_choices:
            return choose(ally_choices).id

    test_pool = test_candidates(bot, elig, state)
    if test_pool and random.random() < TEST_PROBABILITY:
        return choose(test_pool).id

    if bot.team == "senate":
        min_s = min(get_effective_suspicion(bot, p.id) for p in elig)
        safest = [p for p in elig if get_effective_suspicion(bot, p.id) <= min_s + 8]
        return choose(safest).id
    return choose(elig).id


def bot_vote_choice(bot: Player, nominee_id: int, state: GameState) -> bool:
    if not smart_roll(bot, state):
        # No suspicion-reading, but still a team-appropriate default —
        # never a coin flip that would read as "secretly knows better".
        if bot.team == "conspiracy":
            return random.random() < 0.90
        return random.random() < NAIVE_SENATE_AYE_PROBABILITY

    nominee_susp = get_effective_suspicion(bot, nominee_id)
    princeps_susp = get_effective_suspicion(bot, state.current_princeps().id)
    threat = (nominee_susp + princeps_susp) / 2

    if bot.team == "conspiracy":
        if conspirator_plays_cover(bot, state):
            return True  # an unexplained Nay against a safe pairing is itself a tell
        if (rp_progress(state) >= CONSPIRACY_OBSTRUCTION_RP_PROGRESS and
                threat <= CONSPIRACY_OBSTRUCTION_TRUST_THRESHOLD):
            if random.random() < CONSPIRACY_OBSTRUCTION_PROBABILITY:
                return False
        return True

    threshold = SENATE_NAY_THRESHOLD
    if in_caution(state):
        threshold = SENATE_NAY_THRESHOLD_CAUTION_LARGE if state.count >= 7 else SENATE_NAY_THRESHOLD_CAUTION
    return threat < threshold


def bot_discard_choice(bot: Player, three: list, state: GameState) -> int:
    """Which of 3 drawn cards to discard. NEVER skill-gated — every bot
    always favors its own team here. Skill only gates whether a
    Conspirator plays a COVER card instead (see conspirator_plays_cover)."""
    if bot.team == "senate":
        return three.index("IM") if "IM" in three else 0
    if conspirator_plays_cover(bot, state):
        return three.index("IM") if "IM" in three else 0
    return three.index("RP") if "RP" in three else 0


def bot_enact_choice(bot: Player, two: list, state: GameState) -> int:
    if bot.team == "senate":
        return two.index("RP") if "RP" in two else 0
    if conspirator_plays_cover(bot, state):
        return two.index("RP") if "RP" in two else 0
    return two.index("IM") if "IM" in two else 0


def conspirator_plays_cover(bot: Player, state: GameState) -> bool:
    """Whether a Conspirator plays OUT OF CHARACTER (the Senate-favored
    card) this turn to bank trust. Gated by cost first (is it even worth
    it right now?), then by skill (does this bot recognize the opening?).
    A 'sleeper' just starts with a higher baseline appetite for it — this
    is a per-turn probability, never a fixed role trait alone."""
    if in_caution(state):
        return False
    if im_progress(state) >= RUSH_IM_PROGRESS:
        return False
    if state.rp >= EMERGENCY_RP:
        return False
    exposed_count = len(alive_exposers(bot, state))
    if exposed_count > 0 and random.random() < (SLEEPER_LATE_DEFECT_CHANCE + SLEEPER_EXPOSED_DEFECT_BONUS * exposed_count):
        return False
    if average_public_suspicion(bot, state) >= SLEEPER_COVER_THRESHOLD:
        return False
    if random.random() < SLEEPER_LATE_DEFECT_CHANCE:
        return False
    base_chance = SLEEPER_BASE_COVER_CHANCE if bot.sleeper else NON_SLEEPER_BASE_COVER_CHANCE
    return random.random() < base_chance * skill_prob(bot, state)


def consul_values_cover(bot: Player, state: GameState) -> bool:
    """A Conspiracy Consul weighing whether a COVER VETO (declining a free
    Imperium to look innocent) is worth proposing right now."""
    if in_caution(state):
        return False
    if im_progress(state) >= RUSH_IM_PROGRESS:
        return False
    if state.rp >= EMERGENCY_RP:
        return False
    if alive_exposers(bot, state):
        return False
    if average_public_suspicion(bot, state) >= SLEEPER_COVER_THRESHOLD:
        return False
    return True


def bot_veto_choice(bot: Player, two: list, proposer_id: Optional[int], state: GameState) -> bool:
    """Whether to accept a proposed veto. Reading your own hand for
    "does refusing this force something my team doesn't want" is never
    skill-gated; skill only gates the one genuinely strategic extra: extending
    trust to a KNOWN ally's veto request even when it wasn't strictly forced.
    """
    proposer = state.by_id(proposer_id) if proposer_id is not None else None
    if not smart_roll(bot, state):
        return forced_enactment_hurts_team(bot, two)
    if (proposer and bot.team != "senate" and proposer.team != "senate" and
            proposer.id in known_allies(bot, state.small_game()) and
            not forced_enactment_hurts_team(proposer, two)):
        return True
    return forced_enactment_hurts_team(bot, two)


def bot_investigate_choice(bot: Player, state: GameState) -> int:
    pool_all = [p for p in state.alive_players() if p.id != bot.id]
    if not smart_roll(bot, state):
        return choose(pool_all).id
    allies = known_allies(bot, state.small_game())
    base = [p for p in pool_all if p.id not in allies and p.id not in bot.intel]
    if not base:
        base = [p for p in pool_all if p.id not in allies]
    if not base:
        base = pool_all
    # Spend it where it teaches the most: whoever sits closest to 50/50.
    best = sorted(base, key=lambda p: abs(get_effective_suspicion(bot, p.id) - 50))
    closest = abs(get_effective_suspicion(bot, best[0].id) - 50)
    tied = [p for p in best if abs(get_effective_suspicion(bot, p.id) - 50) <= closest + 5]
    return choose(tied).id


def bot_proscribe_choice(bot: Player, state: GameState) -> int:
    allies = known_allies(bot, state.small_game())
    pool = [p for p in state.alive_players() if p.id != bot.id and p.id not in allies]
    if not pool:
        pool = [p for p in state.alive_players() if p.id != bot.id]
    if not smart_roll(bot, state):
        return choose(pool).id

    witnesses = [p for p in pool if p.id in bot.exposed_to]
    if witnesses:
        return choose(witnesses).id

    enemies = [p for p in pool if bot.intel.get(p.id) == "enemy"]
    if enemies:
        return choose(enemies).id

    if bot.team == "conspiracy":
        min_s = min(get_effective_suspicion(bot, p.id) for p in pool)
        band = PROSCRIBE_TIE_BAND
        if state.small_game() and not known_allies(bot, True):
            band += FRIENDLY_FIRE_HEDGE_BAND
        trusted = [p for p in pool if get_effective_suspicion(bot, p.id) <= min_s + band]
        return choose(narrow_by_nay_votes(trusted, want_fewest=True)).id

    urgent_margin = URGENT_IM_MARGIN_SMALL if state.small_game() else URGENT_IM_MARGIN
    urgent = (state.im_win_threshold() - state.im) <= urgent_margin
    tie_band = URGENT_PROSCRIBE_TIE_BAND if urgent else PROSCRIBE_TIE_BAND
    max_s = max(get_effective_suspicion(bot, p.id) for p in pool)
    top = [p for p in pool if get_effective_suspicion(bot, p.id) >= max_s - tie_band]
    return choose(narrow_by_nay_votes(top, want_fewest=False)).id
