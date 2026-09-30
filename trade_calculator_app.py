"""
Sleeper Dynasty Trade Scout — Streamlit app (season-agnostic)

Scans every dynasty league you're in on Sleeper, values rostered players and future
picks with KeepTradeCut (ktc_values.csv in this repo), and suggests trades that:
  * are close in value after format + consolidation adjustments,
  * improve YOUR starting lineup without weakening your partner's,
  * leave both teams able to field a legal starting lineup.

Run locally:  streamlit run trade_calculator_app.py
Deploy:       Streamlit Community Cloud, main file = trade_calculator_app.py
requirements.txt needs:  streamlit  and  requests
"""

import csv
import io
import os
import re
import time
import unicodedata
from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from datetime import date
from itertools import combinations

import requests
import streamlit as st

# =============================================================================
# DEFAULT SETTINGS — the sidebar starts from these; edit to change the defaults.
# =============================================================================
SLEEPER_USERNAME = ""                 # pre-fills the username box
KTC_CSV_PATH = "ktc_values.csv"       # in the repo next to this file
VALUE_TOLERANCE_PCT = 7.0             # max gap between sides, % of the larger side (adjusted)
MAX_PLAYERS_PER_SIDE = 3              # assets (players + picks) per side
MIN_MY_LINEUP_GAIN = 250              # my starting lineup must improve by at least this
CONSOLIDATION_EXPONENT = 1.5          # side value = (sum v^p)^(1/p); 1.0 = off
ONE_QB_QB_MULTIPLIER = 0.60           # QB multiplier in 1QB leagues (CSV values are superflex)
TE_PREMIUM_BOOST_PER_POINT = 0.25     # TE multiplier = 1 + this * bonus_rec_te
INCLUDE_KEEPER_LEAGUES = False        # Sleeper settings.type: 0 redraft, 1 keeper, 2 dynasty

# --- Advanced (not in the sidebar) ---------------------------------------------
MIN_ASSET_VALUE = 800                 # assets below this are never trade pieces
TOP_PLAYERS_PER_TEAM = 10             # each team's N most valuable players are considered
TOP_PICKS_PER_TEAM = 4                # ...plus N most valuable picks
MIN_PIECE_SHARE = 0.20                # every piece in a package must be >= 20% of the best piece
MIN_PARTNER_LINEUP_CHANGE = 0         # partner's lineup change must be >= this
WEAK_SPOT_THRESHOLD = 0.90            # "weak" = starters < 90% of league average at that position
COMPLEXITY_PENALTY = 300              # ranking penalty per asset beyond a 1-for-1
MAX_APPEARANCES_PER_ASSET = 2         # any asset appears in at most N suggestions per league
MAX_SUGGESTIONS_PER_LEAGUE = 10
MAX_SUGGESTIONS_PER_PARTNER = 3
PICK_YEARS_AHEAD = 3                  # picks through current season + N
PICK_TIER_METHOD = "roster_value"     # or "mid"
PLAYERS_CACHE_HOURS = 24              # /players/nfl is ~5 MB; refresh at most daily
LEAGUE_CACHE_MINUTES = 10             # Sleeper league/roster responses are reused this long
REQUEST_TIMEOUT = 20
API_RETRIES = 3
# =============================================================================

SLEEPER_BASE = "https://api.sleeper.app/v1"
SKILL_POSITIONS = ("QB", "RB", "WR", "TE")
SLOT_ELIGIBILITY = {
    "QB": ("QB",), "RB": ("RB",), "WR": ("WR",), "TE": ("TE",),
    "WRRB_FLEX": ("RB", "WR"), "REC_FLEX": ("WR", "TE"),
    "FLEX": ("RB", "WR", "TE"), "SUPER_FLEX": ("QB", "RB", "WR", "TE"),
}
NON_STARTER_SLOTS = {"BN", "IR", "TAXI"}
POS_COLOR = {"QB": "red", "RB": "green", "WR": "blue", "TE": "orange", "PICK": "gray"}


@dataclass
class Config:
    """Settings chosen in the sidebar for one scan."""
    tolerance_pct: float = VALUE_TOLERANCE_PCT
    max_per_side: int = MAX_PLAYERS_PER_SIDE
    min_my_gain: float = MIN_MY_LINEUP_GAIN
    consolidation: float = CONSOLIDATION_EXPONENT
    one_qb_mult: float = ONE_QB_QB_MULTIPLIER
    te_boost: float = TE_PREMIUM_BOOST_PER_POINT
    include_keeper: bool = INCLUDE_KEEPER_LEAGUES


def ordinal(n):
    return "%d%s" % (n, "tsnrhtdd"[(n // 10 % 10 != 1) * (n % 10 < 4) * n % 10::4])


# =============================================================================
# Sleeper API (cached so Streamlit reruns don't re-hit Sleeper)
# =============================================================================
class SleeperError(Exception):
    pass


_session = requests.Session()
_session.headers["User-Agent"] = "sleeper-trade-scout/2.1"


def _fetch(path):
    """GET with retries. Returns parsed JSON (None for 404/null); raises SleeperError on failure."""
    url = f"{SLEEPER_BASE}{path}"
    last = None
    for attempt in range(1, API_RETRIES + 1):
        try:
            resp = _session.get(url, timeout=REQUEST_TIMEOUT)
            if resp.status_code == 404:
                return None
            if resp.status_code == 429 or resp.status_code >= 500:
                raise requests.HTTPError(f"HTTP {resp.status_code}")
            resp.raise_for_status()
            return resp.json()
        except (requests.RequestException, ValueError) as exc:
            last = exc
            if attempt < API_RETRIES:
                time.sleep(1.5 * attempt)
    raise SleeperError(f"{path}: {last}")


# Failures raise, and Streamlit never caches exceptions, so a failed call is retried next time.
@st.cache_data(ttl=LEAGUE_CACHE_MINUTES * 60, show_spinner=False)
def _cached_fetch(path):
    return _fetch(path)


def api(path, default=None):
    try:
        data = _cached_fetch(path)
    except SleeperError:
        return default
    return default if data is None else data


def resolve_season(override):
    """Current season from /state/nfl (league_season, falling back to season)."""
    if override:
        return str(override), [], "your override"
    state = api("/state/nfl", {}) or {}
    primary = state.get("league_season") or state.get("season")
    if primary:
        alternates = [str(s) for s in (state.get("season"), state.get("previous_season"))
                      if s and str(s) != str(primary)]
        return str(primary), alternates, f"Sleeper (week {state.get('week')}, {state.get('season_type')})"
    return str(date.today().year), [], "calendar year (Sleeper state unavailable)"


@st.cache_data(ttl=PLAYERS_CACHE_HOURS * 3600, show_spinner="Downloading the Sleeper player list (once a day)…")
def load_players_db():
    """Sleeper players database, slimmed to QB/RB/WR/TE: {pid: (name, position, team)}."""
    raw = _fetch("/players/nfl")        # raises on failure, so a failure isn't cached
    slim = {}
    for pid, p in (raw or {}).items():
        if not isinstance(p, dict) or p.get("position") not in SKILL_POSITIONS:
            continue
        name = p.get("full_name") or " ".join(x for x in (p.get("first_name"), p.get("last_name")) if x) or pid
        slim[pid] = (name, p["position"], p.get("team") or "")
    return slim


# =============================================================================
# KTC values & name matching
# =============================================================================
_SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "v"}
_PICK_RE = re.compile(r"^\s*(\d{4})\s+(early|mid|late)\s+(\d+)(?:st|nd|rd|th)\s*$", re.I)


def name_tokens(name):
    """lowercase, strip accents, periods, apostrophes, hyphens, trailing Jr./Sr./II/III/IV/V."""
    s = unicodedata.normalize("NFKD", str(name or "")).encode("ascii", "ignore").decode("ascii").lower()
    s = s.replace("-", " ")
    s = re.sub(r"[^a-z0-9\s]", "", s)
    tokens = s.split()
    while len(tokens) > 1 and tokens[-1] in _SUFFIXES:
        tokens.pop()
    return tokens


def name_key(name):
    return "".join(name_tokens(name))   # "A.J. Brown" == "AJ Brown"


def initial_key(name):
    t = name_tokens(name)
    return f"{t[0][0]}|{t[-1]}" if len(t) >= 2 else None


class KTCValues:
    def __init__(self, csv_text):
        self.players = {}      # name_key -> (csv_name, value)
        self.picks = {}        # (season, tier, round) -> value
        self._by_initial = {}
        self.fallback = {}
        self._cache = {}
        reader = csv.DictReader(io.StringIO(csv_text.lstrip("\ufeff")))
        cols = {c.strip().lower(): c for c in (reader.fieldnames or [])}
        name_col, value_col = cols.get("player_sleeper"), cols.get("ktc_value")
        if not name_col or not value_col:
            raise ValueError(f"CSV needs Player_Sleeper and KTC_Value columns (found {reader.fieldnames}).")
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
        """First-initial + last-name fallback only when exactly one CSV name fits and that
        name isn't already an exact match for another Sleeper player."""
        claimed = {name_key(v[0]) for v in players_db.values()}
        self.fallback = {ik: keys[0] for ik, keys in self._by_initial.items()
                         if len(keys) == 1 and keys[0] not in claimed}
        self._cache.clear()

    def match_player(self, pid, name):
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
    def __init__(self):
        self.unmatched = {}   # pid -> (name, pos, team)
        self.fallback = {}    # pid -> (sleeper_name, csv_name)


# =============================================================================
# League model
# =============================================================================
class Asset:
    __slots__ = ("id", "name", "pos", "nfl_team", "ktc", "value", "kind", "lineup_eligible", "detail")

    def __init__(self, id, name, pos, nfl_team, ktc, value, kind, lineup_eligible, detail=""):
        self.id, self.name, self.pos, self.nfl_team = id, name, pos, nfl_team
        self.ktc, self.value, self.kind, self.lineup_eligible = ktc, value, kind, lineup_eligible
        self.detail = detail


class LeagueFormat:
    def __init__(self, league, cfg):
        settings = league.get("settings") or {}
        scoring = league.get("scoring_settings") or {}
        rp = [str(s) for s in (league.get("roster_positions") or [])]
        starters = [s for s in rp if s not in NON_STARTER_SLOTS]
        self.cfg = cfg
        self.league_type = int(settings.get("type") or 0)
        self.best_ball = bool(settings.get("best_ball"))
        self.num_teams = int(league.get("total_rosters") or 0)
        self.starter_count = len(starters)
        self.roster_limit = len([s for s in rp if s not in ("IR", "TAXI")]) or 999
        self.qb_slots = starters.count("QB")
        self.has_sf = "SUPER_FLEX" in starters
        self.superflex = self.has_sf or self.qb_slots >= 2
        self.tep = float(scoring.get("bonus_rec_te") or 0)
        self.ppr = float(scoring.get("rec") or 0)
        self.draft_rounds = int(settings.get("draft_rounds") or 4)
        skill = [s for s in starters if s in SLOT_ELIGIBILITY]
        # Lineup fill order: dedicated slots first, then flex slots narrowest -> widest.
        self.slot_order = sorted(skill, key=lambda s: len(SLOT_ELIGIBILITY[s]))
        self.flex_eligibility = {p: [s for s in skill if p in SLOT_ELIGIBILITY[s]] for p in SKILL_POSITIONS}

    def multiplier(self, pos):
        if pos == "QB" and not self.superflex:
            return self.cfg.one_qb_mult
        if pos == "TE" and self.tep > 0:
            return 1 + self.cfg.te_boost * self.tep
        return 1.0

    def describe(self):
        qb = "Superflex" if self.has_sf else ("2QB" if self.qb_slots >= 2 else "1QB")
        ppr = {1.0: "PPR", 0.5: "Half PPR", 0.0: "Standard"}.get(self.ppr, f"{self.ppr:g} PPR")
        parts = [f"{self.num_teams}-team dynasty", qb, ppr]
        if self.tep:
            parts.append(f"{self.tep:g} TE premium")
        parts.append(f"starts {self.starter_count}")
        if self.roster_limit < 999:
            parts.append(f"{self.roster_limit}-player roster")
        if self.best_ball:
            parts.append("best ball")
        return ", ".join(parts)

    def adjustment_note(self):
        parts = []
        if not self.superflex:
            parts.append(f"QBs ×{self.cfg.one_qb_mult:g} (1QB league; KTC values are superflex)")
        if self.tep:
            parts.append(f"TEs ×{self.multiplier('TE'):g} for TE premium")
        if self.cfg.consolidation != 1:
            parts.append(f"consolidation strength {self.cfg.consolidation:g}")
        return "; ".join(parts)


class Team:
    def __init__(self, roster_id, owner_id, co_owners, label, is_orphan):
        self.roster_id, self.owner_id, self.co_owners = roster_id, owner_id, co_owners
        self.label, self.is_orphan = label, is_orphan
        self.players = {}
        self.picks = []
        self.active_count = 0
        self.pos_lists = {p: [] for p in SKILL_POSITIONS}
        self.base_total = 0.0
        self.base_empty = 0
        self.base_by_pos = {}
        self.starters = set()


# =============================================================================
# Lineup logic
# =============================================================================
def best_lineup(pos_lists, slot_order, want_starters=False):
    """Greedy best lineup: each slot (dedicated first, then flex narrow -> wide) takes the
    highest-value eligible player left. 0-value players still fill slots (legality)."""
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
    """Lineup lists with `outgoing` removed and `incoming` players added (only changed positions rebuilt)."""
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
    """Weakest starter a new player at each position could replace. Incoming packages with
    nobody above this can't improve my lineup, so they're skipped early (conservative prune)."""
    th = {}
    for pos in SKILL_POSITIONS:
        slots = fmt.flex_eligibility[pos]
        if not slots:
            th[pos] = float("inf")
        elif team.base_empty:
            th[pos] = -1.0
        else:
            elig = {p for s in slots for p in SLOT_ELIGIBILITY[s]}
            vals = [v for p in elig for v, pid in team.pos_lists[p] if pid in team.starters]
            th[pos] = min(vals) if vals else -1.0
    return th


# =============================================================================
# Trade search
# =============================================================================
def side_value(values, p):
    """Consolidation: (sum v^p)^(1/p). One player keeps full value; two 4,000s ≈ 6,350 at p=1.5,
    so the side receiving more pieces must send more raw value. Throw-ins add almost nothing."""
    if p == 1:
        return float(sum(values))
    return sum(v ** p for v in values) ** (1.0 / p)


def build_combos(team, cfg):
    """1..max_per_side packages from a team's tradeable assets, sorted by adjusted value.
    Unmatched (0-value) players are never tradeable."""
    players = sorted((a for a in team.players.values() if a.value >= MIN_ASSET_VALUE),
                     key=lambda a: a.value, reverse=True)[:TOP_PLAYERS_PER_TEAM]
    picks = sorted((a for a in team.picks if a.value >= MIN_ASSET_VALUE),
                   key=lambda a: a.value, reverse=True)[:TOP_PICKS_PER_TEAM]
    pool = sorted(players + picks, key=lambda a: a.value, reverse=True)
    combos = []
    for n in range(1, cfg.max_per_side + 1):
        for combo in combinations(pool, n):
            if n > 1 and combo[-1].value < MIN_PIECE_SHARE * combo[0].value:
                continue
            combos.append((side_value([a.value for a in combo], cfg.consolidation), combo))
    combos.sort(key=lambda c: c[0])
    return combos


def active_after(team, outgoing, n_incoming_players):
    out_active = sum(1 for a in outgoing if a.kind == "player" and a.lineup_eligible)
    return team.active_count - out_active + n_incoming_players


def find_trades(me, partner, fmt, my_combos, my_thresholds, cfg):
    tol = cfg.tolerance_pct / 100.0
    my_effs = [c[0] for c in my_combos]
    results = []
    for get_eff, get in build_combos(partner, cfg):
        if not any(a.kind == "player" and a.value > my_thresholds[a.pos] for a in get):
            continue
        # Value window: |give - get| <= tol * max(give, get)
        lo, hi = get_eff * (1 - tol), get_eff / (1 - tol)
        for give_eff, give in my_combos[bisect_left(my_effs, lo):bisect_right(my_effs, hi)]:
            my_total, my_empty, _, _ = best_lineup(lists_after_trade(me.pos_lists, give, get), fmt.slot_order)
            my_gain = my_total - me.base_total
            if my_empty > me.base_empty or my_gain < cfg.min_my_gain:
                continue
            their_total, their_empty, _, _ = best_lineup(
                lists_after_trade(partner.pos_lists, get, give), fmt.slot_order)
            their_gain = their_total - partner.base_total
            if their_empty > partner.base_empty or their_gain < MIN_PARTNER_LINEUP_CHANGE:
                continue
            diff = get_eff - give_eff    # + favors me
            # Rank: combined lineup gain, lightly penalizing value gaps and extra pieces.
            score = my_gain + their_gain - 0.25 * abs(diff) - COMPLEXITY_PENALTY * (len(give) + len(get) - 2)
            results.append({"partner": partner, "give": give, "get": get, "give_eff": give_eff,
                            "get_eff": get_eff, "diff": diff, "my_gain": my_gain,
                            "their_gain": their_gain, "score": score})
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


def _upgrade_phrase(incoming, weak):
    pos = _positions(incoming)
    if not pos:
        return "add future picks"
    hit = [p for p in pos if p in weak]
    return f"shore up a weak {'/'.join(hit)} spot" if hit else f"upgrade at {'/'.join(pos)}"


def build_reason(t, me, league_avg):
    partner = t["partner"]
    you = (f"You {_upgrade_phrase(t['get'], weak_positions(me, league_avg))} "
           f"(+{t['my_gain']:,.0f} lineup) using {_outgoing_phrase(t['give'], me)}")
    if t["their_gain"] > 0:
        they = (f"they {_upgrade_phrase(t['give'], weak_positions(partner, league_avg))} "
                f"(+{t['their_gain']:,.0f} lineup) using {_outgoing_phrase(t['get'], partner)}")
    else:
        incoming = " + ".join(x for x in ("/".join(_positions(t["give"])),
                                          "future picks" if any(a.kind == "pick" for a in t["give"]) else "") if x)
        they = f"they turn {_outgoing_phrase(t['get'], partner)} into {incoming} without weakening their lineup"
    return f"{you}; {they}."


def build_flags(t, me, fmt):
    flags = []
    ng, nr = len(t["give"]), len(t["get"])
    if ng > nr:
        flags.append("You're consolidating, so you pay the premium in extra value")
    elif nr > ng:
        flags.append("They're consolidating, so they pay the premium in extra pieces")
    mine = active_after(me, t["give"], sum(1 for a in t["get"] if a.kind == "player"))
    theirs = active_after(t["partner"], t["get"], sum(1 for a in t["give"] if a.kind == "player"))
    if mine > fmt.roster_limit:
        flags.append(f"You'd need to cut {mine - fmt.roster_limit}")
    if theirs > fmt.roster_limit:
        flags.append(f"They'd need to cut {theirs - fmt.roster_limit}")
    return flags


# =============================================================================
# League processing
# =============================================================================
def process_league(league, user_id, season, players_db, ktc, tracker, cfg):
    name = league.get("name") or league.get("league_id")
    lid = league.get("league_id")
    fmt = LeagueFormat(league, cfg)

    # League type from Sleeper settings.type: 0 redraft, 1 keeper, 2 dynasty.
    if fmt.league_type != 2 and not (fmt.league_type == 1 and cfg.include_keeper):
        kind = {0: "redraft", 1: "keeper"}.get(fmt.league_type, f"type {fmt.league_type}")
        return {"skipped": f"{name}: {kind} league"}
    if not fmt.slot_order:
        return {"skipped": f"{name}: no QB/RB/WR/TE starting slots"}

    users = api(f"/league/{lid}/users", []) or []
    rosters = api(f"/league/{lid}/rosters", []) or []
    traded = api(f"/league/{lid}/traded_picks", []) or []
    if not rosters:
        return {"skipped": f"{name}: couldn't load rosters"}
    fmt.num_teams = fmt.num_teams or len(rosters)

    user_info = {}
    for u in users:
        team_name = ((u.get("metadata") or {}).get("team_name") or "").strip()
        disp = u.get("display_name") or u.get("username") or u.get("user_id")
        user_info[u.get("user_id")] = f"{disp} ({team_name})" if team_name and team_name != disp else disp

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
        pids = [str(p) for p in (r.get("players") or []) if p]        # players can be null
        benched = {str(p) for p in (r.get("reserve") or [])} | {str(p) for p in (r.get("taxi") or [])}
        team.active_count = sum(1 for p in pids if p not in benched)
        for pid in pids:
            info = players_db.get(pid)
            if not info:
                continue                                                # K/DEF/IDP aren't in the slim list
            pname, pos, nfl = info
            raw, csv_name, method = ktc.match_player(pid, pname)
            if method is None:
                tracker.unmatched[pid] = (pname, pos, nfl)
            elif method == "initial":
                tracker.fallback[pid] = (pname, csv_name)
            value = raw * fmt.multiplier(pos)
            eligible = pid not in benched                               # IR/taxi can't start
            team.players[pid] = Asset(pid, pname, pos, nfl, raw, value, "player", eligible)
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
    league_avg = {p: sum(t.base_by_pos[p] for t in active_teams) / max(1, len(active_teams)) for p in SKILL_POSITIONS}

    # ---- Draft picks: next season (or this one if its rookie draft hasn't run) through +N years.
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

    # Nearest pick year: tier by the ORIGINAL team's lineup-value rank (weakest third = Early).
    ranked = sorted(teams, key=lambda t: t.base_total)
    n = len(ranked)
    tier_by_rid = {t.roster_id: ("Early" if i < n / 3 else "Late" if i >= 2 * n / 3 else "Mid")
                   for i, t in enumerate(ranked)}
    for (y, rnd, orig), cur in owner_of.items():
        if cur not in by_rid:
            continue
        tier = tier_by_rid.get(orig, "Mid") if (PICK_TIER_METHOD == "roster_value" and int(y) == first_year) else "Mid"
        value = ktc.picks.get((y, tier, rnd), 0)
        src = "own" if orig == cur else (f"via {by_rid[orig].label.split(' (')[0]}" if orig in by_rid else f"via #{orig}")
        by_rid[cur].picks.append(Asset(f"pick:{y}:{rnd}:{orig}", f"{y} {ordinal(rnd)}", "PICK", None,
                                       value, value, "pick", False, detail=f"{src}, projected {tier}"))
    for t in teams:
        t.picks.sort(key=lambda a: (a.name, -a.value))

    # ---- Search every partner
    my_combos = build_combos(me, cfg)
    thresholds = improvement_thresholds(me, fmt)
    candidates = []
    for partner in teams:
        if partner is me or partner.is_orphan or not partner.players:
            continue                                     # orphans can't accept trades
        candidates.extend(find_trades(me, partner, fmt, my_combos, thresholds, cfg))

    # ---- Keep the list varied
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
            "weak": weak_positions(me, league_avg), "suggestions": chosen}


def run_scan(username, season_override, league_filter, ktc, cfg, progress):
    """Whole scan. Returns a dict for the UI; errors come back as {'error': message}."""
    user = api(f"/user/{username}", None)
    if not isinstance(user, dict) or not user.get("user_id"):
        return {"error": f"No Sleeper account found for “{username}”. Use your Sleeper username "
                         f"(not your display name), and check that Sleeper is reachable."}
    user_id = user["user_id"]

    season, alternates, source = resolve_season(season_override)
    progress(0.05, f"Season {season} from {source}. Loading your leagues…")
    leagues = api(f"/user/{user_id}/leagues/nfl/{season}", []) or []
    for alt in alternates:                              # offseason before leagues renew
        if leagues:
            break
        leagues = api(f"/user/{user_id}/leagues/nfl/{alt}", []) or []
        if leagues:
            season = alt
    if not leagues:
        return {"error": f"No Sleeper leagues found for {username} in {season}."}
    if league_filter:
        wanted = league_filter.lower()
        leagues = [lg for lg in leagues if wanted in (lg.get("name") or "").lower()]
        if not leagues:
            return {"error": f"None of your leagues have “{league_filter}” in the name."}

    progress(0.1, "Loading the Sleeper player list…")
    try:
        players_db = load_players_db()
    except SleeperError as exc:
        return {"error": f"Couldn't download the Sleeper player list ({exc}). Try again in a minute."}
    ktc.build_fallback(players_db)

    tracker, results, skipped = MatchTracker(), [], []
    for i, lg in enumerate(leagues):
        progress(0.12 + 0.86 * i / len(leagues), f"Scanning {lg.get('name', 'league')} ({i + 1} of {len(leagues)})…")
        try:
            res = process_league(lg, user_id, season, players_db, ktc, tracker, cfg)
        except Exception as exc:                        # one bad league never stops the scan
            skipped.append(f"{lg.get('name', lg.get('league_id'))}: error ({type(exc).__name__}: {exc})")
            continue
        if "skipped" in res:
            skipped.append(res["skipped"])
        else:
            results.append(res)

    missing = sorted({str(y) for y in range(int(season) + 1, int(season) + PICK_YEARS_AHEAD + 1)}
                     - set(ktc.pick_years()))
    if missing:
        skipped.append(f"Note: the KTC file has no pick values for {', '.join(missing)}, so those picks aren't traded.")
    progress(1.0, f"Done. Scanned {len(results)} dynasty league{'s' if len(results) != 1 else ''} for {season}.")
    return {"season": season, "results": results, "skipped": skipped,
            "unmatched": dict(tracker.unmatched), "fallback": dict(tracker.fallback)}


def suggestions_csv(results):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["league", "partner", "you_give", "you_get", "give_ktc", "get_ktc", "give_adjusted",
                "get_adjusted", "adjusted_diff", "my_lineup_gain", "their_lineup_gain", "reason", "notes"])
    for res in results:
        for t in res["suggestions"]:
            nm = lambda a: f"{a.name} ({a.detail})" if a.kind == "pick" else a.name
            w.writerow([res["name"], t["partner"].label, "; ".join(nm(a) for a in t["give"]),
                        "; ".join(nm(a) for a in t["get"]), sum(a.ktc for a in t["give"]),
                        sum(a.ktc for a in t["get"]), round(t["give_eff"]), round(t["get_eff"]),
                        round(t["diff"]), round(t["my_gain"]), round(t["their_gain"]),
                        t["reason"], "; ".join(t["flags"])])
    return buf.getvalue()


# =============================================================================
# Streamlit UI
# =============================================================================
@st.cache_data(show_spinner=False)
def parse_ktc(csv_text):
    return KTCValues(csv_text)


def read_repo_csv():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), KTC_CSV_PATH)
    if not os.path.exists(path):
        path = KTC_CSV_PATH
    with open(path, encoding="utf-8-sig") as fh:
        return fh.read()


def asset_line(a):
    color = POS_COLOR.get(a.pos, "gray")
    tag = "PICK" if a.kind == "pick" else a.pos
    sub = a.detail if a.kind == "pick" else (a.nfl_team or "FA")
    return f":{color}[**{tag}**] {a.name} · {a.ktc:,}  \n<small style='opacity:.7'>{sub}</small>"


def render_trade(t, tol_pct):
    give_raw, get_raw = sum(a.ktc for a in t["give"]), sum(a.ktc for a in t["get"])
    pct = 100 * t["diff"] / max(t["give_eff"], t["get_eff"])
    lean = "even" if abs(pct) < 0.05 else (f"{abs(pct):.1f}% your way" if pct > 0 else f"{abs(pct):.1f}% their way")
    with st.container(border=True):
        st.markdown(f"#### {t['partner'].label}  \n<small style='opacity:.7'>{len(t['give'])}-for-{len(t['get'])}</small>",
                    unsafe_allow_html=True)
        c1, c2 = st.columns(2)
        with c1:
            st.markdown(":red[**You give**]")
            for a in t["give"]:
                st.markdown(asset_line(a), unsafe_allow_html=True)
            st.caption(f"Adjusted {t['give_eff']:,.0f} · KTC {give_raw:,}")
        with c2:
            st.markdown(":green[**You get**]")
            for a in t["get"]:
                st.markdown(asset_line(a), unsafe_allow_html=True)
            st.caption(f"Adjusted {t['get_eff']:,.0f} · KTC {get_raw:,}")
        # Balance bar: 0 = far in their favor, 1 = far in yours; the bar spans ±2× tolerance.
        st.progress(min(1.0, max(0.0, 0.5 + pct / (4 * tol_pct))),
                    text=f"Adjusted difference {t['diff']:+,.0f} ({lean}); raw KTC {get_raw - give_raw:+,}")
        st.markdown(t["reason"])
        if t["flags"]:
            st.caption(". ".join(t["flags"]) + ".")


def render_league(res, tol_pct):
    me, fmt = res["me"], res["fmt"]
    st.subheader(res["name"], divider="gray")
    st.caption(fmt.describe())
    c1, c2, c3 = st.columns(3)
    c1.metric("Lineup rank", f"#{res['rank']} of {res['teams']}", help=f"Starting lineup value {me.base_total:,.0f}")
    c2.metric("Weak spots", ", ".join(sorted(res["weak"])) or "None")
    c3.metric("Picks owned", len(me.picks))
    if me.base_empty:
        st.warning(f"You can't fill {me.base_empty} starting slot(s) right now.")
    if me.picks:
        with st.expander("Your picks"):
            st.markdown("\n".join(f"- {p.name} ({p.detail}){'' if p.value else ' — no KTC value'}" for p in me.picks))
    note = fmt.adjustment_note()
    if note:
        st.caption(f"Adjustments: {note}.")
    if not res["suggestions"]:
        st.info("No trades here passed the value, lineup and roster checks. "
                "Try a wider value tolerance or a lower minimum lineup gain in the sidebar.")
    for t in res["suggestions"]:
        render_trade(t, tol_pct)


def main():
    st.set_page_config(page_title="Trade Scout", page_icon="🏈", layout="centered")
    st.title("Trade Scout")
    st.caption("Dynasty trade ideas across your Sleeper leagues, valued with KeepTradeCut "
               "and checked against both teams' starting lineups.")

    # ---- Sidebar settings
    with st.sidebar:
        st.header("Settings")
        tol = st.slider("Value tolerance (%)", 1.0, 20.0, VALUE_TOLERANCE_PCT, 0.5,
                        help="Max gap between the two sides after adjustments.")
        max_side = st.select_slider("Max assets per side", options=[1, 2, 3], value=MAX_PLAYERS_PER_SIDE)
        min_gain = st.number_input("Minimum lineup gain", 0, 20000, MIN_MY_LINEUP_GAIN, 50,
                                   help="How much your starting lineup must improve.")
        cons = st.slider("Consolidation strength", 1.0, 3.0, CONSOLIDATION_EXPONENT, 0.1,
                         help="1 = off. Higher makes 2-for-1s cost the side getting more pieces.")
        qb_mult = st.slider("QB value in 1QB leagues", 0.2, 1.0, ONE_QB_QB_MULTIPLIER, 0.05,
                            help="The KTC file holds superflex values.")
        te_boost = st.slider("TE premium boost", 0.0, 1.0, TE_PREMIUM_BOOST_PER_POINT, 0.05,
                             help="Per point of TE bonus (0.25 at 0.5 TEP = +12.5%).")
        league_filter = st.text_input("Only leagues named", placeholder="blank = all")
        season_override = st.text_input("Season override", placeholder="auto from Sleeper")
        include_keeper = st.checkbox("Include keeper leagues", INCLUDE_KEEPER_LEAGUES)
        st.divider()
        upload = st.file_uploader("Use a different KTC CSV", type="csv",
                                  help="Columns: Player_Sleeper, KTC_Value. Replaces the repo file for this session.")

    # ---- KTC values
    try:
        csv_text = upload.getvalue().decode("utf-8-sig") if upload else read_repo_csv()
        ktc = parse_ktc(csv_text)
    except FileNotFoundError:
        st.error(f"{KTC_CSV_PATH} isn't in the repo next to this file. Add it or upload one in the sidebar.")
        st.stop()
    except (ValueError, UnicodeDecodeError) as exc:
        st.error(f"Couldn't read the KTC CSV: {exc}")
        st.stop()
    years = ktc.pick_years()
    st.caption(f"Values: {'uploaded file' if upload else KTC_CSV_PATH} — {len(ktc.players)} players, "
               f"{len(ktc.picks)} picks{(' (' + years[0] + '–' + years[-1] + ')') if years else ''}")

    # ---- Scan
    with st.form("scan"):
        username = st.text_input("Sleeper username", value=st.session_state.get("username", SLEEPER_USERNAME))
        go = st.form_submit_button("Find trades", type="primary", width="stretch")

    cfg = Config(tol, int(max_side), float(min_gain), float(cons), float(qb_mult), float(te_boost), include_keeper)
    if go:
        if not username.strip():
            st.warning("Enter your Sleeper username.")
            st.stop()
        st.session_state["username"] = username.strip()
        bar = st.progress(0.0, text="Finding your Sleeper account…")
        scan = run_scan(username.strip(), season_override.strip(), league_filter.strip(), ktc, cfg,
                        lambda frac, msg: bar.progress(min(1.0, frac), text=msg))
        bar.empty()
        scan["tol"] = tol
        st.session_state["scan_result"] = scan

    scan = st.session_state.get("scan_result")
    if not scan:
        st.info("Enter your Sleeper username and tap **Find trades**. Settings are in the sidebar (› at top left on phones).")
        return
    if "error" in scan:
        st.error(scan["error"])
        return

    st.success(f"Scanned {len(scan['results'])} dynasty league(s) for {scan['season']}.")
    if any(r["suggestions"] for r in scan["results"]):
        st.download_button("Download suggestions (CSV)", suggestions_csv(scan["results"]),
                           file_name="trade_suggestions.csv", mime="text/csv", width="stretch")
    for res in scan["results"]:
        render_league(res, scan["tol"])

    st.subheader("Value check", divider="gray")
    if scan["skipped"]:
        with st.expander(f"Skipped leagues and notes ({len(scan['skipped'])})"):
            st.markdown("\n".join(f"- {s}" for s in scan["skipped"]))
    if scan["fallback"]:
        with st.expander(f"Matched by first initial + last name ({len(scan['fallback'])}) — confirm these"):
            st.markdown("\n".join(f"- {s} → {c}" for s, c in sorted(scan["fallback"].values())))
    if scan["unmatched"]:
        with st.expander(f"Rostered players with no KTC value ({len(scan['unmatched'])})"):
            st.caption("Valued at 0. They still count toward lineups, but trades are never built on them.")
            for pos in SKILL_POSITIONS:
                names = sorted(f"{n} ({t or 'FA'})" for n, p, t in scan["unmatched"].values() if p == pos)
                if names:
                    st.markdown(f"**{pos}:** " + ", ".join(names))
    else:
        st.caption("Every rostered QB, RB, WR and TE matched a KTC value.")


main()
