"""UAEPL player-category analysis dashboard (plan.md Section 5).

Run with: streamlit run app.py
"""
import sqlite3
from pathlib import Path

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

DB_PATH = Path(__file__).resolve().parent / "data" / "uaepl.db"

# Fixed categorical color order (validated CVD-safe adjacent pairing) -- assigned
# once and reused across every chart, never recomputed per-filter.
CATEGORY_COLORS = {
    "Local": "#2a78d6",
    "Resident": "#008300",
    "Foreign": "#e87ba4",
}
CATEGORY_ORDER = ["Local", "Resident", "Foreign"]


@st.cache_data
def load_data():
    conn = sqlite3.connect(DB_PATH)
    players = pd.read_sql("SELECT * FROM players", conn)
    teams = pd.read_sql("SELECT * FROM teams", conn)
    stats = pd.read_sql("SELECT * FROM player_match_stats", conn)
    conn.close()
    return players, teams, stats


def scoped_stats(stats: pd.DataFrame, team_key) -> pd.DataFrame:
    """Stats scoped to a specific team's match-day appearances, or all stats if None.

    Filtering on player_match_stats.team_key (not players.team_key) means a
    transferred player's stats are correctly split by which team they represented
    in each specific match.
    """
    if team_key is None:
        return stats
    return stats[stats["team_key"] == team_key]


def wide_metrics(stats_scoped: pd.DataFrame, players: pd.DataFrame) -> pd.DataFrame:
    """Pivot the long fact table to one row per player with the metrics the
    dashboard needs, joined to player_category.
    """
    needed = ["minsPlayed", "goals", "goalAssist", "totalPass", "accuratePass", "totalTackle", "wonTackle"]
    sub = stats_scoped[stats_scoped["stat_type"].isin(needed)]
    pivot = sub.pivot_table(index="player_key", columns="stat_type", values="stat_value", aggfunc="sum", fill_value=0)
    for col in needed:
        if col not in pivot.columns:
            pivot[col] = 0
    pivot = pivot.reset_index()
    return pivot.merge(players[["player_key", "canonical_name", "position", "player_category", "team_key"]], on="player_key", how="inner")


def per90(numerator: pd.Series, minutes: pd.Series) -> float:
    total_minutes = minutes.sum()
    if total_minutes == 0:
        return 0.0
    return numerator.sum() / total_minutes * 90


st.set_page_config(page_title="UAEPL Player Category Analysis", layout="wide")
st.title("UAEPL Player Category Analysis")
st.caption("Local vs. Resident vs. Foreign player performance, 2025/26 season")

players, teams, stats = load_data()

team_options = ["All Teams"] + sorted(teams["canonical_name"].dropna().unique().tolist())
selected_team = st.sidebar.selectbox("Team", team_options)
team_key = None
if selected_team != "All Teams":
    team_key = int(teams.loc[teams["canonical_name"] == selected_team, "team_key"].iloc[0])

st.sidebar.markdown("---")
st.sidebar.caption(
    "Per-90-minute and percentage metrics are used throughout instead of raw totals, "
    "since categories differ in average playing time -- raw totals would be misleading."
)

stats_scoped = scoped_stats(stats, team_key)
metrics = wide_metrics(stats_scoped, players)
metrics = metrics[metrics["player_category"].notna()]  # exclude unenriched (ambiguous/unmatched) from category charts

team_names = teams[["team_key", "canonical_name"]].rename(columns={"canonical_name": "team_name"})
metrics = metrics.merge(team_names, on="team_key", how="left")

tab_composition, tab_minutes, tab_attack, tab_pass, tab_defense, tab_leaderboard = st.tabs(
    ["Squad Composition", "Playing Time", "Attacking Output", "Passing", "Defensive Performance", "Leaderboards"]
)

FILTER_HINT = "💡 Use the **Team** filter in the sidebar to scope this view to a single club."

# ---------------------------------------------------------------- Squad composition
with tab_composition:
    st.caption(FILTER_HINT)
    if selected_team == "All Teams":
        st.subheader("Local / Resident / Foreign composition per team")
        team_names = teams[["team_key", "canonical_name"]].rename(columns={"canonical_name": "team_name"})
        comp = players[players["player_category"].notna()].merge(team_names, on="team_key", how="left")
        comp_counts = comp.groupby(["team_name", "player_category"]).size().reset_index(name="n")
        fig = px.bar(
            comp_counts, x="team_name", y="n", color="player_category",
            color_discrete_map=CATEGORY_COLORS, category_orders={"player_category": CATEGORY_ORDER},
            labels={"team_name": "Team", "n": "Players", "player_category": "Category"},
        )
        fig.update_layout(barmode="stack", legend_title_text="Category")
        st.plotly_chart(fig, use_container_width=True)

        col1, col2, col3 = st.columns(3)
        totals = players["player_category"].value_counts()
        col1.metric("Local", int(totals.get("Local", 0)))
        col2.metric("Resident", int(totals.get("Resident", 0)))
        col3.metric("Foreign", int(totals.get("Foreign", 0)))
    else:
        st.subheader(f"{selected_team}: squad composition")
        team_players = players[(players["team_key"] == team_key) & (players["player_category"].notna())]
        counts = team_players["player_category"].value_counts().reindex(CATEGORY_ORDER, fill_value=0)
        fig = go.Figure(
            data=[go.Pie(labels=counts.index, values=counts.values, marker_colors=[CATEGORY_COLORS[c] for c in counts.index], hole=0.45)]
        )
        st.plotly_chart(fig, use_container_width=True)
        col1, col2, col3 = st.columns(3)
        col1.metric("Local", int(counts.get("Local", 0)))
        col2.metric("Resident", int(counts.get("Resident", 0)))
        col3.metric("Foreign", int(counts.get("Foreign", 0)))

# ---------------------------------------------------------------- Playing time
with tab_minutes:
    st.caption(FILTER_HINT)
    st.subheader("Average minutes played by category")
    avg_minutes = metrics.groupby("player_category")["minsPlayed"].mean().reindex(CATEGORY_ORDER)
    fig = px.bar(
        avg_minutes.reset_index(), x="player_category", y="minsPlayed",
        color="player_category", color_discrete_map=CATEGORY_COLORS,
        category_orders={"player_category": CATEGORY_ORDER},
        labels={"player_category": "Category", "minsPlayed": "Avg. minutes played"},
    )
    fig.update_layout(showlegend=False)
    st.plotly_chart(fig, use_container_width=True)

# ---------------------------------------------------------------- Attacking output
with tab_attack:
    st.caption(FILTER_HINT)
    st.subheader("Goals and assists per 90 minutes, by category")

    def per90_table(df):
        rows = []
        for cat in CATEGORY_ORDER:
            sub = df[df["player_category"] == cat]
            rows.append({
                "player_category": cat,
                "Goals per 90": per90(sub["goals"], sub["minsPlayed"]),
                "Assists per 90": per90(sub["goalAssist"], sub["minsPlayed"]),
            })
        return pd.DataFrame(rows)

    overall = per90_table(metrics).melt(id_vars="player_category", var_name="metric", value_name="value")
    fig = px.bar(
        overall, x="metric", y="value", color="player_category", barmode="group",
        color_discrete_map=CATEGORY_COLORS, category_orders={"player_category": CATEGORY_ORDER},
        labels={"metric": "", "value": "Per 90 minutes", "player_category": "Category"},
    )
    st.plotly_chart(fig, use_container_width=True)

    st.markdown("**By position** (goals/assists per 90 only meaningfully compare within a position group):")
    for pos in ["Attacker", "Midfielder", "Defender"]:
        pos_df = metrics[metrics["position"] == pos]
        if pos_df.empty:
            continue
        pos_table = per90_table(pos_df).melt(id_vars="player_category", var_name="metric", value_name="value")
        fig = px.bar(
            pos_table, x="metric", y="value", color="player_category", barmode="group",
            color_discrete_map=CATEGORY_COLORS, category_orders={"player_category": CATEGORY_ORDER},
            labels={"metric": "", "value": "Per 90 minutes", "player_category": "Category"},
            title=pos,
        )
        fig.update_layout(height=300)
        st.plotly_chart(fig, use_container_width=True)

# ---------------------------------------------------------------- Passing
with tab_pass:
    st.caption(FILTER_HINT)
    st.subheader("Pass volume vs. accuracy, by player")
    st.caption("Each dot is one player. Volume (passes attempted per 90) vs. accuracy reveals whether "
               "high accuracy holds up at high volume, or only shows up in players with few attempts.")
    pass_df = metrics[(metrics["totalPass"] > 0) & (metrics["minsPlayed"] > 0)].copy()
    pass_df["Passes per 90"] = pass_df["totalPass"] / (pass_df["minsPlayed"] / 90)
    pass_df["Pass accuracy %"] = pass_df["accuratePass"] / pass_df["totalPass"] * 100
    fig = px.scatter(
        pass_df, x="Passes per 90", y="Pass accuracy %", color="player_category",
        color_discrete_map=CATEGORY_COLORS, category_orders={"player_category": CATEGORY_ORDER},
        hover_name="canonical_name", hover_data={"team_name": True, "position": True, "Passes per 90": ":.1f",
                                                  "Pass accuracy %": ":.1f", "player_category": False},
        labels={"player_category": "Category"},
        opacity=0.75,
    )
    fig.update_traces(marker=dict(size=8))
    st.plotly_chart(fig, use_container_width=True)

# ---------------------------------------------------------------- Defensive performance
with tab_defense:
    st.caption(FILTER_HINT)
    st.subheader("Tackle volume vs. success rate, by player (Defenders & Midfielders)")
    st.caption("Each dot is one player. Volume (tackles attempted per 90) vs. success rate reveals whether "
               "a high tackle-success rate is backed by real defensive workload. Limited to Defenders and "
               "Midfielders, since tackling volume isn't a meaningful comparison for Attackers/Goalkeepers.")
    tackle_df = metrics[
        (metrics["totalTackle"] > 0) & (metrics["minsPlayed"] > 0)
        & (metrics["position"].isin(["Defender", "Midfielder"]))
    ].copy()
    tackle_df["Tackles per 90"] = tackle_df["totalTackle"] / (tackle_df["minsPlayed"] / 90)
    tackle_df["Successful tackle %"] = tackle_df["wonTackle"] / tackle_df["totalTackle"] * 100
    fig = px.scatter(
        tackle_df, x="Tackles per 90", y="Successful tackle %", color="player_category",
        color_discrete_map=CATEGORY_COLORS, category_orders={"player_category": CATEGORY_ORDER},
        hover_name="canonical_name", hover_data={"team_name": True, "position": True, "Tackles per 90": ":.1f",
                                                  "Successful tackle %": ":.1f", "player_category": False},
        labels={"player_category": "Category"},
        opacity=0.75,
    )
    fig.update_traces(marker=dict(size=8))
    st.plotly_chart(fig, use_container_width=True)

# ---------------------------------------------------------------- Leaderboards
with tab_leaderboard:
    st.caption(FILTER_HINT)
    leaderboard_base = metrics
    display_cols = ["canonical_name", "team_name", "player_category", "position", "goals", "goalAssist", "minsPlayed"]
    rename_map = {
        "canonical_name": "Player", "team_name": "Team", "player_category": "Category", "position": "Position",
        "goals": "Goals", "goalAssist": "Assists", "minsPlayed": "Minutes",
    }

    st.subheader("Top scorers" + ("" if selected_team == "All Teams" else f" — {selected_team}"))
    scorers = leaderboard_base.sort_values("goals", ascending=False).head(15)
    st.dataframe(scorers[display_cols].rename(columns=rename_map), use_container_width=True, hide_index=True)

    st.subheader("Top assists" + ("" if selected_team == "All Teams" else f" — {selected_team}"))
    assisters = leaderboard_base.sort_values("goalAssist", ascending=False).head(15)
    st.dataframe(assisters[display_cols].rename(columns=rename_map), use_container_width=True, hide_index=True)
