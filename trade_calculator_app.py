import streamlit as st
import pandas as pd
import requests
from itertools import combinations

# --------------------
# Sleeper League Loader with KTC Matching
# --------------------
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
            position = player_data.get("position", "")
            team = player_data.get("team", "")

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

    return pd.DataFrame(data), player_pool

# --------------------
# Stud Bonus Function
# --------------------
def stud_bonus(value):
    if value >= 9000: return 3700
    elif value >= 8500: return 3400
    elif value >= 8000: return 3100
    elif value >= 7500: return 2850
    elif value >= 7000: return 2600
    elif value >= 6500: return 2300
    elif value >= 6000: return 2000
    elif value >= 5000: return 1650
    elif value >= 4000: return 1300
    elif value >= 3000: return 1000
    elif value >= 2000: return 700
    return 0

# --------------------
# Dud Bonus Function (Negative Bonus for Low Value Players)
# --------------------
def dud_bonus(value):
    if value <= 1000: return -800
    elif value <= 1500: return -600
    elif value <= 2000: return -400
    elif value <= 2500: return -250
    return 0

# --------------------
# Streamlit UI
# --------------------
st.set_page_config(page_title="KTC Trade Suggest", layout="wide")
st.markdown("""
<style>
thead tr th, tbody tr td {
    text-align: center !important;
    vertical-align: middle !important;
}
</style>
""", unsafe_allow_html=True)

# ... (rest of your code remains unchanged)
