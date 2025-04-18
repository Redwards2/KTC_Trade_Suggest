import streamlit as st
import pandas as pd
import requests
from itertools import combinations

# --------------------
# Sleeper League Loader
# --------------------
@st.cache_data(show_spinner="Fetching league data from Sleeper...")
def load_league_data(league_id, ktc_df):
    # Load Sleeper player pool
    player_pool_url = "https://api.sleeper.app/v1/players/nfl"
    pool_response = requests.get(player_pool_url)
    player_pool = pool_response.json()

    # Load league users and rosters
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
# Sidebar: League Input
# --------------------
st.sidebar.header("Import Your League")
league_id = st.sidebar.text_input("Enter your Sleeper League ID")

# Load local KTC values
ktc_df = pd.read_csv("ktc_values.csv", encoding="utf-8-sig")
ktc_df = ktc_df[ktc_df["KTC_Value"] >= 2000]

if league_id:
    df = load_league_data(league_id, ktc_df)
else:
    df = pd.DataFrame()

# --------------------
# Main App Logic
# --------------------
if not df.empty:
    st.title("Dynasty Trade Calculator (KTC Style)")

    player_list = df["Player_Sleeper"].sort_values().unique()
    selected_player = st.selectbox("Select a player to trade away:", player_list)
    tolerance = st.slider("Match Tolerance (%)", 1, 15, 5)

    if selected_player:
        row = df[df["Player_Sleeper"] == selected_player].iloc[0]
        owner = row["Team_Owner"]
        base_value = row["KTC_Value"]
        bonus = stud_bonus(base_value)
        adjusted_value = base_value + bonus

        st.subheader("Selected Player Details")
        st.markdown(f"- **Player:** {selected_player}")
        st.markdown(f"- **Raw KTC Value:** {base_value}")
        st.markdown(f"- **Stud Bonus (2-for-1 only):** +{bonus}")
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
    st.info("Enter your Sleeper League ID in the sidebar to get started.")
