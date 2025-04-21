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
# Dud Bonus Function
# --------------------
def dud_bonus(value):
    if value <= 1000: return -800
    elif value <= 1500: return -600
    elif value <= 2000: return -400
    elif value <= 2500: return -250
    return 0

# --------------------
# Streamlit UI Setup
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

# --------------------
# Main App
# --------------------
ktc_df = pd.read_csv("ktc_values.csv")
st.title("Dynasty Trade Calculator (KTC Style)")
st.caption("Adding draft picks soon")

username = st.sidebar.text_input("Enter Sleeper Username:")
league_id = st.sidebar.text_input("Enter League ID:")

if username and league_id:
    try:
        df, _ = load_league_data(league_id, ktc_df)
        player_list = sorted(df["Player_Sleeper"].unique())
        selected_player = st.selectbox("Select a player to trade away:", player_list)

        if selected_player:
            row = df[df["Player_Sleeper"] == selected_player].iloc[0]
            base_value = row["KTC_Value"]
            bonus = stud_bonus(base_value)
            penalty = dud_bonus(base_value)
            adjusted_value = base_value + bonus + penalty

            st.subheader("Selected Player Details")
            st.markdown(f"- **Player:** {selected_player}")
            st.markdown(f"- **Raw KTC Value:** {base_value}")
            st.markdown(f"- **Stud Bonus:** +{bonus}")
            st.markdown(f"- **Dud Penalty:** {penalty}")
            st.markdown(f"- **Adjusted 2-for-1 Value:** {adjusted_value}")

            tolerance = st.slider("Match Tolerance (%)", 1, 15, 5)
            one_low = int(base_value * (1 - tolerance / 100))
            one_high = int(base_value * (1 + tolerance / 100))

            one_for_one = df[(df["KTC_Value"] >= one_low) & (df["KTC_Value"] <= one_high) & (df["Player_Sleeper"] != selected_player)]
            one_for_one['Team'] = one_for_one['Team'].fillna('').apply(
                lambda abbr: f"<img src='https://a.espncdn.com/i/teamlogos/nfl/500/{abbr}.png' width='30'/> {abbr}" if abbr else abbr)

            # 2-for-1 Trades
            adjusted_low = int(adjusted_value * (1 - tolerance / 100))
            adjusted_high = int(adjusted_value * (1 + tolerance / 100))
            results = []

            for team_owner in df["Team_Owner"].unique():
                if team_owner == row["Team_Owner"]:
                    continue
                team_players = df[df["Team_Owner"] == team_owner]
                combos = combinations(team_players.iterrows(), 2)
                for (i1, p1), (i2, p2) in combos:
                    total = p1["KTC_Value"] + p2["KTC_Value"]
                    if p1["Player_Sleeper"] == selected_player or p2["Player_Sleeper"] == selected_player:
                        continue
                    if p1["KTC_Value"] > base_value or p2["KTC_Value"] > base_value:
                        continue
                    if adjusted_low <= total <= adjusted_high:
                        results.append({
                            "Owner": team_owner,
                            "Player 1": p1["Player_Sleeper"],
                            "Player 2": p2["Player_Sleeper"],
                            "Total KTC": total
                        })

            # Display 1-for-1
            with st.expander("1-for-1 Trade Suggestions", expanded=True):
                if not one_for_one.empty:
                    centered_html = f"""
                    <div style='display: flex; justify-content: center;'>
                      <div style='text-align: center;'>
                        {one_for_one[['Player_Sleeper', 'Position', 'Team', 'KTC_Value', 'Team_Owner']].to_html(escape=False, index=False)}
                      </div>
                    </div>
                    """
                    st.markdown(centered_html, unsafe_allow_html=True)
                else:
                    st.markdown("No good 1-for-1 trade suggestions found.")

            # Display 2-for-1
            with st.expander("2-for-1 Trade Suggestions", expanded=True):
                if results:
                    suggestions_df = pd.DataFrame(results)
                    st.dataframe(suggestions_df)
                else:
                    st.markdown("No good 2-for-1 trade suggestions found.")

    except Exception as e:
        st.error(f"Something went wrong: {e}")
