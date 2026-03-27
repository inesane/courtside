"""Sport registry — centralizes sport metadata, teams, and rule builders."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from alerts.base import AlertRule


@dataclass
class SportConfig:
    display_name: str
    sport: str  # ESPN sport identifier
    league: str  # ESPN league identifier
    teams: list[tuple[str, str]]  # (abbrev, full_name)
    build_rules: Callable  # function(alerts_cfg, engine) -> list[AlertRule]
    icon: str = ""


SPORT_REGISTRY: dict[str, SportConfig] = {}


def register_sport(key: str, config: SportConfig) -> None:
    SPORT_REGISTRY[key] = config


def get_sport(key: str) -> SportConfig | None:
    return SPORT_REGISTRY.get(key)


def get_enabled_sports(config: dict[str, Any]) -> list[str]:
    """Return list of sport keys that are enabled in the config."""
    sports_cfg = config.get("sports", {})
    return [k for k in SPORT_REGISTRY if sports_cfg.get(k, {}).get("enabled", False)]


# ---------------------------------------------------------------------------
# NBA
# ---------------------------------------------------------------------------
NBA_TEAMS = [
    ("ATL", "Atlanta Hawks"), ("BOS", "Boston Celtics"), ("BKN", "Brooklyn Nets"),
    ("CHA", "Charlotte Hornets"), ("CHI", "Chicago Bulls"), ("CLE", "Cleveland Cavaliers"),
    ("DAL", "Dallas Mavericks"), ("DEN", "Denver Nuggets"), ("DET", "Detroit Pistons"),
    ("GS", "Golden State Warriors"), ("HOU", "Houston Rockets"), ("IND", "Indiana Pacers"),
    ("LAC", "LA Clippers"), ("LAL", "Los Angeles Lakers"), ("MEM", "Memphis Grizzlies"),
    ("MIA", "Miami Heat"), ("MIL", "Milwaukee Bucks"), ("MIN", "Minnesota Timberwolves"),
    ("NO", "New Orleans Pelicans"), ("NY", "New York Knicks"), ("OKC", "Oklahoma City Thunder"),
    ("ORL", "Orlando Magic"), ("PHI", "Philadelphia 76ers"), ("PHX", "Phoenix Suns"),
    ("POR", "Portland Trail Blazers"), ("SAC", "Sacramento Kings"), ("SA", "San Antonio Spurs"),
    ("TOR", "Toronto Raptors"), ("UTAH", "Utah Jazz"), ("WAS", "Washington Wizards"),
]


def build_nba_rules(alerts_cfg: dict[str, Any], engine=None) -> list[AlertRule]:
    from alerts.nba.rules import (
        BlowoutComebackRule, CloseGameRule, HistoricScoringRule,
        HistoricStatLineRule, OvertimeRule,
    )

    rules: list[AlertRule] = []
    close = alerts_cfg.get("close_game", {})
    if close.get("enabled", True):
        rules.append(CloseGameRule(
            point_threshold=close.get("point_threshold", 5),
            minutes_remaining=close.get("minutes_remaining", 4.0),
            quarters=close.get("quarters", [4, 5, 6, 7]),
        ))

    scoring = alerts_cfg.get("historic_scoring", {})
    if scoring.get("enabled", True):
        rules.append(HistoricScoringRule(
            points_threshold=scoring.get("points_threshold", 50),
        ))

    if alerts_cfg.get("historic_stats", {}).get("enabled", True):
        rules.append(HistoricStatLineRule())

    comeback = alerts_cfg.get("blowout_comeback", {})
    if comeback.get("enabled", False):
        rules.append(BlowoutComebackRule(
            deficit_threshold=comeback.get("deficit_threshold", 20),
            close_threshold=comeback.get("close_threshold", 5),
            engine=engine,
        ))

    if alerts_cfg.get("overtime", {}).get("enabled", True):
        rules.append(OvertimeRule())

    return rules


register_sport("nba", SportConfig(
    display_name="NBA",
    sport="basketball",
    league="nba",
    teams=NBA_TEAMS,
    build_rules=build_nba_rules,
    icon="&#127936;",
))


# ---------------------------------------------------------------------------
# NCAA Basketball (March Madness)
# ---------------------------------------------------------------------------
def build_ncaab_rules(alerts_cfg: dict[str, Any], engine=None) -> list[AlertRule]:
    from alerts.nba.rules import (
        BlowoutComebackRule, CloseGameRule, HistoricScoringRule,
        HistoricStatLineRule, OvertimeRule,
    )
    from alerts.ncaab.rules import UpsetAlertRule

    rules: list[AlertRule] = []
    close = alerts_cfg.get("close_game", {})
    if close.get("enabled", True):
        rules.append(CloseGameRule(
            point_threshold=close.get("point_threshold", 5),
            minutes_remaining=close.get("minutes_remaining", 4.0),
            quarters=close.get("quarters", [2, 3, 4, 5]),  # 2nd half + OT
        ))

    scoring = alerts_cfg.get("historic_scoring", {})
    if scoring.get("enabled", True):
        rules.append(HistoricScoringRule(
            points_threshold=scoring.get("points_threshold", 40),
        ))

    if alerts_cfg.get("historic_stats", {}).get("enabled", True):
        rules.append(HistoricStatLineRule())

    upset = alerts_cfg.get("upset_alert", {})
    if upset.get("enabled", True):
        rules.append(UpsetAlertRule(
            seed_difference=upset.get("seed_difference", 5),
        ))

    comeback = alerts_cfg.get("blowout_comeback", {})
    if comeback.get("enabled", False):
        rules.append(BlowoutComebackRule(
            deficit_threshold=comeback.get("deficit_threshold", 15),
            close_threshold=comeback.get("close_threshold", 5),
            engine=engine,
        ))

    if alerts_cfg.get("overtime", {}).get("enabled", True):
        rules.append(OvertimeRule())

    return rules


NCAAB_TEAMS = [
    # ACC
    ("DUKE", "Duke Blue Devils"), ("UNC", "North Carolina Tar Heels"),
    ("UVA", "Virginia Cavaliers"), ("CLEM", "Clemson Tigers"),
    ("NCSU", "NC State Wolfpack"), ("VT", "Virginia Tech Hokies"),
    ("WAKE", "Wake Forest Demon Deacons"), ("SYR", "Syracuse Orange"),
    ("PITT", "Pittsburgh Panthers"), ("ND", "Notre Dame Fighting Irish"),
    ("MIA", "Miami Hurricanes"), ("FSU", "Florida State Seminoles"),
    ("SMU", "SMU Mustangs"), ("STAN", "Stanford Cardinal"),
    # SEC
    ("AUB", "Auburn Tigers"), ("ALA", "Alabama Crimson Tide"),
    ("TENN", "Tennessee Volunteers"), ("UK", "Kentucky Wildcats"),
    ("FLA", "Florida Gators"), ("TA&M", "Texas A&M Aggies"),
    ("ARK", "Arkansas Razorbacks"), ("LSU", "LSU Tigers"),
    ("SC", "South Carolina Gamecocks"), ("MIZ", "Missouri Tigers"),
    ("MSST", "Mississippi State Bulldogs"), ("UGA", "Georgia Bulldogs"),
    ("TEX", "Texas Longhorns"), ("OU", "Oklahoma Sooners"),
    ("OKST", "Oklahoma State Cowboys"),
    # Big Ten
    ("PUR", "Purdue Boilermakers"), ("IU", "Indiana Hoosiers"),
    ("MSU", "Michigan State Spartans"), ("MICH", "Michigan Wolverines"),
    ("ILL", "Illinois Fighting Illini"), ("IOWA", "Iowa Hawkeyes"),
    ("WIS", "Wisconsin Badgers"), ("MD", "Maryland Terrapins"),
    ("NEB", "Nebraska Cornhuskers"), ("OSU", "Ohio State Buckeyes"),
    ("PSU", "Penn State Nittany Lions"), ("UCLA", "UCLA Bruins"),
    ("USC", "USC Trojans"), ("ORE", "Oregon Ducks"), ("WASH", "Washington Huskies"),
    # Big 12
    ("KU", "Kansas Jayhawks"), ("BAY", "Baylor Bears"),
    ("HOU", "Houston Cougars"), ("ISU", "Iowa State Cyclones"),
    ("TTU", "Texas Tech Red Raiders"), ("TCU", "TCU Horned Frogs"),
    ("KSU", "Kansas State Wildcats"), ("WVU", "West Virginia Mountaineers"),
    ("BYU", "BYU Cougars"), ("CIN", "Cincinnati Bearcats"),
    ("COLO", "Colorado Buffaloes"), ("ARIZ", "Arizona Wildcats"),
    # Big East
    ("CONN", "UConn Huskies"), ("MARQ", "Marquette Golden Eagles"),
    ("CREI", "Creighton Bluejays"), ("VILL", "Villanova Wildcats"),
    ("SJU", "St. John's Red Storm"), ("XAV", "Xavier Musketeers"),
    ("GTWN", "Georgetown Hoyas"), ("PROV", "Providence Friars"),
    # Other notable programs
    ("GONZ", "Gonzaga Bulldogs"), ("SDSU", "San Diego State Aztecs"),
    ("MEM", "Memphis Tigers"), ("FAU", "Florida Atlantic Owls"),
    ("DAY", "Dayton Flyers"),
]

register_sport("ncaab", SportConfig(
    display_name="March Madness",
    sport="basketball",
    league="mens-college-basketball",
    teams=NCAAB_TEAMS,
    build_rules=build_ncaab_rules,
    icon="&#127941;",
))


# ---------------------------------------------------------------------------
# NFL
# ---------------------------------------------------------------------------
NFL_TEAMS = [
    ("ARI", "Arizona Cardinals"), ("ATL", "Atlanta Falcons"), ("BAL", "Baltimore Ravens"),
    ("BUF", "Buffalo Bills"), ("CAR", "Carolina Panthers"), ("CHI", "Chicago Bears"),
    ("CIN", "Cincinnati Bengals"), ("CLE", "Cleveland Browns"), ("DAL", "Dallas Cowboys"),
    ("DEN", "Denver Broncos"), ("DET", "Detroit Lions"), ("GB", "Green Bay Packers"),
    ("HOU", "Houston Texans"), ("IND", "Indianapolis Colts"), ("JAX", "Jacksonville Jaguars"),
    ("KC", "Kansas City Chiefs"), ("LV", "Las Vegas Raiders"), ("LAC", "Los Angeles Chargers"),
    ("LAR", "Los Angeles Rams"), ("MIA", "Miami Dolphins"), ("MIN", "Minnesota Vikings"),
    ("NE", "New England Patriots"), ("NO", "New Orleans Saints"), ("NYG", "New York Giants"),
    ("NYJ", "New York Jets"), ("PHI", "Philadelphia Eagles"), ("PIT", "Pittsburgh Steelers"),
    ("SF", "San Francisco 49ers"), ("SEA", "Seattle Seahawks"), ("TB", "Tampa Bay Buccaneers"),
    ("TEN", "Tennessee Titans"), ("WAS", "Washington Commanders"),
]


def build_nfl_rules(alerts_cfg: dict[str, Any], engine=None) -> list[AlertRule]:
    from alerts.nba.rules import CloseGameRule, OvertimeRule, BlowoutComebackRule
    from alerts.nfl.rules import HighScoringQBRule, BigRushingGameRule

    rules: list[AlertRule] = []
    close = alerts_cfg.get("close_game", {})
    if close.get("enabled", True):
        rules.append(CloseGameRule(
            point_threshold=close.get("point_threshold", 7),
            minutes_remaining=close.get("minutes_remaining", 4.0),
            quarters=close.get("quarters", [4, 5, 6]),
        ))

    qb = alerts_cfg.get("high_scoring_qb", {})
    if qb.get("enabled", True):
        rules.append(HighScoringQBRule(
            td_threshold=qb.get("td_threshold", 4),
            yards_threshold=qb.get("yards_threshold", 400),
        ))

    rushing = alerts_cfg.get("big_rushing_game", {})
    if rushing.get("enabled", True):
        rules.append(BigRushingGameRule(
            yards_threshold=rushing.get("yards_threshold", 150),
            td_threshold=rushing.get("td_threshold", 3),
        ))

    comeback = alerts_cfg.get("blowout_comeback", {})
    if comeback.get("enabled", False):
        rules.append(BlowoutComebackRule(
            deficit_threshold=comeback.get("deficit_threshold", 17),
            close_threshold=comeback.get("close_threshold", 7),
            engine=engine,
        ))

    if alerts_cfg.get("overtime", {}).get("enabled", True):
        rules.append(OvertimeRule())

    return rules


register_sport("nfl", SportConfig(
    display_name="NFL",
    sport="football",
    league="nfl",
    teams=NFL_TEAMS,
    build_rules=build_nfl_rules,
    icon="&#127944;",
))


# ---------------------------------------------------------------------------
# Soccer
# ---------------------------------------------------------------------------
SOCCER_LEAGUES = {
    "eng.1": "Premier League",
    "uefa.champions": "Champions League",
    "esp.1": "La Liga",
    "usa.1": "MLS",
}


def build_soccer_rules(alerts_cfg: dict[str, Any], engine=None) -> list[AlertRule]:
    from alerts.soccer.rules import (
        LateGoalRule, EqualizerRule, ComebackRule,
        RedCardRule, ExtraTimeRule,
    )

    rules: list[AlertRule] = []

    late = alerts_cfg.get("late_goal", {})
    if late.get("enabled", True):
        rules.append(LateGoalRule(
            minute_threshold=late.get("minute_threshold", 80),
        ))

    eq = alerts_cfg.get("equalizer", {})
    if eq.get("enabled", True):
        rules.append(EqualizerRule(
            minute_threshold=eq.get("minute_threshold", 75),
        ))

    comeback = alerts_cfg.get("comeback", {})
    if comeback.get("enabled", True):
        rules.append(ComebackRule(
            deficit_threshold=comeback.get("deficit_threshold", 2),
            engine=engine,
        ))

    if alerts_cfg.get("red_card", {}).get("enabled", True):
        rules.append(RedCardRule())

    if alerts_cfg.get("extra_time", {}).get("enabled", True):
        rules.append(ExtraTimeRule())

    return rules


SOCCER_TEAMS = {
    "eng.1": [
        ("ARS", "Arsenal"), ("AVL", "Aston Villa"), ("BOU", "AFC Bournemouth"),
        ("BRE", "Brentford"), ("BHA", "Brighton & Hove Albion"), ("CHE", "Chelsea"),
        ("CRY", "Crystal Palace"), ("EVE", "Everton"), ("FUL", "Fulham"),
        ("LIV", "Liverpool"), ("MNC", "Manchester City"), ("MAN", "Manchester United"),
        ("NEW", "Newcastle United"), ("NFO", "Nottingham Forest"), ("TOT", "Tottenham Hotspur"),
        ("WHU", "West Ham United"), ("WOL", "Wolverhampton Wanderers"),
    ],
    "esp.1": [
        ("ATM", "Atlético Madrid"), ("BAR", "Barcelona"), ("RMA", "Real Madrid"),
        ("SEV", "Sevilla"), ("RSO", "Real Sociedad"), ("BET", "Real Betis"),
        ("VIL", "Villarreal"), ("ATH", "Athletic Club"), ("CEL", "Celta Vigo"),
        ("GIR", "Girona"), ("MLL", "Mallorca"), ("OSA", "Osasuna"),
        ("VAL", "Valencia"), ("GET", "Getafe"), ("ALA", "Alavés"),
        ("RAY", "Rayo Vallecano"), ("ESP", "Espanyol"), ("LEV", "Levante"),
    ],
    "usa.1": [
        ("ATL", "Atlanta United FC"), ("ATX", "Austin FC"), ("MTL", "CF Montréal"),
        ("CLT", "Charlotte FC"), ("CHI", "Chicago Fire FC"), ("COL", "Colorado Rapids"),
        ("CLB", "Columbus Crew"), ("DC", "D.C. United"), ("CIN", "FC Cincinnati"),
        ("DAL", "FC Dallas"), ("HOU", "Houston Dynamo FC"), ("MIA", "Inter Miami CF"),
        ("LA", "LA Galaxy"), ("LAFC", "LAFC"), ("MIN", "Minnesota United FC"),
        ("NSH", "Nashville SC"), ("NE", "New England Revolution"), ("NYC", "New York City FC"),
        ("ORL", "Orlando City SC"), ("PHI", "Philadelphia Union"), ("POR", "Portland Timbers"),
        ("RSL", "Real Salt Lake"), ("RBNY", "Red Bull New York"), ("SD", "San Diego FC"),
        ("SJ", "San Jose Earthquakes"), ("SEA", "Seattle Sounders FC"),
        ("SKC", "Sporting Kansas City"), ("STL", "St. Louis CITY SC"), ("TOR", "Toronto FC"),
    ],
    "uefa.champions": [
        ("ARS", "Arsenal"), ("ATM", "Atlético Madrid"), ("BAR", "Barcelona"),
        ("MUN", "Bayern Munich"), ("DOR", "Borussia Dortmund"), ("CHE", "Chelsea"),
        ("INT", "Internazionale"), ("JUV", "Juventus"), ("LIV", "Liverpool"),
        ("MNC", "Manchester City"), ("NAP", "Napoli"), ("PSG", "Paris Saint-Germain"),
        ("RMA", "Real Madrid"), ("B04", "Bayer Leverkusen"), ("SLB", "Benfica"),
        ("ATA", "Atalanta"), ("TOT", "Tottenham Hotspur"), ("NEW", "Newcastle United"),
    ],
}

# Flatten all soccer teams for the registry (deduplicated)
_all_soccer_teams: list[tuple[str, str]] = []
_seen_soccer: set[str] = set()
for _league_teams in SOCCER_TEAMS.values():
    for _abbrev, _name in _league_teams:
        if _abbrev not in _seen_soccer:
            _all_soccer_teams.append((_abbrev, _name))
            _seen_soccer.add(_abbrev)

register_sport("soccer", SportConfig(
    display_name="Soccer",
    sport="soccer",
    league="eng.1",
    teams=_all_soccer_teams,
    build_rules=build_soccer_rules,
    icon="&#9917;",
))
