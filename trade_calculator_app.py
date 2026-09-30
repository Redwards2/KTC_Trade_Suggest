"""
Sleeper Trade Calculator — Streamlit app (season-agnostic)

Pick a league, check one or more of your players, and see every 1-for-1 and 1-for-2 return
available from the other teams in that league, valued with KeepTradeCut (ktc_values.csv).

Run locally:  streamlit run trade_calculator_app.py
requirements.txt:  streamlit  and  requests
"""

import csv
import io
import os
import re
import time
import unicodedata
from datetime import date
from itertools import combinations

import requests
import streamlit as st

# =============================================================================
# SETTINGS — sidebar defaults; edit to change them.
# =============================================================================
KTC_CSV_PATH = "ktc_values.csv"
DEFAULT_TOLERANCE_PCT = 7             # match window: ± this % of your adjusted value
CONSOLIDATION_EXPONENT = 1.5          # 2-player side value = (v1^p + v2^p)^(1/p); 1.0 = plain sum
ONE_QB_QB_MULTIPLIER = 0.60           # QB multiplier in 1QB leagues (CSV values are superflex)
TE_PREMIUM_BOOST_PER_POINT = 0.25     # TE multiplier = 1 + this * bonus_rec_te
INCLUDE_PICKS_DEFAULT = True          # include draft picks in dynasty leagues
MIN_PIECE_SHARE = 0.20                # in a 1-for-2, the smaller piece must be >= 20% of the bigger one
MIN_PIECE_VALUE = 300                 # ignore near-zero throw-ins in 1-for-2s
MAX_SAVED_USERNAMES = 10
PICK_YEARS_AHEAD = 3                  # dynasty picks through current season + N
PLAYERS_CACHE_HOURS = 24              # /players/nfl is ~5 MB; refresh at most daily
LEAGUE_CACHE_MINUTES = 10             # Sleeper league/roster responses are reused this long
REQUEST_TIMEOUT = 20
API_RETRIES = 3
# =============================================================================

SLEEPER_BASE = "https://api.sleeper.app/v1"
POSITIONS = ("QB", "RB", "WR", "TE")
NON_STARTER_SLOTS = {"BN", "IR", "TAXI"}
LEAGUE_TYPES = ("Redraft Lineup", "Redraft Bestball", "Dynasty Lineup", "Dynasty Bestball")
DEFAULT_TYPES = {"Dynasty Lineup", "Dynasty Bestball"}


def ordinal(n):
    return "%d%s" % (n, "tsnrhtdd"[(n // 10 % 10 != 1) * (n % 10 < 4) * n % 10::4])


# =============================================================================
# Sleeper API (cached so reruns don't re-hit Sleeper)
# =============================================================================
class SleeperError(Exception):
    pass


_session = requests.Session()
_session.headers["User-Agent"] = "sleeper-trade-calculator/3.0"


def _fetch(path):
    """GET with retries. Returns JSON (None for 404/null); raises SleeperError on failure."""
    last = None
    for attempt in range(1, API_RETRIES + 1):
        try:
            resp = _session.get(f"{SLEEPER_BASE}{path}", timeout=REQUEST_TIMEOUT)
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


@st.cache_data(ttl=LEAGUE_CACHE_MINUTES * 60, show_spinner=False)
def _cached_fetch(path):
    return _fetch(path)   # failures raise, and Streamlit never caches exceptions


def api(path, default=None):
    try:
        data = _cached_fetch(path)
    except SleeperError:
        return default
    return default if data is None else data


def resolve_season(override):
    """Current season from /state/nfl (league_season, then season). Returns (season, alternates)."""
    if override:
        return str(override), []
    state = api("/state/nfl", {}) or {}
    primary = state.get("league_season") or state.get("season")
    if primary:
        alts = [str(s) for s in (state.get("season"), state.get("previous_season")) if s and str(s) != str(primary)]
        return str(primary), alts
    return str(date.today().year), []


@st.cache_data(ttl=PLAYERS_CACHE_HOURS * 3600, show_spinner="Downloading the Sleeper player list (once a day)…")
def load_players_db():
    """{pid: (name, position, nfl_team)} for QB/RB/WR/TE only."""
    raw = _fetch("/players/nfl")
    slim = {}
    for pid, p in (raw or {}).items():
        if not isinstance(p, dict) or p.get("position") not in POSITIONS:
            continue
        name = p.get("full_name") or " ".join(x for x in (p.get("first_name"), p.get("last_name")) if x) or pid
        slim[pid] = (name, p["position"], p.get("team") or "FA")
    return slim


# =============================================================================
# KTC values & name matching
# =============================================================================
_SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "v"}
_PICK_RE = re.compile(r"^\s*(\d{4})\s+(early|mid|late)\s+(\d+)(?:st|nd|rd|th)\s*$", re.I)


def name_tokens(name):
    """lowercase, strip accents, periods, apostrophes, hyphens, trailing Jr./Sr./II/III/IV/V."""
    s = unicodedata.normalize("NFKD", str(name or "")).encode("ascii", "ignore").decode("ascii").lower()
    s = re.sub(r"[^a-z0-9\s]", "", s.replace("-", " "))
    tokens = s.split()
    while len(tokens) > 1 and tokens[-1] in _SUFFIXES:
        tokens.pop()
    return tokens


def name_key(name):
    return "".join(name_tokens(name))


def initial_key(name):
    t = name_tokens(name)
    return f"{t[0][0]}|{t[-1]}" if len(t) >= 2 else None


class KTCValues:
    def __init__(self, csv_text):
        self.players, self.picks, self._by_initial, self.fallback = {}, {}, {}, {}
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

    def match(self, name):
        """(value, method) with method 'exact', 'initial' or None."""
        key = name_key(name)
        if key in self.players:
            return self.players[key][1], "exact"
        ik = initial_key(name)
        if ik in self.fallback:
            return self.players[self.fallback[ik]][1], "initial"
        return 0, None


@st.cache_resource(show_spinner=False, max_entries=4)
def parse_ktc(csv_text):
    """Parsed once per distinct CSV and shared (cache_resource keeps the object as-is, no pickling)."""
    return KTCValues(csv_text)


def read_repo_csv():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), KTC_CSV_PATH)
    with open(path if os.path.exists(path) else KTC_CSV_PATH, encoding="utf-8-sig") as fh:
        return fh.read()


# =============================================================================
# League info
# =============================================================================
def league_category(league):
    """One of LEAGUE_TYPES. Sleeper settings.type 2 = dynasty; 0 (redraft) and 1 (keeper) count as redraft.
    settings.best_ball 1 = best ball."""
    s = league.get("settings") or {}
    dynasty = int(s.get("type") or 0) == 2
    bestball = int(s.get("best_ball") or 0) == 1
    return f"{'Dynasty' if dynasty else 'Redraft'} {'Bestball' if bestball else 'Lineup'}"


class LeagueFormat:
    def __init__(self, league, one_qb_mult, te_boost):
        s = league.get("settings") or {}
        scoring = league.get("scoring_settings") or {}
        starters = [p for p in (league.get("roster_positions") or []) if p not in NON_STARTER_SLOTS]
        self.category = league_category(league)
        self.dynasty = self.category.startswith("Dynasty")
        self.num_teams = int(league.get("total_rosters") or 0)
        self.start = len(starters)
        self.qb_slots = starters.count("QB")
        self.has_sf = "SUPER_FLEX" in starters
        self.superflex = self.has_sf or self.qb_slots >= 2
        self.ppr = float(scoring.get("rec") or 0)
        self.tep = float(scoring.get("bonus_rec_te") or 0)
        self.draft_rounds = int(s.get("draft_rounds") or 4)
        self.one_qb_mult, self.te_boost = one_qb_mult, te_boost

    def multiplier(self, pos):
        """KTC values are superflex: discount QBs in 1QB leagues; boost TEs with TE premium."""
        if pos == "QB" and not self.superflex:
            return self.one_qb_mult
        if pos == "TE" and self.tep > 0:
            return 1 + self.te_boost * self.tep
        return 1.0

    def describe(self):
        qb = "Superflex" if self.has_sf else ("2QB" if self.qb_slots >= 2 else "1QB")
        ppr = {1.0: "PPR", 0.5: "Half PPR", 0.0: "Standard"}.get(self.ppr, f"{self.ppr:g} PPR")
        typ, fmt = self.category.split()
        parts = [f"{self.num_teams} Team", typ, qb, ppr]
        if self.tep:
            parts.append(f"{self.tep:g} TEP")
        parts += [fmt, f"Start {self.start}"]
        return " ".join(parts)


@st.cache_data(ttl=LEAGUE_CACHE_MINUTES * 60, show_spinner="Loading league…")
def load_league(league, season, _players_db, _ktc, one_qb_mult, te_boost, ktc_sig):
    """Every rostered QB/RB/WR/TE (and dynasty future pick) in the league with its value.
    Returns (assets, teams, unmatched, fallback). Args starting with _ aren't hashed by
    Streamlit; ktc_sig changes when a different KTC file is used, which refreshes the cache."""
    lid = league["league_id"]
    fmt = LeagueFormat(league, one_qb_mult, te_boost)
    users = api(f"/league/{lid}/users", []) or []
    rosters = api(f"/league/{lid}/rosters", []) or []

    names = {}
    for u in users:
        team_name = ((u.get("metadata") or {}).get("team_name") or "").strip()
        disp = u.get("display_name") or u.get("username") or "?"
        names[u.get("user_id")] = f"{disp} ({team_name})" if team_name and team_name != disp else disp

    teams = {}      # roster_id -> {"owner_id", "co_owners", "label", "orphan"}
    assets = []     # dicts: id, name, pos, nfl, ktc, value, roster_id, kind
    unmatched, fallback = [], []
    for r in rosters:
        try:
            rid = int(r.get("roster_id"))
        except (TypeError, ValueError):
            continue
        owner = r.get("owner_id")
        teams[rid] = {"owner_id": owner, "co_owners": [c for c in (r.get("co_owners") or []) if c],
                      "label": names.get(owner) or (f"Orphaned team #{rid}" if not owner else f"User {owner}"),
                      "orphan": not owner}
        for pid in (r.get("players") or []):              # players can be null on empty rosters
            info = _players_db.get(str(pid))
            if not info:
                continue                                  # K / DEF / IDP
            pname, pos, nfl = info
            ktc, how = _ktc.match(pname)
            if how is None:
                unmatched.append((pname, pos, teams[rid]["label"]))
            elif how == "initial":
                fallback.append(pname)
            assets.append({"id": str(pid), "name": pname, "pos": pos, "nfl": nfl, "ktc": ktc,
                           "value": round(ktc * fmt.multiplier(pos)), "roster_id": rid, "kind": "player"})

    # ---- Dynasty picks: next season (or this one if its rookie draft hasn't run) through +N years.
    if fmt.dynasty and teams:
        first = int(season) + (0 if league.get("status") in ("pre_draft", "drafting") else 1)
        owner_of = {(str(y), rnd, rid): rid for y in range(first, int(season) + PICK_YEARS_AHEAD + 1)
                    for rnd in range(1, fmt.draft_rounds + 1) for rid in teams}
        for tp in api(f"/league/{lid}/traded_picks", []) or []:   # roster_id = original, owner_id = current
            try:
                key = (str(tp.get("season")), int(tp.get("round")), int(tp.get("roster_id")))
                if key in owner_of and tp.get("owner_id") is not None:
                    owner_of[key] = int(tp["owner_id"])
            except (TypeError, ValueError):
                continue
        # Next year's pick tier comes from the original team's roster value rank (weakest third = Early);
        # later years are valued as Mid.
        roster_val = {rid: 0 for rid in teams}
        for a in assets:
            roster_val[a["roster_id"]] += a["value"]
        ranked = sorted(roster_val, key=roster_val.get)
        n = len(ranked)
        tier_of = {rid: ("Early" if i < n / 3 else "Late" if i >= 2 * n / 3 else "Mid") for i, rid in enumerate(ranked)}
        for (y, rnd, orig), cur in sorted(owner_of.items()):
            if cur not in teams:
                continue
            tier = tier_of.get(orig, "Mid") if int(y) == first else "Mid"
            value = _ktc.picks.get((y, tier, rnd), 0)
            if not value:
                continue
            src = "own" if orig == cur else (f"via {teams[orig]['label'].split(' (')[0]}" if orig in teams else f"via #{orig}")
            assets.append({"id": f"pick:{y}:{rnd}:{orig}", "name": f"{y} {ordinal(rnd)} ({src}, {tier})",
                           "pos": "PICK", "nfl": "", "ktc": value, "value": value, "roster_id": cur,
                           "kind": "pick"})
    return assets, teams, unmatched, fallback


# =============================================================================
# Trade matching
# =============================================================================
def side_value(values, p):
    """Consolidation: (sum v^p)^(1/p). One asset keeps its value; two pieces count for less than
    their sum (two 4,000s ≈ 6,350 at p=1.5), so a 2-player return has to add extra value."""
    if p == 1 or len(values) == 1:
        return float(sum(values))
    return sum(v ** p for v in values) ** (1.0 / p)


def one_for_one(my_value, candidates, tol):
    lo, hi = my_value * (1 - tol), my_value * (1 + tol)
    return [a for a in candidates if lo <= a["value"] <= hi]


def one_for_two(my_value, candidates, tol, p):
    """Every pair from one team whose consolidated value is within ± tol of mine. Neither piece
    may be worth more than my side, and the smaller piece must be a real piece (not a throw-in)."""
    lo, hi = my_value * (1 - tol), my_value * (1 + tol)
    by_team = {}
    for a in candidates:
        if MIN_PIECE_VALUE <= a["value"] <= my_value:
            by_team.setdefault(a["roster_id"], []).append(a)
    out = []
    for pool in by_team.values():
        pool.sort(key=lambda a: a["value"], reverse=True)
        for a, b in combinations(pool, 2):
            if b["value"] < MIN_PIECE_SHARE * a["value"]:
                continue
            v = side_value([a["value"], b["value"]], p)
            if lo <= v <= hi:
                out.append((a, b, v))
    return out


# =============================================================================
# Remember usernames in this browser (localStorage, via two tiny Streamlit components)
# =============================================================================
_STORE_KEY = "trade_calc_usernames"
try:
    _reader = st.components.v2.component("username_reader", js=f"""
export default function(component) {{
  let saved = [];
  try {{ saved = JSON.parse(window.localStorage.getItem("{_STORE_KEY}") || "[]"); }} catch (e) {{}}
  component.setStateValue("users", Array.isArray(saved) ? saved : []);
}}""")
    _writer = st.components.v2.component("username_writer", js=f"""
export default function(component) {{
  const d = component.data;
  if (d && Array.isArray(d.users)) {{
    try {{ window.localStorage.setItem("{_STORE_KEY}", JSON.stringify(d.users)); }} catch (e) {{}}
  }}
}}""")
except Exception:          # Streamlit without components.v2: usernames just won't persist
    _reader = _writer = None


def saved_usernames():
    """Usernames saved in this browser, read once per session."""
    ss = st.session_state
    if "saved_users" not in ss:
        ss["saved_users"], ss["saved_loaded"] = [], False
    if _reader is not None and not ss["saved_loaded"]:
        res = _reader(key="username_reader", default={"users": None}, on_users_change=lambda: None, height=0)
        loaded = getattr(res, "users", None)
        if isinstance(loaded, list):
            merged = ss["saved_users"] + [str(u) for u in loaded if u and str(u) not in ss["saved_users"]]
            ss["saved_users"], ss["saved_loaded"] = merged[:MAX_SAVED_USERNAMES], True
    return ss["saved_users"]


def remember_username(name):
    users = [u for u in st.session_state.get("saved_users", []) if u.lower() != name.lower()]
    st.session_state["saved_users"] = ([name] + users)[:MAX_SAVED_USERNAMES]


def forget_usernames():
    st.session_state["saved_users"] = []
    st.session_state.pop("username_pick", None)


def sync_usernames():
    if _writer is not None and st.session_state.get("saved_loaded"):
        _writer(key="username_writer", data={"users": st.session_state["saved_users"]}, height=0)


# =============================================================================
# UI
# =============================================================================
def main():
    st.set_page_config(page_title="Sleeper Trade Calculator", page_icon="🏈", layout="wide")
    ss = st.session_state

    # ---------------- Sidebar: user, league types, league ----------------
    with st.sidebar:
        st.header("Import Your League")
        saved = saved_usernames()
        current = ss.get("active_user")
        options = ([current] if current else []) + [u for u in saved if u != current]
        username = st.selectbox(
            "Sleeper username", options=options, index=0 if options else None,
            accept_new_options=True, placeholder="Type or pick a username",
            help="Usernames you look up are remembered on this device.")
        username = (username or "").strip()

        st.markdown("**League types**")
        chosen_types = [t for t in LEAGUE_TYPES if st.checkbox(t, value=t in DEFAULT_TYPES, key=f"type_{t}")]

        league = None
        if username:
            user = api(f"/user/{username}", None)
            if not isinstance(user, dict) or not user.get("user_id"):
                st.error(f"No Sleeper account named “{username}”. Use your username, not your display name.")
            else:
                ss["active_user"] = username
                remember_username(username)
                season, alts = resolve_season(ss.get("season_override", ""))
                leagues = api(f"/user/{user['user_id']}/leagues/nfl/{season}", []) or []
                for alt in alts:                         # offseason before leagues renew
                    if leagues:
                        break
                    leagues = api(f"/user/{user['user_id']}/leagues/nfl/{alt}", []) or []
                    season = alt if leagues else season
                shown = [lg for lg in leagues if league_category(lg) in chosen_types]
                if not leagues:
                    st.warning(f"No leagues found for {username} in {season}.")
                elif not shown:
                    st.info("None of your leagues match the checked league types.")
                else:
                    labels = {}
                    for lg in shown:
                        base = f"{lg.get('name') or lg.get('league_id')} ({league_category(lg)})"
                        labels[base if base not in labels else f"{base} #{lg.get('league_id')[-4:]}"] = lg
                    pick = st.selectbox(f"Select a League ({len(shown)})", list(labels), key="league_pick")
                    league = labels[pick]
                ss["user_id"], ss["season"] = user["user_id"], season

        st.divider()
        st.subheader("Trade Settings")
        tol = st.slider("Match Tolerance (%)", 1, 20, DEFAULT_TOLERANCE_PCT,
                        help="Show returns within ± this % of your adjusted value.")
        cons = st.slider("Consolidation strength", 1.0, 3.0, CONSOLIDATION_EXPONENT, 0.1,
                         help="1 = two players are worth their plain sum. Higher means a 2-player "
                              "return must add up to more than a single player.")
        qb_mult = st.slider("QB value in 1QB leagues", 0.2, 1.0, ONE_QB_QB_MULTIPLIER, 0.05,
                            help="KTC values are superflex, so QBs are discounted in 1QB leagues.")
        te_boost = st.slider("TE premium boost", 0.0, 1.0, TE_PREMIUM_BOOST_PER_POINT, 0.05,
                             help="Per point of TE bonus: 0.25 at 0.5 TEP = TEs +12.5%.")
        include_picks = st.checkbox("Include draft picks", INCLUDE_PICKS_DEFAULT)
        with st.expander("More"):
            st.text_input("Season override", key="season_override", placeholder="auto from Sleeper")
            upload = st.file_uploader("Use a different KTC CSV", type="csv",
                                      help="Columns: Player_Sleeper, KTC_Value. Applies to this session.")
            st.button("Forget saved usernames", on_click=forget_usernames)
        sync_usernames()

    # ---------------- Values ----------------
    try:
        csv_text = upload.getvalue().decode("utf-8-sig") if upload else read_repo_csv()
        ktc = parse_ktc(csv_text)
    except FileNotFoundError:
        st.error(f"{KTC_CSV_PATH} isn't in the repo next to this app. Add it, or upload one under More.")
        return
    except (ValueError, UnicodeDecodeError) as exc:
        st.error(f"Couldn't read the KTC CSV: {exc}")
        return

    st.title("Sleeper Trade Calculator")
    if not username:
        st.info("Enter your Sleeper username in the sidebar (› at the top left on phones).")
        return
    if league is None:
        return

    try:
        players_db = load_players_db()
    except SleeperError as exc:
        st.error(f"Couldn't download the Sleeper player list ({exc}). Try again in a minute.")
        return
    ktc.build_fallback(players_db)

    fmt = LeagueFormat(league, qb_mult, te_boost)
    st.markdown(f"<div style='font-size:22px; font-weight:600; color:#4da6ff; text-align:center;'>"
                f"{fmt.describe()}</div>", unsafe_allow_html=True)

    assets, teams, unmatched, fallback = load_league(
        league, ss["season"], players_db, ktc, qb_mult, te_boost, hash(csv_text))
    user_id = ss["user_id"]
    my_rid = next((rid for rid, t in teams.items() if t["owner_id"] == user_id or user_id in t["co_owners"]), None)
    if my_rid is None:
        st.warning("You don't own a roster in this league.")
        return
    if not include_picks:
        assets = [a for a in assets if a["kind"] != "pick"]
    mine = sorted((a for a in assets if a["roster_id"] == my_rid), key=lambda a: a["value"], reverse=True)
    # Trade targets: other teams only; orphaned teams and unmatched (0-value) players are never offered.
    others = [a for a in assets if a["roster_id"] != my_rid and not teams[a["roster_id"]]["orphan"] and a["value"] > 0]

    # ---------------- Player selection ----------------
    st.markdown("<h3 style='text-align:center;'>Select player(s) to trade away:</h3>", unsafe_allow_html=True)
    lid = league["league_id"]
    selected = []
    with st.expander("Player Selection", expanded=True):
        groups = [("QB", "RB"), ("WR", "TE")] + ([("PICK",)] if any(a["kind"] == "pick" for a in mine) else [])
        for col, group in zip(st.columns(len(groups)), groups):
            with col:
                for pos in group:
                    st.markdown(f"**{'Picks' if pos == 'PICK' else pos}**")
                    group_assets = [a for a in mine if a["pos"] == pos]
                    if not group_assets:
                        st.caption("None")
                    for a in group_assets:
                        label = f"{a['name']} (KTC: {a['ktc']:,})" if a["ktc"] else f"{a['name']} (no KTC value)"
                        if st.checkbox(label, key=f"cb_{lid}_{a['id']}", disabled=not a["value"]):
                            selected.append(a)

    if not selected:
        st.caption("Check a player above to see the 1-for-1 and 1-for-2 trades available in this league.")
    else:
        n = len(selected)
        raw_total = sum(a["ktc"] for a in selected)
        fmt_total = sum(a["value"] for a in selected)
        my_value = side_value([a["value"] for a in selected], cons)   # a multi-player package is consolidated too
        img_col, val_col = st.columns([1, 2], gap="large")
        with img_col:
            pics = [a for a in selected if a["kind"] == "player"]
            if pics:
                st.image([f"https://sleepercdn.com/content/nfl/players/{a['id']}.jpg" for a in pics],
                         caption=[a["name"] for a in pics], width=120)
        with val_col:
            lines = [f"<b>Total Raw KTC Value:</b> {raw_total:,}"]
            if fmt_total != raw_total:
                lines.append(f"<b>League Format Adjustment:</b> {fmt_total - raw_total:+,}")
            if n > 1:
                lines.append(f"<b>Package Adjustment:</b> {my_value - fmt_total:+,.0f}")
            lines.append(f"<b>Adjusted Trade Value:</b> {my_value:,.0f}")
            st.markdown("<h3 style='text-align:center;'>Selected Player Package</h3>"
                        "<div style='text-align:center; line-height:1.9'>" + "<br>".join(lines) + "</div>",
                        unsafe_allow_html=True)

        tol_f = tol / 100.0
        owner = lambda a: teams[a["roster_id"]]["label"]

        with st.expander(f"📈 {n}-for-1 Trade Suggestions", expanded=True):
            rows = sorted(one_for_one(my_value, others, tol_f), key=lambda a: a["value"], reverse=True)
            if rows:
                st.dataframe([{"Player": a["name"], "Position": a["pos"], "Team": a["nfl"], "KTC": a["ktc"],
                               "Adjusted": a["value"], "Difference": round(a["value"] - my_value),
                               "Team Owner": owner(a)} for a in rows], hide_index=True, width="stretch")
            else:
                st.write("No 1-for-1 trades found in that range.")

        with st.expander(f"👥 {n}-for-2 Trade Suggestions", expanded=True):
            pairs = sorted(one_for_two(my_value, others, tol_f, cons), key=lambda t: t[2], reverse=True)
            if pairs:
                st.dataframe([{"Team Owner": owner(a),
                               "Player 1": f"{a['name']} ({a['pos']}, KTC: {a['ktc']:,})",
                               "Player 2": f"{b['name']} ({b['pos']}, KTC: {b['ktc']:,})",
                               "Total KTC": a["ktc"] + b["ktc"], "Adjusted": round(v),
                               "Difference": round(v - my_value)} for a, b, v in pairs],
                             hide_index=True, width="stretch")
            else:
                st.write("No 2-player returns found in that range.")

    # ---------------- Value gaps ----------------
    if unmatched or fallback:
        with st.expander(f"Players without a KTC match in this league ({len(unmatched)})"):
            if unmatched:
                st.caption("Valued at 0 and never offered in trades.")
                st.dataframe([{"Player": nm, "Position": pos, "Team Owner": who} for nm, pos, who in sorted(unmatched)],
                             hide_index=True, width="stretch")
            if fallback:
                st.caption("Matched by first initial + last name (worth a quick check): " + ", ".join(sorted(fallback)))


main()
