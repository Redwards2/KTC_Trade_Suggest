import streamlit as st
import pandas as pd
import requests
from itertools import combinations

# --------------------
# Sleeper League Loader
# --------------------
# @st.cache_data(show_spinner="Fetching league data from Sleeper...")

def load_league_data(league_id, ktc_df):
        player_pool_url = "https://api.sleeper.app/v1/players/nfl"
        pool_response = requests.get(player_pool_url)
        player_pool = pool_response.json()

        users_url = f"https://api.sleeper.app/v1/league/{league_id}/users"
        rosters_url = f"https://api.sleeper.app/v1/league/{league_id}/rosters"
        users = requests.get(users_url).json()
        rosters = requests.get(rosters_url).json()

        user_map = {user['user_id']: user['display_name'] for user in users}

        data = []

        # Create a mapping of roster_id to owner_name
        roster_owner_map = {roster["roster_id"]: user_map.get(roster["owner_id"], f"User {roster['owner_id']}") for roster in rosters}

        # Determine draft order based on reverse final standings
        try:
            previous_league_id = requests.get(f"https://api.sleeper.app/v1/league/{league_id}").json().get("previous_league_id")
            if previous_league_id:
                standings = requests.get(f"https://api.sleeper.app/v1/league/{previous_league_id}/rosters").json()
                standings_sorted = sorted(standings, key=lambda x: x.get("settings", {}).get("wins", 0) - x.get("settings", {}).get("losses", 0))
                ordered_rosters = [r["roster_id"] for r in standings_sorted]
            else:
                ordered_rosters = sorted(roster_owner_map.keys())
        except:
            ordered_rosters = sorted(roster_owner_map.keys())

        for rnd in range(1, 5):
            for i, roster_id in enumerate(ordered_rosters):
                pick_number = f"{rnd}.{i+1:02d}"
                pick_label = f"2025 Pick {pick_number}"
                pid = f"2025_{rnd}_{i+1}"
                ktc_row = ktc_df[ktc_df["Player_Sleeper"].str.strip().str.lower() == pick_label.lower()]
                ktc_value = int(ktc_row["KTC_Value"].iloc[0]) if not ktc_row.empty else 0
                data.append({
                    "Sleeper_Player_ID": pid,
                    "Player_Sleeper": pick_label,
                    "Position": "PICK",
                    "Team": "",
                    "Team_Owner": roster_owner_map[roster_id],
                    "Roster_ID": roster_id,
                    "KTC_Value": ktc_value
                })

        # Override with traded pick data
        try:
            traded = requests.get(f"https://api.sleeper.app/v1/league/{league_id}/draft_picks").json()
            pick_index_map = {(f"2025 Pick {r}.{p:02d}"): idx for r in range(1, 5) for p, idx in zip(range(1, 13), range(12))}
            for pick in traded:
                season = pick.get("season")
                round_num = pick.get("round")
                order = pick.get("order")
                owner_id = pick.get("owner_id")
                if season != "2025" or not round_num or not order:
                    continue
                pick_label = f"2025 Pick {round_num}.{int(order):02d}"
                pid = f"2025_{round_num}_{order}"
                ktc_row = ktc_df[ktc_df["Player_Sleeper"].str.strip().str.lower() == pick_label.lower()]
                ktc_value = int(ktc_row["KTC_Value"].iloc[0]) if not ktc_row.empty else 0
                owner_name = user_map.get(owner_id, f"User {owner_id}")
                data = [d for d in data if d["Sleeper_Player_ID"] != pid]
                data.append({
                    "Sleeper_Player_ID": pid,
                    "Player_Sleeper": pick_label,
                    "Position": "PICK",
                    "Team": "",
                    "Team_Owner": owner_name,
                    "Roster_ID": None,
                    "KTC_Value": ktc_value
                })
        except Exception as e:
            st.warning(f"⚠️ Could not process traded picks: {e}")
            pass

        for roster in rosters:
            roster_id = roster["roster_id"]
            owner_id = roster["owner_id"]
            owner_name = user_map.get(owner_id, f"User {owner_id}")
            player_ids = roster.get("players", [])

            for pid in player_ids:
                player_data = player_pool.get(pid, {})
                full_name = player_data.get("full_name", pid)
                position = player_data.get("position", "PICK" if "_" in pid and pid.count("_") == 2 else "")
                team = player_data.get("team", "")

                if position == "PICK":
                    parts = pid.split("_")
                    if len(parts) == 3 and all(part.isdigit() for part in parts):
                        season, rnd, pick = parts
                        full_name = f"{season} Pick {rnd}.{int(pick):02d}"

                ktc_row = ktc_df[ktc_df["Player_Sleeper"].str.strip().str.lower() == full_name.lower()]
                ktc_value = int(ktc_row["KTC_Value"].iloc[0]) if not ktc_row.empty else 0

                data.append({
                    "Sleeper_Player_ID": pid,
                    "Player_Sleeper": full_name,
                    "Position": position,
                    "Team": team,
                    "Team_Owner": owner_name,
                    "Roster_ID": roster_id,
                    "KTC_Value": ktc_value
                })

        df = pd.DataFrame(data)
        ktc_df["Player_Sleeper_lower"] = ktc_df["Player_Sleeper"].str.lower()
        df["Player_Sleeper_lower"] = df["Player_Sleeper"].str.lower()

        merged = df.merge(ktc_df, on="Player_Sleeper_lower", how="left", suffixes=("", "_ktc"))
        merged = merged.drop(columns=["Player_Sleeper_lower"])
        if "KTC_Value_ktc" in merged.columns:
            merged["KTC_Value"] = merged["KTC_Value_ktc"].fillna(merged["KTC_Value"])
            merged = merged.drop(columns=["KTC_Value_ktc"])
        merged["KTC_Value"] = merged["KTC_Value"].fillna(0).astype(int)
        return merged

# --------------------
# Stud Bonus Function
# --------------------

def stud_bonus(value):
        if value >= 9000: return 3450
        elif value >= 8500: return 3150
        elif value >= 8000: return 2850
        elif value >= 7500: return 2650
        elif value >= 7000: return 2450
        elif value >= 6500: return 2250
        elif value >= 6000: return 2050
        elif value >= 5000: return 1650
        elif value >= 4000: return 1250
        elif value >= 3000: return 950
        elif value >= 2000: return 650
        return 0

# --------------------
# Sidebar: User & League Picker
# --------------------

st.sidebar.header("Import Your League")
username = st.sidebar.text_input("Enter your Sleeper username").strip()
username_lower = username.lower()

league_id = None
league_options = {}
df = pd.DataFrame()

if username:
        try:
            user_info_url = f"https://api.sleeper.app/v1/user/{username}"
            with st.spinner("🔍 Looking up user ID..."):
                user_response = requests.get(user_info_url, timeout=10)
                user_response.raise_for_status()
                user_data = user_response.json()
                user_id = user_data.get("user_id")

            leagues_url = f"https://api.sleeper.app/v1/user/{user_id}/leagues/nfl/2025"
            with st.spinner("🔍 Looking up leagues..."):
                response = requests.get(leagues_url, timeout=10)
                response.raise_for_status()
                leagues = response.json()

            if leagues:
                for league in leagues:
                    name = league["name"]
                    lid = league["league_id"]
                    league_options[name] = lid

                if league_options:
                    selected_league_name = st.sidebar.selectbox("Select a league", list(league_options.keys()))
                    league_id = league_options[selected_league_name]
                else:
                    st.sidebar.warning("No leagues found for this username.")
            else:
                st.sidebar.warning("No leagues found for this username.")

            # Load KTC values
            ktc_df = pd.read_csv("ktc_values.csv", encoding="utf-8-sig")
            

            # Load league data
            if league_id:
                df = load_league_data(league_id, ktc_df)

        except requests.exceptions.Timeout:
            st.sidebar.error("⚠️ Sleeper API timed out. Try again shortly.")
        except requests.exceptions.RequestException as e:
            st.sidebar.error(f"⚠️ Error fetching leagues: {e}")
        except Exception as e:
            st.error(f"🚨 Something broke while loading league data: {e}")

# --------------------
try:
    # --------------------
    if not df.empty:
        st.title("Trade Suggestions (Based off KTC Values)")
        st.caption("Adding draft picks soon")

        user_players = df[df["Team_Owner"].str.lower() == username_lower]
        player_list = user_players["Player_Sleeper"].sort_values().unique()
        selected_player = st.selectbox("Select a player to trade away:", player_list)
        tolerance = st.slider("Match Tolerance (%)", 1, 15, 5)
        st.markdown("**QB Premium**")
        st.caption("How much does your league value QBs?")
        qb_premium_setting = st.slider("QB Premium Bonus", 0, 1500, 300, step=25)

        if selected_player:
            row = df[df["Player_Sleeper"] == selected_player].iloc[0]
            owner = row["Team_Owner"]
            base_value = row["KTC_Value"]
            bonus = stud_bonus(base_value)
            qb_premium = qb_premium_setting if row["Position"] == "QB" else 0
            adjusted_value = base_value + bonus + qb_premium

            st.subheader("Selected Player Details")
            st.markdown(f"- **Player:** {selected_player}")
            st.markdown(f"- **Team Owner:** {owner}")
            st.markdown(f"- **Raw KTC Value:** {base_value}")
            st.markdown(f"- **Stud Bonus (2-for-1 only):** +{bonus}")
            st.markdown(f"- **QB Premium:** +{qb_premium if qb_premium else 0}")
            st.markdown(f"- **Adjusted 2-for-1 Value:** {adjusted_value}")

            # 1-for-1 Trades
            st.subheader("1-for-1 Trade Suggestions")
            one_low = int(base_value * (1 - tolerance / 100))
            one_high = int(base_value * (1 + tolerance / 100))

            one_for_one = df[
                (df["KTC_Value"] >= one_low) &
                (df["KTC_Value"] <= one_high) &
                (df["Team_Owner"] != owner)
            ]

            if not one_for_one.empty:
                st.dataframe(one_for_one[["Player_Sleeper", "Position", "Team", "KTC_Value", "Team_Owner"]])
            else:
                st.markdown("No good 1-for-1 trade suggestions found.")

            # 2-for-1 Trades
            st.subheader("2-for-1 Trade Suggestions")
            two_low = int(adjusted_value * (1 - tolerance / 100))
            two_high = int(adjusted_value * (1 + tolerance / 100))

            results = []
            one_for_one_players = set(one_for_one["Player_Sleeper"])
            for team_owner in df["Team_Owner"].unique():
                if team_owner == owner:
                    continue
                team_players = df[df["Team_Owner"] == team_owner]
                combos = combinations(team_players.iterrows(), 2)
                for (i1, p1), (i2, p2) in combos:
                    if p1["Player_Sleeper"] in one_for_one_players or p2["Player_Sleeper"] in one_for_one_players:
                        continue
                    if p1["KTC_Value"] > base_value or p2["KTC_Value"] > base_value:
                        continue
                    total = p1["KTC_Value"] + p2["KTC_Value"]
                    if p1["Position"] == "QB": total += qb_premium_setting
                    if p2["Position"] == "QB": total += qb_premium_setting
                    if two_low <= total <= two_high:
                        results.append({
                            "Owner": team_owner,
                            "Player 1": p1["Player_Sleeper"],
                            "Player 2": p2["Player_Sleeper"],
                            "Total KTC": total
                        })

            if results:
                trade_df = pd.DataFrame(results)
                selected_owner_filter = st.selectbox("Filter by Owner:", options=["All"] + sorted(trade_df["Owner"].unique()))
                
                if selected_owner_filter != "All":
                    trade_df = trade_df[trade_df["Owner"] == selected_owner_filter]
                st.dataframe(trade_df.sort_values(by=["Owner", "Player 1", "Player 2"]))
            else:
                st.markdown("No good 2-for-1 trade suggestions found.")
    else:
        st.info("Enter your Sleeper username and select a league to begin.")

except Exception as e:
    st.error(f"🚨 Something broke: {e}")
