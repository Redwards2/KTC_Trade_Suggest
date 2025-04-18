import streamlit as st
import pandas as pd
import requests
from itertools import combinations

try:

    # --------------------
    # Sleeper League Loader
    # --------------------
    @st.cache_data(show_spinner="Fetching league data from Sleeper...")
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
        for roster in rosters:
            roster_id = roster["roster_id"]
            owner_id = roster["owner_id"]
            owner_name = user_map.get(owner_id, f"User {owner_id}")
            player_ids = roster.get("players", [])

            for pid in player_ids:
                player_data = player_pool.get(pid)
                if not player_data:
                    continue
                full_name = player_data.get("full_name", pid)
                position = player_data.get("position", "")
                team = player_data.get("team", "")
                data.append({
                    "Sleeper_Player_ID": pid,
                    "Player_Sleeper": full_name,
                    "Position": position,
                    "Team": team,
                    "Team_Owner": owner_name,
                    "Roster_ID": roster_id
                })

        df = pd.DataFrame(data)
        ktc_df["Player_Sleeper_lower"] = ktc_df["Player_Sleeper"].str.lower()
        df["Player_Sleeper_lower"] = df["Player_Sleeper"].str.lower()

        merged = df.merge(ktc_df, on="Player_Sleeper_lower", how="left", suffixes=("", "_ktc"))
        merged = merged.drop(columns=["Player_Sleeper_lower"])
        merged["KTC_Value"] = merged["KTC_Value"].fillna(0).astype(int)
        merged = merged[merged["KTC_Value"] >= 2000]
        return merged

    # --------------------
    # Stud Bonus Function
    # --------------------
    def stud_bonus(value):
        if value >= 9000: return 3200
        elif value >= 8500: return 2900
        elif value >= 8000: return 2600
        elif value >= 7500: return 2400
        elif value >= 7000: return 2200
        elif value >= 6500: return 2000
        elif value >= 6000: return 1800
        elif value >= 5000: return 1400
        elif value >= 4000: return 1000
        elif value >= 3000: return 700
        elif value >= 2000: return 400
        return 0

    # --------------------
    # Sidebar: User & League Picker
    # --------------------
    st.sidebar.header("Import Your League")
    username = st.sidebar.text_input("Enter your Sleeper username")

    league_id = None
    league_options = {}

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

        except requests.exceptions.Timeout:
            st.sidebar.error("⚠️ Sleeper API timed out. Try again shortly.")
        except requests.exceptions.RequestException as e:
            st.sidebar.error(f"⚠️ Error fetching leagues: {e}")

except Exception as e:
    st.error(f"🚨 Something broke: {e}")
