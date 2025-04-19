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
                player_data = player_pool.get(pid, {})
                full_name = player_data.get("full_name", pid)
                position = player_data.get("position", "PICK" if "pick" in pid else "")
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
            ktc_df = ktc_df[ktc_df["KTC_Value"] >= 2000]

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
    # Main App Logic
    # --------------------
    if not df.empty:
        st.title("Dynasty Trade Calculator (KTC Style)")

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
            for team_owner in df["Team_Owner"].unique():
                if team_owner == owner:
                    continue
                team_players = df[df["Team_Owner"] == team_owner]
                combos = combinations(team_players.iterrows(), 2)
                for (i1, p1), (i2, p2) in combos:
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
                st.dataframe(pd.DataFrame(results))
            else:
                st.markdown("No good 2-for-1 trade suggestions found.")
    else:
        st.info("Enter your Sleeper username and select a league to begin.")

except Exception as e:
    st.error(f"🚨 Something broke: {e}")
