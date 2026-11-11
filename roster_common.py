"""Identity and team normalization shared by providers and reconciliation."""
import re
import unicodedata
from datetime import date, datetime, timezone

CANONICAL_TEAMS = "ARI ATL BAL BUF CAR CHI CIN CLE DAL DEN DET GB HOU IND JAX KC LAR LAC LV MIA MIN NE NO NYG NYJ PHI PIT SEA SF TB TEN WAS".split()
TEAM_ALIASES = {x: x for x in CANONICAL_TEAMS}
TEAM_ALIASES.update({"JAC": "JAX", "LA": "LAR", "WSH": "WAS", "ARZ": "ARI",
                     "SD": "LAC", "STL": "LAR", "OAK": "LV"})


def nfl_team(team):
    return TEAM_ALIASES.get(str(team).strip().upper())


def normalize_name(name):
    name = unicodedata.normalize("NFKD", str(name)).casefold()
    name = re.sub(r"(?:\s+|,)(jr\.?|sr\.?|ii|iii|iv|v)\s*$", "", name)
    return "".join(c for c in name if c.isalnum() and not unicodedata.combining(c))


def normalize_date(value):
    value = str(value or "").strip().lstrip("'")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return ""
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        return ""


def stamp():
    return datetime.now(timezone.utc).isoformat()


def require_fresh(value, now=None, max_age=86400):
    try:
        fetched = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if fetched.utcoffset() is None:
            raise ValueError("Timezone required")
        age = ((now or datetime.now(timezone.utc)) - fetched).total_seconds()
        if not 0 <= age <= max_age:
            raise ValueError("Stale or future source timestamp; fetch and plan again")
    except (TypeError, AttributeError) as exc:
        raise ValueError("Missing source timestamp") from exc
    return age
