import pandas as pd
import streamlit as st
from itertools import combinations

# Load data
df = pd.read_csv("rosters_with_ktc_values.csv", encoding="utf-8-sig")
df["KTC_Value"] = df["KTC_Value"].fillna(0).astype(int)
df = df[df["KTC_Value"] >= 2000]  # Remove junk players

# Bonus logic
def stud_bonus(value):
    if value >= 9000:
        return 3200
    elif value >= 8500:
        return 2900
    elif value >= 8000:
        return 2600
    elif value >= 7500:
        return 2400
    elif value >= 7000:
        return 2200
    elif value >= 6500:
        return 2000
    elif value >= 6000:
        return 1800
    elif value >= 5000:
        return 1400
    elif value >= 4000:
        return 1000
    elif value >= 3000:
        return 700
    elif value >= 2000:
        return 400
    return 0

# UI
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

    # 1-for-1 suggestions
    st.subheader("1-for-1 Trade Suggestions")
    low = int(base_value * (1 - tolerance / 100))
    high = int(base_value * (1 + tolerance / 100))
    one_for_one = df[
        (df["KTC_Value"] >= low) &
        (df["KTC_Value"] <= high) &
        (df["Team_Owner"] != owner)
    ]
    if not one_for_one.empty:
        st.dataframe(one_for_one[["Player_Sleeper", "Position", "Team", "KTC_Value", "Team_Owner"]])
    else:
        st.markdown("No good 1-for-1 trade suggestions found.")

    # 2-for-1 suggestions
    st.subheader("2-for-1 Trade Suggestions")
    low = int(adjusted_value * (1 - tolerance / 100))
    high = int(adjusted_value * (1 + tolerance / 100))
    results = []

    for team_owner in df["Team_Owner"].unique():
        if team_owner == owner:
            continue
        team_players = df[df["Team_Owner"] == team_owner]
        combos = combinations(team_players.iterrows(), 2)
        for (i1, p1), (i2, p2) in combos:
            total = p1["KTC_Value"] + p2["KTC_Value"]
            if low <= total <= high:
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
