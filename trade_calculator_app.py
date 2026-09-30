#!/usr/bin/env python3
"""
Sleeper Dynasty Trade Scout (season-agnostic rebuild of trade_calculator_multiplayer_test.py)

Scans every dynasty league you're in on Sleeper, values every rostered player and
future draft pick with KeepTradeCut (ktc_values.csv), and suggests trades that:
  * are close in value after format + consolidation adjustments,
  * improve YOUR starting lineup without weakening your partner's,
  * leave both teams able to field a legal starting lineup.

Requirements: Python 3.8+, `pip install requests`
Run:          python sleeper_trade_scout.py            (uses SETTINGS below)
              python sleeper_trade_scout.py --help     (command-line overrides)
"""

import argparse
import csv
import json
import os
import re
import sys
import time
import unicodedata
from bisect import bisect_left, bisect_right
from datetime import date
from itertools import combinations

import requests

# =============================================================================
# SETTINGS — edit these. Every one can also be overridden on the command line.
# =============================================================================
SLEEPER_USERNAME = ""                 # your Sleeper username; if blank you'll be prompted
KTC_CSV_PATH = "ktc_values.csv"       # columns: Player_Sleeper, KTC_Value
VALUE_TOLERANCE_PCT = 7.0             # max gap between the two sides, % of the larger side (adjusted values)
MAX_PLAYERS_PER_SIDE = 3              # max assets (players + picks) on each side of a trade
SEASON_OVERRIDE = None                # e.g. "2026" for testing; None = ask Sleeper's /state/nfl

# --- Value adjustments --------------------------------------------------------
# ktc_values.csv holds SUPERFLEX values (Josh Allen 9999, Caleb Williams top-10),
# so QBs are discounted in 1QB leagues instead of boosted in superflex.
ONE_QB_QB_MULTIPLIER = 0.60           # QB value multiplier in 1QB leagues
TE_PREMIUM_BOOST_PER_POINT = 0.25     # TE multiplier = 1 + this * bonus_rec_te (0.5 TEP -> +12.5%)

# Consolidation: a side's value is (sum of v^p)^(1/p) instead of a plain sum.
# p = 1.0 turns it off. p = 1.5 means two 4,000 players count as ~6,350 and a
# 500-value throw-in adds almost nothing to an 8,000 player. So the side that
# receives MORE pieces must send more raw KTC value to balance the deal.
CONSOLIDATION_EXPONENT = 1.5

# --- Search limits (speed vs. coverage) ----------------------------------------
MIN_ASSET_VALUE = 800                 # assets below this (adjusted) are never used as trade pieces
TOP_PLAYERS_PER_TEAM = 10             # only each team's N most valuable players are considered
TOP_PICKS_PER_TEAM = 4                # ...plus their N most valuable picks
MIN_PIECE_SHARE = 0.20                # in multi-asset sides, every piece must be >= 20% of the best piece
COMPLEXITY_PENALTY = 300              # ranking penalty per asset beyond a 1-for-1 (favors simpler deals)
MAX_APPEARANCES_PER_ASSET = 2         # any one player/pick appears in at most N suggestions per league
MIN_MY_LINEUP_GAIN = 250              # my starting lineup must improve by at least this much
MIN_PARTNER_LINEUP_CHANGE = 0         # partner's lineup change must be >= this (0 = can't get worse)
WEAK_SPOT_THRESHOLD = 0.90            # a position is "weak" if its starters are < 90% of league average

# --- Picks ----------------------------------------------------------------------
PICK_YEARS_AHEAD = 3                  # model picks through current season + N
PICK_TIER_METHOD = "roster_value"     # "roster_value": next year's picks tiered Early/Mid/Late by the
                                      # ORIGINAL owner's starting-lineup value rank; later years = Mid.
                                      # "mid": every pick valued as Mid.

# --- Leagues & output -----------------------------------------------------------
INCLUDE_KEEPER_LEAGUES = False        # Sleeper settings.type: 0 = redraft, 1 = keeper, 2 = dynasty
MAX_SUGGESTIONS_PER_LEAGUE = 10
MAX_SUGGESTIONS_PER_PARTNER = 3
EXPORT_CSV_PATH = "trade_suggestions.csv"   # None to skip the export

# --- Plumbing -------------------------------------------------------------------
PLAYERS_CACHE_PATH = "sleeper_players_cache.json"
PLAYERS_CACHE_MAX_AGE_HOURS = 24      # /players/nfl is ~5 MB; Sleeper asks for at most one call per day
REQUEST_TIMEOUT = 20
API_RETRIES = 3
# =============================================================================

SLEEPER_BASE = "https://api.sleeper.app/v1"
SKILL_POSITIONS = ("QB", "RB", "WR", "TE")
# Which positions can fill each Sleeper starting slot. Slots not listed here
# (K, DEF, IDP) are ignored: trades only move QB/RB/WR/TE and picks, so those slots never change.
SLOT_ELIGIBILITY = {
    "QB": ("QB",), "RB": ("RB",), "WR": ("WR",), "TE": ("TE",),
    "WRRB_FLEX": ("RB", "WR"), "REC_FLEX": ("WR", "TE"),
    "FLEX": ("RB", "WR", "TE"), "SUPER_FLEX": ("QB", "RB", "WR", "TE"),
}
NON_STARTER_SLOTS = {"BN", "IR", "TAXI"}
PICK_TIERS = ("Early", "Mid", "Late")


def warn(msg):
    print(f"  ! {msg}", file=sys.stderr)


def ordinal(n):
    return "%d%s" % (n, "tsnrhtdd"[(n // 10 % 10 != 1) * (n % 10 < 4) * n % 10::4])


# =============================================================================
# Sleeper API
# =============================================================================
_session = requests.Session()
_session.headers["User-Agent"] = "sleeper-trade-scout/2.0"


def sleeper_get(path, default=None):
    """GET a Sleeper endpoint. Retries on timeouts/429/5xx; returns `default` on failure or null body."""
    url = f"{SLEEPER_BASE}{path}"
    for attempt in range(1, API_RETRIES + 1):
        try:
            resp = _session.get(url, timeout=REQUEST_TIMEOUT)
            if resp.status_code == 404:
                return default
            if resp.status_code == 429 or resp.status_code >= 500:
                raise requests.HTTPError(f"HTTP {resp.status_code}")
            resp.raise_for_status()
            data = resp.json()
            return default if data is None else data
        except (requests.RequestException, ValueError) as exc:
            if attempt == API_RETRIES:
                warn(f"Sleeper API call failed: {path} ({exc})")
                return default
            time.sleep(1.5 * attempt)
    return default


def resolve_season(override):
    """Current season from /state/nfl (league_season, falling back to season). Returns (season, alternates, note)."""
    if override:
        return str(override), [], "override"
    state = sleeper_get("/state/nfl", {}) or {}
    league_season = state.get("league_season")
    season = state.get("season")
    primary = league_season or season
    if primary:
        alternates = [str(s) for s in (season, state.get("previous_season")) if s and str(s) != str(primary)]
        note = f"Sleeper /state/nfl (season_type={state.get('season_type')}, week={state.get('week')})"
        return str(primary), alternates, note
    fallback = str(date.today().year)
    warn(f"Couldn't read /state/nfl; falling back to calendar year {fallback}.")
    return fallback, [], "calendar-year fallback"


def load_players_db(force_refresh=False):
    """Sleeper players database, cached locally and refreshed at most once per PLAYERS_CACHE_MAX_AGE_HOURS."""
    path = PLAYERS_CACHE_PATH
    is_fresh = (os.path.exists(path)
                and time.time() - os.path.getmtime(path) < PLAYERS_CACHE_MAX_AGE_HOURS * 3600)
    if is_fresh and not force_refresh:
        try:
            with open(path, encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, ValueError):
            warn("Players cache unreadable; re-downloading.")

    print("Downloading Sleeper players database (cached for 24h)...")
    raw = sleeper_get("/players/nfl", None)
    if isinstance(raw, dict) and raw:
        # Keep only the fields we use: shrinks the cache from ~5 MB to well under 1 MB.
        keep = ("full_name", "first_name", "last_name", "position", "team")
        slim = {pid: {k: p.get(k) for k in keep} for pid, p in raw.items() if isinstance(p, dict)}
        tmp = path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(slim, fh)
            os.replace(tmp, path)
        except OSError as exc:
            warn(f"Couldn't write players cache ({exc}); continuing without it.")
        return slim

    if os.path.exists(path):
        warn("Players download failed; using the stale cache.")
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    warn("No players database available; player names can't be resolved.")
    return {}


def player_display_name(info, pid):
    name = (info or {}).get("full_name")
    if not name:
        name = " ".join(x for x in ((info or {}).get("first_name"), (info or {}).get("last_name")) if x)
    return name or str(pid)


# =============================================================================
# KTC values & name matching
# =============================================================================
_SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "v"}
_PICK_RE = re.compile(r"^\s*(\d{4})\s+(early|mid|late)\s+(\d+)(?:st|nd|rd|th)\s*$", re.I)


def name_tokens(name):
    """lowercase, strip accents, periods, apostrophes, hyphens, trailing Jr./Sr./II/III/IV/V, extra spaces."""
    s = unicodedata.normalize("NFKD", str(name or "")).encode("ascii", "ignore").decode("ascii").lower()
    s = s.replace("-", " ")
    s = re.sub(r"[^a-z0-9\s]", "", s)          # periods, apostrophes, commas, etc.
    tokens = s.split()
    while len(tokens) > 1 and tokens[-1] in _SUFFIXES:
        tokens.pop()
    return tokens


def name_key(name):
    # Joined without spaces so "A.J. Brown" == "AJ Brown" == "A J Brown", "Smith-Njigba" == "Smith Njigba".
    return "".join(name_tokens(name))


def initial_key(name):
    t = name_tokens(name)
    return f"{t[0][0]}|{t[-1]}" if len(t) >= 2 else None


class KTCValues:
    def __init__(self, path):
        self.players = {}      # name_key -> (csv_name, value)
        self.picks = {}        # (season "2027", tier "Early", round 1) -> value
        self._by_initial = {}  # initial_key -> [name_key, ...]
        self.fallback = {}     # initial_key -> name_key (only when unambiguous and unclaimed)
        self._cache = {}       # sleeper pid -> (value, csv_name, method)

        if not os.path.exists(path):
            sys.exit(f"KTC file not found: {path}")
        with open(path, newline="", encoding="utf-8-sig") as fh:
            reader = csv.DictReader(fh)
            cols = {c.strip().lower(): c for c in (reader.fieldnames or [])}
            name_col, value_col = cols.get("player_sleeper"), cols.get("ktc_value")
            if not name_col or not value_col:
                sys.exit(f"{path} needs Player_Sleeper and KTC_Value columns (found {reader.fieldnames}).")
            for row in reader:
                name = (row.get(name_col) or "").strip()
                try:
                    value = int(float(str(row.get(value_col, "")).replace(",", "")))
                except ValueError:
                    continue
                if not name:
                    continue
                m = _PICK_RE.match(name)
                if m:
                    self.picks[(m.group(1), m.group(2).title(), int(m.group(3)))] = value
                    continue
                key = name_key(name)
                if key in self.players and self.players[key][1] >= value:
                    continue
                self.players[key] = (name, value)
                ik = initial_key(name)
                if ik:
                    self._by_initial.setdefault(ik, []).append(key)

    def build_fallback(self, players_db):
        """First-initial + last-name fallback, used only when it can't grab the wrong player:
        exactly one CSV name has that initial+last, and that CSV name doesn't exactly match
        some other Sleeper player (i.e., it isn't already 'claimed')."""
        claimed = {name_key(player_display_name(p, pid)) for pid, p in players_db.items()
                   if (p or {}).get("position") in SKILL_POSITIONS}
        self.fallback = {ik: keys[0] for ik, keys in self._by_initial.items()
                         if len(keys) == 1 and keys[0] not in claimed}

    def match_player(self, pid, name):
        """Returns (value, csv_name, method) where method is 'exact', 'initial' or None."""
        if pid in self._cache:
            return self._cache[pid]
        result = (0, None, None)
        key = name_key(name)
        if key in self.players:
            csv_name, value = self.players[key]
            result = (value, csv_name, "exact")
        else:
            ik = initial_key(name)
            if ik in self.fallback:
                csv_name, value = self.players[self.fallback[ik]]
                result = (value, csv_name, "initial")
        self._cache[pid] = result
        return result

    def pick_years(self):
        return sorted({k[0] for k in self.picks})


class MatchTracker:
    """Collects unmatched / fallback-matched rostered players across all scanned leagues."""
    def __init__(self):
        self.unmatched = {}   # pid -> {name,pos,team,leagues}
        self.fallback = {}    # pid -> (sleeper_name, csv_name)

    def add_unmatched(self, pid, name, pos, team, league):
        rec = self.unmatched.setdefault(pid, {"name": name, "pos": pos, "team": team or "FA", "leagues": set()})
        rec["leagues"].add(league)

    def add_fallback(self, pid, sleeper_name, csv_name):
        self.fallback[pid] = (sleeper_name, csv_name)


# =============================================================================
# League model
# =============================================================================
class Asset:
    __slots__ = ("id", "name", "pos", "nfl_team", "ktc", "value", "kind", "lineup_eligible")

    def __init__(self, id, name, pos, nfl_team, ktc, value, kind, lineup_eligible):
        self.id, self.name, self.pos, self.nfl_team = id, name, pos, nfl_team
        self.ktc, self.value, self.kind, self.lineup_eligible = ktc, value, kind, lineup_eligible

    def label(self):
        if self.kind == "pick":
            return f"{self.name} [{self.ktc:,}]"
        return f"{self.name} ({self.pos}, {self.nfl_team or 'FA'}) [{self.ktc:,}]"


class LeagueFormat:
    """Reads roster_positions / settings / scoring_settings once per league."""
    def __init__(self, league):
        settings = league.get("settings") or {}
        scoring = league.get("scoring_settings") or {}
        rp = [str(s) for s in (league.get("roster_positions") or [])]
        starters = [s for s in rp if s not in NON_STARTER_SLOTS]

        self.league_type = int(settings.get("type") or 0)
        self.best_ball = bool(settings.get("best_ball"))
        self.num_teams = int(league.get("total_rosters") or 0)
        self.starter_count = len(starters)
        self.roster_limit = len([s for s in rp if s not in ("IR", "TAXI")]) or 999
        self.qb_slots = starters.count("QB")
        self.superflex = "SUPER_FLEX" in starters or self.qb_slots >= 2
        self.tep = float(scoring.get("bonus_rec_te") or 0)
        self.ppr = float(scoring.get("rec") or 0)
        self.draft_rounds = int(settings.get("draft_rounds") or 4)
        # Lineup fill order: dedicated slots first, then flex slots from narrowest to widest.
        skill = [s for s in starters if s in SLOT_ELIGIBILITY]
        self.slot_order = sorted(skill, key=lambda s: len(SLOT_ELIGIBILITY[s]))
        self.flex_eligibility = {p: [s for s in skill if p in SLOT_ELIGIBILITY[s]] for p in SKILL_POSITIONS}

    def multiplier(self, pos):
        if pos == "QB" and not self.superflex:
            return ONE_QB_QB_MULTIPLIER
        if pos == "TE" and self.tep > 0:
            return 1 + TE_PREMIUM_BOOST_PER_POINT * self.tep
        return 1.0

    def describe(self):
        qb = "Superflex" if "SUPER_FLEX" in self.slot_order else ("2QB" if self.qb_slots >= 2 else "1QB")
        ppr = {1.0: "PPR", 0.5: "Half PPR", 0.0: "Standard"}.get(self.ppr, f"{self.ppr:g} PPR")
        parts = [f"{self.num_teams}-team", qb, ppr]
        if self.tep:
            parts.append(f"{self.tep:g} TEP")
        parts.append(f"start {self.starter_count}")
        if self.roster_limit < 999:
            parts.append(f"{self.roster_limit}-man roster")
        if self.best_ball:
            parts.append("best ball")
        return ", ".join(parts)


class Team:
    def __init__(self, roster_id, owner_id, co_owners, label, is_orphan):
        self.roster_id, self.owner_id, self.co_owners = roster_id, owner_id, co_owners
        self.label, self.is_orphan = label, is_orphan
        self.players = {}          # pid -> Asset (skill players only, including 0-value)
        self.picks = []            # Assets
        self.active_count = 0      # players counting toward the roster limit (not IR/taxi)
        self.pos_lists = {p: [] for p in SKILL_POSITIONS}  # lineup-eligible (value, pid) sorted desc
        self.base_total = 0.0
        self.base_empty = 0
        self.base_by_pos = {}
        self.starters = set()


# =============================================================================
# Lineup logic
# =============================================================================
def best_lineup(pos_lists, slot_order, want_starters=False):
    """Greedy best starting lineup: fill each slot (dedicated first, then flex narrow -> wide)
    with the highest-value eligible player still available. Unmatched (0-value) players still
    fill slots, so they count toward lineup legality.
    Returns (total_value, empty_slots, value_by_position, starter_ids)."""
    ptr = {p: 0 for p in SKILL_POSITIONS}
    total, empty = 0.0, 0
    by_pos = {p: 0.0 for p in SKILL_POSITIONS}
    starters = set()
    for slot in slot_order:
        best_pos, best_val = None, -1.0
        for p in SLOT_ELIGIBILITY[slot]:
            lst, i = pos_lists[p], ptr[p]
            if i < len(lst) and lst[i][0] > best_val:
                best_pos, best_val = p, lst[i][0]
        if best_pos is None:
            empty += 1
            continue
        if want_starters:
            starters.add(pos_lists[best_pos][ptr[best_pos]][1])
        ptr[best_pos] += 1
        total += best_val
        by_pos[best_pos] += best_val
    return total, empty, by_pos, starters


def lists_after_trade(pos_lists, outgoing, incoming):
    """Copy of a team's lineup lists with `outgoing` removed and `incoming` players added.
    Only the positions that change are rebuilt (keeps the search fast)."""
    new = dict(pos_lists)
    out_by_pos, in_by_pos = {}, {}
    for a in outgoing:
        if a.kind == "player":
            out_by_pos.setdefault(a.pos, set()).add(a.id)
    for a in incoming:
        if a.kind == "player":
            in_by_pos.setdefault(a.pos, []).append((a.value, a.id))
    for pos in set(out_by_pos) | set(in_by_pos):
        removed = out_by_pos.get(pos, ())
        lst = [e for e in pos_lists[pos] if e[1] not in removed] + in_by_pos.get(pos, [])
        lst.sort(reverse=True)
        new[pos] = lst
    return new


def improvement_thresholds(team, fmt):
    """For each position: the weakest current starter that a new player at that position could
    replace. A received player at or below this can't improve the lineup, so combos made only
    of such players are skipped before the (more expensive) lineup evaluation."""
    # Conservative: uses every starter at any position sharing a slot with `pos`, so it can only
    # under-prune (never wrongly skip a trade that would help).
    th = {}
    for pos in SKILL_POSITIONS:
        slots = fmt.flex_eligibility[pos]
        if not slots:
            th[pos] = float("inf")          # position can't start in this league
        elif team.base_empty:
            th[pos] = -1.0                   # an empty slot: anyone helps
        else:
            eligible_starter_positions = {p for s in slots for p in SLOT_ELIGIBILITY[s]}
            vals = [v for p in eligible_starter_positions for v, pid in team.pos_lists[p] if pid in team.starters]
            th[pos] = min(vals) if vals else -1.0
    return th


# =============================================================================
# Trade search
# =============================================================================
def side_value(values):
    """Consolidation-adjusted side value (see CONSOLIDATION_EXPONENT)."""
    p = CONSOLIDATION_EXPONENT
    if p == 1:
        return float(sum(values))
    return sum(v ** p for v in values) ** (1.0 / p)


def build_combos(team):
    """All 1..MAX_PLAYERS_PER_SIDE packages from a team's tradeable assets, sorted by adjusted value.
    Unmatched (0-value) players are never tradeable, so no suggestion is built on them."""
    players = sorted((a for a in team.players.values() if a.value >= MIN_ASSET_VALUE),
                     key=lambda a: a.value, reverse=True)[:TOP_PLAYERS_PER_TEAM]
    picks = sorted((a for a in team.picks if a.value >= MIN_ASSET_VALUE),
                   key=lambda a: a.value, reverse=True)[:TOP_PICKS_PER_TEAM]
    pool = sorted(players + picks, key=lambda a: a.value, reverse=True)
    combos = []
    for n in range(1, MAX_PLAYERS_PER_SIDE + 1):
        for combo in combinations(pool, n):          # pool is sorted, so combo[0] is the best piece
            if n > 1 and combo[-1].value < MIN_PIECE_SHARE * combo[0].value:
                continue                              # skip junk throw-ins
            vals = [a.value for a in combo]
            combos.append((side_value(vals), combo))
    combos.sort(key=lambda c: c[0])
    return combos


def active_after(team, outgoing, n_incoming_players):
    out_active = sum(1 for a in outgoing if a.kind == "player" and a.lineup_eligible)
    return team.active_count - out_active + n_incoming_players


def find_trades(me, partner, fmt, my_combos, my_thresholds, tol):
    """Every trade with `partner` that passes value, lineup-gain, and legality checks."""
    my_effs = [c[0] for c in my_combos]
    results = []
    for get_eff, get in build_combos(partner):
        # Fast prune: at least one incoming player must beat a current starter of mine.
        if not any(a.kind == "player" and a.value > my_thresholds[a.pos] for a in get):
            continue
        # Value window: |give - get| <= tol * max(give, get)
        lo, hi = get_eff * (1 - tol), get_eff / (1 - tol)
        for give_eff, give in my_combos[bisect_left(my_effs, lo):bisect_right(my_effs, hi)]:
            # --- my side: lineup must improve and stay legal
            my_lists = lists_after_trade(me.pos_lists, give, get)
            my_total, my_empty, _, _ = best_lineup(my_lists, fmt.slot_order)
            my_gain = my_total - me.base_total
            if my_empty > me.base_empty or my_gain < MIN_MY_LINEUP_GAIN:
                continue
            # --- their side: lineup can't get worse (by default) and must stay legal
            their_lists = lists_after_trade(partner.pos_lists, get, give)
            their_total, their_empty, _, _ = best_lineup(their_lists, fmt.slot_order)
            their_gain = their_total - partner.base_total
            if their_empty > partner.base_empty or their_gain < MIN_PARTNER_LINEUP_CHANGE:
                continue
            diff = get_eff - give_eff             # + = in my favor
            # Rank: total lineup improvement across both teams, lightly penalizing value gaps
            # and extra pieces (simpler deals are easier to get accepted).
            pieces = len(give) + len(get)
            score = my_gain + their_gain - 0.25 * abs(diff) - COMPLEXITY_PENALTY * (pieces - 2)
            results.append({
                "partner": partner, "give": give, "get": get,
                "give_eff": give_eff, "get_eff": get_eff, "diff": diff,
                "my_gain": my_gain, "their_gain": their_gain, "score": score,
            })
    return results


# =============================================================================
# Reasons & flags
# =============================================================================
def weak_positions(team, league_avg):
    return {p for p in SKILL_POSITIONS
            if league_avg.get(p, 0) > 0 and team.base_by_pos.get(p, 0) < WEAK_SPOT_THRESHOLD * league_avg[p]}


def _positions(assets):
    seen = []
    for a in assets:
        if a.kind == "player" and a.pos not in seen:
            seen.append(a.pos)
    return seen


def _outgoing_phrase(assets, team):
    parts = []
    pos = _positions(assets)
    if pos:
        bench_only = all(a.id not in team.starters for a in assets if a.kind == "player")
        parts.append(f"surplus {'/'.join(pos)} depth" if bench_only else "/".join(pos))
    if any(a.kind == "pick" for a in assets):
        parts.append("future picks")
    return " + ".join(parts)


def _upgrade_phrase(incoming, team, weak):
    pos = _positions(incoming)
    if not pos:
        return "add future picks"
    weak_hit = [p for p in pos if p in weak]
    if weak_hit:
        return f"shore up a weak {'/'.join(weak_hit)} spot"
    return f"upgrade at {'/'.join(pos)}"


def build_reason(t, me, league_avg):
    partner = t["partner"]
    my_weak, their_weak = weak_positions(me, league_avg), weak_positions(partner, league_avg)
    you = (f"you {_upgrade_phrase(t['get'], me, my_weak)} (+{t['my_gain']:,.0f} lineup) "
           f"using {_outgoing_phrase(t['give'], me)}")
    if t["their_gain"] > 0:
        they = (f"they {_upgrade_phrase(t['give'], partner, their_weak)} (+{t['their_gain']:,.0f} lineup) "
                f"using {_outgoing_phrase(t['get'], partner)}")
    else:
        incoming = "/".join(_positions(t["give"])) or ""
        if any(a.kind == "pick" for a in t["give"]):
            incoming = (incoming + " + " if incoming else "") + "future picks"
        they = f"they turn {_outgoing_phrase(t['get'], partner)} into {incoming} without weakening their lineup"
    return f"{you}; {they}."


def build_flags(t, me, fmt):
    flags = []
    ng, nr = len(t["give"]), len(t["get"])
    if ng > nr:
        flags.append(f"{ng}-for-{nr}: you consolidate, so you pay the consolidation premium")
    elif nr > ng:
        flags.append(f"{ng}-for-{nr}: they consolidate, so they pay the premium in extra pieces")
    my_players_in = sum(1 for a in t["get"] if a.kind == "player")
    their_players_in = sum(1 for a in t["give"] if a.kind == "player")
    mine = active_after(me, t["give"], my_players_in)
    theirs = active_after(t["partner"], t["get"], their_players_in)
    if mine > fmt.roster_limit:
        flags.append(f"you'd need to cut {mine - fmt.roster_limit}")
    if theirs > fmt.roster_limit:
        flags.append(f"they'd need to cut {theirs - fmt.roster_limit}")
    return flags


# =============================================================================
# League processing
# =============================================================================
def process_league(league, user_id, season, players_db, ktc, tracker, tol):
    name = league.get("name") or league.get("league_id")
    lid = league.get("league_id")
    fmt = LeagueFormat(league)

    # ---- League type: Sleeper settings.type (0 redraft, 1 keeper, 2 dynasty)
    if fmt.league_type != 2 and not (fmt.league_type == 1 and INCLUDE_KEEPER_LEAGUES):
        kind = {0: "redraft", 1: "keeper"}.get(fmt.league_type, f"type {fmt.league_type}")
        return {"skipped": f"{name}: {kind} league"}
    if not fmt.slot_order:
        return {"skipped": f"{name}: no QB/RB/WR/TE starting slots"}

    users = sleeper_get(f"/league/{lid}/users", []) or []
    rosters = sleeper_get(f"/league/{lid}/rosters", []) or []
    traded = sleeper_get(f"/league/{lid}/traded_picks", []) or []
    if not rosters:
        return {"skipped": f"{name}: couldn't load rosters"}
    fmt.num_teams = fmt.num_teams or len(rosters)

    user_info = {}
    for u in users:
        team_name = ((u.get("metadata") or {}).get("team_name") or "").strip()
        disp = u.get("display_name") or u.get("username") or u.get("user_id")
        user_info[u.get("user_id")] = f"{disp} ({team_name})" if team_name and team_name != disp else disp

    # ---- Teams & player values
    teams = []
    for r in rosters:
        try:
            rid = int(r.get("roster_id"))
        except (TypeError, ValueError):
            continue
        owner = r.get("owner_id")
        co = [c for c in (r.get("co_owners") or []) if c]
        label = user_info.get(owner) or (f"Orphaned team #{rid}" if not owner else f"User {owner}")
        team = Team(rid, owner, co, label, is_orphan=not owner)

        pids = [str(p) for p in (r.get("players") or []) if p]          # players can be null
        benched = {str(p) for p in (r.get("reserve") or [])} | {str(p) for p in (r.get("taxi") or [])}
        team.active_count = sum(1 for p in pids if p not in benched)
        for pid in pids:
            info = players_db.get(pid)
            pos = (info or {}).get("position")
            if pos not in SKILL_POSITIONS:
                continue                                                   # K/DEF/IDP: not valued
            pname = player_display_name(info, pid)
            raw, csv_name, method = ktc.match_player(pid, pname)
            if method is None:
                tracker.add_unmatched(pid, pname, pos, info.get("team"), name)
            elif method == "initial":
                tracker.add_fallback(pid, pname, csv_name)
            value = raw * fmt.multiplier(pos)
            eligible = pid not in benched                                  # IR/taxi can't start
            team.players[pid] = Asset(pid, pname, pos, info.get("team"), raw, value, "player", eligible)
            if eligible:
                team.pos_lists[pos].append((value, pid))
        for lst in team.pos_lists.values():
            lst.sort(reverse=True)
        team.base_total, team.base_empty, team.base_by_pos, team.starters = best_lineup(
            team.pos_lists, fmt.slot_order, want_starters=True)
        teams.append(team)

    me = next((t for t in teams if t.owner_id == user_id or user_id in t.co_owners), None)
    if me is None:
        return {"skipped": f"{name}: you don't own a roster here"}

    by_rid = {t.roster_id: t for t in teams}
    active_teams = [t for t in teams if t.players]
    league_avg = {p: sum(t.base_by_pos[p] for t in active_teams) / max(1, len(active_teams))
                  for p in SKILL_POSITIONS}

    # ---- Draft picks
    # Start at next season once this season's rookie draft is done (league status past "drafting").
    first_year = int(season) + (0 if league.get("status") in ("pre_draft", "drafting") else 1)
    last_year = int(season) + PICK_YEARS_AHEAD
    owner_of = {}
    for y in range(first_year, last_year + 1):
        for rnd in range(1, fmt.draft_rounds + 1):
            for rid in by_rid:
                owner_of[(str(y), rnd, rid)] = rid
    # traded_picks: roster_id = ORIGINAL owner, owner_id = CURRENT owner.
    for tp in traded:
        try:
            key = (str(tp.get("season")), int(tp.get("round")), int(tp.get("roster_id")))
            if key in owner_of and tp.get("owner_id") is not None:
                owner_of[key] = int(tp["owner_id"])
        except (TypeError, ValueError):
            continue

    # Tier for the nearest pick year: original team's lineup-value rank (weakest third = Early).
    ranked = sorted(teams, key=lambda t: t.base_total)
    n = len(ranked)
    tier_by_rid = {}
    for i, t in enumerate(ranked):
        tier_by_rid[t.roster_id] = "Early" if i < n / 3 else ("Late" if i >= 2 * n / 3 else "Mid")

    unvalued_picks = 0
    for (y, rnd, orig), cur in owner_of.items():
        if cur not in by_rid:
            continue
        tier = tier_by_rid.get(orig, "Mid") if (PICK_TIER_METHOD == "roster_value" and int(y) == first_year) else "Mid"
        value = ktc.picks.get((y, tier, rnd), 0)
        if value == 0:
            unvalued_picks += 1
        src = "own" if orig == cur else f"via {by_rid[orig].label.split(' (')[0]}" if orig in by_rid else f"via #{orig}"
        label = f"{y} {ordinal(rnd)} ({src}, proj. {tier})"
        by_rid[cur].picks.append(Asset(f"pick:{y}:{rnd}:{orig}", label, "PICK", None, value, value, "pick", False))
    for t in teams:
        t.picks.sort(key=lambda a: (a.id.split(":")[1], int(a.id.split(":")[2]), -a.value))

    # ---- Search every partner
    my_combos = build_combos(me)
    thresholds = improvement_thresholds(me, fmt)
    candidates = []
    for partner in teams:
        if partner is me or partner.is_orphan or not partner.players:
            continue                                   # orphans can't accept trades
        candidates.extend(find_trades(me, partner, fmt, my_combos, thresholds, tol))

    # ---- Pick the best, keeping the list varied:
    #   * one suggestion per (partner, headline piece I give, headline piece I get)
    #   * at most MAX_SUGGESTIONS_PER_PARTNER per partner
    #   * any single asset appears in at most MAX_APPEARANCES_PER_ASSET suggestions
    candidates.sort(key=lambda t: t["score"], reverse=True)
    chosen, seen, per_partner, appearances = [], set(), {}, {}
    for t in candidates:
        key = (t["partner"].roster_id, t["give"][0].id, t["get"][0].id)
        if key in seen or per_partner.get(t["partner"].roster_id, 0) >= MAX_SUGGESTIONS_PER_PARTNER:
            continue
        ids = [a.id for a in t["give"] + t["get"]]
        if any(appearances.get(i, 0) >= MAX_APPEARANCES_PER_ASSET for i in ids):
            continue
        for i in ids:
            appearances[i] = appearances.get(i, 0) + 1
        seen.add(key)
        per_partner[t["partner"].roster_id] = per_partner.get(t["partner"].roster_id, 0) + 1
        t["reason"] = build_reason(t, me, league_avg)
        t["flags"] = build_flags(t, me, fmt)
        chosen.append(t)
        if len(chosen) >= MAX_SUGGESTIONS_PER_LEAGUE:
            break

    rank = 1 + sum(1 for t in teams if t.base_total > me.base_total)
    return {"name": name, "fmt": fmt, "me": me, "rank": rank, "teams": len(teams),
            "weak": weak_positions(me, league_avg), "suggestions": chosen,
            "unvalued_picks": unvalued_picks, "evaluated": len(candidates)}


def adjustment_note(fmt):
    parts = []
    if not fmt.superflex:
        parts.append(f"QBs x{ONE_QB_QB_MULTIPLIER:g} (1QB; CSV values are superflex)")
    if fmt.tep:
        parts.append(f"TEs x{fmt.multiplier('TE'):g} ({fmt.tep:g} TEP)")
    if CONSOLIDATION_EXPONENT != 1:
        parts.append(f"consolidation exponent {CONSOLIDATION_EXPONENT:g}")
    return "; ".join(parts) or "none"


# =============================================================================
# Output
# =============================================================================
def raw_total(assets):
    return sum(a.ktc for a in assets)


def print_league(res):
    me, fmt = res["me"], res["fmt"]
    print("\n" + "=" * 100)
    print(f"{res['name']}  —  {fmt.describe()}")
    print("=" * 100)
    weak = ", ".join(sorted(res["weak"])) or "none"
    print(f"Your team: {me.label} | starting lineup value {me.base_total:,.0f} "
          f"(#{res['rank']} of {res['teams']}) | below-average spots: {weak}")
    if me.base_empty:
        print(f"  ! You currently can't fill {me.base_empty} starting slot(s).")
    picks = ", ".join(p.name.replace("proj. ", "") for p in me.picks) or "none"
    print(f"Your picks: {picks}")
    print(f"Value adjustments: {adjustment_note(fmt)}")
    if not res["suggestions"]:
        print("\n  No trades met the value, lineup-gain and legality rules. "
              "Try a higher --tolerance or lower MIN_MY_LINEUP_GAIN.")
        return
    for i, t in enumerate(res["suggestions"], 1):
        g_raw, r_raw = raw_total(t["give"]), raw_total(t["get"])
        pct = 100 * t["diff"] / max(t["give_eff"], t["get_eff"])
        print(f"\n  #{i}  Trade partner: {t['partner'].label}")
        print(f"      You give: {', '.join(a.label() for a in t['give'])}")
        print(f"      You get:  {', '.join(a.label() for a in t['get'])}")
        print(f"      KTC totals: you give {g_raw:,} | you get {r_raw:,} | raw difference {r_raw - g_raw:+,}")
        print(f"      Adjusted:   you give {t['give_eff']:,.0f} | you get {t['get_eff']:,.0f} "
              f"| difference {t['diff']:+,.0f} ({pct:+.1f}%; + favors you)")
        print(f"      Why it fits: {t['reason']}")
        if t["flags"]:
            print(f"      Notes: {'; '.join(t['flags'])}")


def print_match_report(tracker):
    print("\n" + "=" * 100)
    print("KTC MATCH REPORT")
    print("=" * 100)
    if tracker.fallback:
        print(f"Matched by first-initial + last-name fallback ({len(tracker.fallback)}) — verify these:")
        for sleeper_name, csv_name in sorted(tracker.fallback.values()):
            print(f"  {sleeper_name}  ->  {csv_name}")
    if not tracker.unmatched:
        print("Every rostered QB/RB/WR/TE matched a KTC value.")
        return
    print(f"Rostered QB/RB/WR/TE with no KTC value ({len(tracker.unmatched)}) — valued at 0 and never "
          f"used in suggestions:")
    for pos in SKILL_POSITIONS:
        recs = sorted((r for r in tracker.unmatched.values() if r["pos"] == pos), key=lambda r: r["name"])
        if recs:
            print(f"  {pos}: " + ", ".join(f"{r['name']} ({r['team']})" for r in recs))


def export_csv(path, results):
    rows = []
    for res in results:
        for t in res["suggestions"]:
            rows.append({
                "league": res["name"], "partner": t["partner"].label,
                "you_give": "; ".join(a.name for a in t["give"]),
                "you_get": "; ".join(a.name for a in t["get"]),
                "give_ktc": raw_total(t["give"]), "get_ktc": raw_total(t["get"]),
                "give_adjusted": round(t["give_eff"]), "get_adjusted": round(t["get_eff"]),
                "adjusted_diff": round(t["diff"]),
                "my_lineup_gain": round(t["my_gain"]), "their_lineup_gain": round(t["their_gain"]),
                "reason": t["reason"], "notes": "; ".join(t["flags"]),
            })
    if not rows:
        return
    try:
        with open(path, "w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)
        print(f"\nSaved {len(rows)} suggestions to {path}")
    except OSError as exc:
        warn(f"Couldn't write {path}: {exc}")


# =============================================================================
# Main
# =============================================================================
def parse_args():
    ap = argparse.ArgumentParser(description="Suggest dynasty trades across your Sleeper leagues using KTC values.")
    ap.add_argument("--username", default=SLEEPER_USERNAME, help="Sleeper username")
    ap.add_argument("--csv", default=KTC_CSV_PATH, help="path to ktc_values.csv")
    ap.add_argument("--season", default=SEASON_OVERRIDE, help="override the season (testing)")
    ap.add_argument("--tolerance", type=float, default=VALUE_TOLERANCE_PCT, help="value tolerance in %%")
    ap.add_argument("--max-per-side", type=int, default=MAX_PLAYERS_PER_SIDE, help="max assets per side")
    ap.add_argument("--league", action="append", default=[], help="only leagues whose name contains this (repeatable)")
    ap.add_argument("--export", default=EXPORT_CSV_PATH, help="CSV path for suggestions ('' to skip)")
    ap.add_argument("--refresh-players", action="store_true", help="force re-download of the players database")
    return ap.parse_args()


def main():
    global MAX_PLAYERS_PER_SIDE
    args = parse_args()
    MAX_PLAYERS_PER_SIDE = max(1, args.max_per_side)
    tol = max(0.0, min(args.tolerance, 50.0)) / 100.0

    username = (args.username or "").strip() or input("Sleeper username: ").strip()
    if not username:
        sys.exit("A Sleeper username is required.")

    ktc = KTCValues(args.csv)
    print(f"Loaded {len(ktc.players)} player values and {len(ktc.picks)} pick values "
          f"(pick years: {', '.join(ktc.pick_years()) or 'none'}) from {args.csv}")

    season, alternates, source = resolve_season(args.season)
    print(f"Season {season} (from {source})")

    user = sleeper_get(f"/user/{username}", None)
    if not isinstance(user, dict) or not user.get("user_id"):
        sys.exit(f"Sleeper user '{username}' not found (or the API is unreachable).")
    user_id = user["user_id"]

    leagues = sleeper_get(f"/user/{user_id}/leagues/nfl/{season}", []) or []
    for alt in alternates:                    # e.g. offseason before leagues renew
        if leagues:
            break
        leagues = sleeper_get(f"/user/{user_id}/leagues/nfl/{alt}", []) or []
        if leagues:
            print(f"No {season} leagues yet; using your {alt} leagues.")
            season = alt
    if not leagues:
        sys.exit(f"No Sleeper leagues found for {username} in {season}.")
    if args.league:
        wanted = [w.lower() for w in args.league]
        leagues = [lg for lg in leagues if any(w in (lg.get("name") or "").lower() for w in wanted)]

    wanted_years = {str(y) for y in range(int(season) + 1, int(season) + PICK_YEARS_AHEAD + 1)}
    missing = sorted(wanted_years - set(ktc.pick_years()))
    if missing:
        warn(f"CSV has no pick values for {', '.join(missing)}; those picks are valued at 0 and not traded.")

    players_db = load_players_db(args.refresh_players)
    ktc.build_fallback(players_db)

    tracker, results, skipped = MatchTracker(), [], []
    start = time.time()
    for lg in leagues:
        try:
            res = process_league(lg, user_id, season, players_db, ktc, tracker, tol)
        except Exception as exc:                              # one bad league never stops the run
            skipped.append(f"{lg.get('name', lg.get('league_id'))}: error ({type(exc).__name__}: {exc})")
            continue
        if "skipped" in res:
            skipped.append(res["skipped"])
            continue
        results.append(res)
        print_league(res)

    if skipped:
        print("\nSkipped leagues:")
        for s in skipped:
            print(f"  - {s}")
    print_match_report(tracker)
    if args.export:
        export_csv(args.export, results)
    print(f"\nDone: {len(results)} dynasty league(s) scanned in {time.time() - start:.1f}s.")


if __name__ == "__main__":
    main()
