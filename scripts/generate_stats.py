#!/usr/bin/env python3
"""
Generate SVG cards for a GitHub profile README.

Outputs:
    assets/stack.svg
    assets/activity.svg

Required environment variables:
    GH_USERNAME    GitHub username.
    GH_TOKEN       GitHub token. In GitHub Actions, use secrets.GITHUB_TOKEN.

Optional environment variables:
    INCLUDE_FORKS          "true" to include forked repositories. Default: false
    INCLUDE_ARCHIVED       "true" to include archived repositories. Default: false
    INCLUDE_PRIVATE        "true" to include private repositories when the
                           token has permission. Default: false
    INCLUDE_ORG_REPOS       "true" to include organization/collaborator
                           repositories. Default: true
    EXCLUDED_REPOS         Comma-separated repository names to exclude.
    EXCLUDED_LANGUAGES     Comma-separated languages to exclude.
                           Default: HTML,CSS,Shell
    TOP_LANGUAGES          Number of languages shown. Default: 6
    CARD_THEME             "dark" or "light". Default: dark

Notes:
    - Repository languages are calculated from GitHub's language byte counts.
    - Activity uses GitHub's contribution calendar for the latest 12-month range.
    - GITHUB_TOKEN is normally limited to the repository containing the workflow.
      Public repositories can still be aggregated. To include private repositories
      outside the profile repository, use a token or GitHub App with suitable access.
"""

from __future__ import annotations

import calendar
import html
import json
import math
import os
import sys
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable


GRAPHQL_URL = "https://api.github.com/graphql"
API_VERSION = "2022-11-28"
OUTPUT_DIR = Path("assets")


@dataclass(frozen=True)
class Theme:
    background: str
    border: str
    title: str
    text: str
    muted: str
    accent: str
    grid: str
    bar_background: str


THEMES = {
    "dark": Theme(
        background="#0d1117",
        border="#30363d",
        title="#f0f6fc",
        text="#c9d1d9",
        muted="#8b949e",
        accent="#58a6ff",
        grid="#21262d",
        bar_background="#21262d",
    ),
    "light": Theme(
        background="#ffffff",
        border="#d0d7de",
        title="#1f2328",
        text="#24292f",
        muted="#656d76",
        accent="#0969da",
        grid="#d8dee4",
        bar_background="#eaeef2",
    ),
}


class GitHubAPIError(RuntimeError):
    """Raised when GitHub's GraphQL API returns an error."""


def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def env_csv(name: str, default: str = "") -> set[str]:
    value = os.getenv(name, default)
    return {item.strip() for item in value.split(",") if item.strip()}


def graphql_request(
    token: str,
    query: str,
    variables: dict[str, Any],
) -> dict[str, Any]:
    payload = json.dumps({"query": query, "variables": variables}).encode("utf-8")
    request = urllib.request.Request(
        GRAPHQL_URL,
        data=payload,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": API_VERSION,
            "User-Agent": "github-profile-stats-generator",
        },
    )

    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            result = json.load(response)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise GitHubAPIError(
            f"GitHub API returned HTTP {exc.code}: {body}"
        ) from exc
    except urllib.error.URLError as exc:
        raise GitHubAPIError(f"Could not connect to GitHub API: {exc}") from exc

    if result.get("errors"):
        messages = "; ".join(
            error.get("message", "Unknown GraphQL error")
            for error in result["errors"]
        )
        raise GitHubAPIError(messages)

    data = result.get("data")
    if data is None:
        raise GitHubAPIError("GitHub API response did not contain a data field.")

    return data


def iso_utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def get_date_range() -> tuple[datetime, datetime]:
    """
    Return a valid contribution range of at most one year.

    Using 364 days avoids edge cases around leap years while still providing
    a clean 12-month profile view.
    """
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=364)
    return start, end


def fetch_profile_data(
    username: str,
    token: str,
    include_private: bool,
    include_org_repos: bool,
) -> dict[str, Any]:
    """
    Fetch contribution totals and every repository visible to the token.

    Public/private filtering is performed in Python because omitting GitHub's
    `privacy` argument allows the same query to return both kinds of repositories.
    """
    start, end = get_date_range()

    contribution_query = """
    query ProfileContributions(
      $login: String!,
      $from: DateTime!,
      $to: DateTime!
    ) {
      user(login: $login) {
        login
        name
        contributionsCollection(from: $from, to: $to) {
          totalCommitContributions
          totalPullRequestContributions
          totalIssueContributions
          totalPullRequestReviewContributions
          restrictedContributionsCount
          contributionCalendar {
            totalContributions
            weeks {
              contributionDays {
                date
                contributionCount
              }
            }
          }
        }
      }
    }
    """

    contribution_data = graphql_request(
        token,
        contribution_query,
        {
            "login": username,
            "from": iso_utc(start),
            "to": iso_utc(end),
        },
    )

    user = contribution_data.get("user")
    if not user:
        raise GitHubAPIError(f"GitHub user '{username}' was not found.")

    affiliations = (
        "[OWNER, ORGANIZATION_MEMBER, COLLABORATOR]"
        if include_org_repos
        else "[OWNER]"
    )

    repository_query = f"""
    query ProfileRepositories(
      $login: String!,
      $after: String
    ) {{
      user(login: $login) {{
        repositories(
          first: 100,
          after: $after,
          ownerAffiliations: {affiliations},
          orderBy: {{field: UPDATED_AT, direction: DESC}}
        ) {{
          pageInfo {{
            hasNextPage
            endCursor
          }}
          nodes {{
            name
            nameWithOwner
            isFork
            isArchived
            isPrivate
            languages(first: 20, orderBy: {{field: SIZE, direction: DESC}}) {{
              edges {{
                size
                node {{
                  name
                  color
                }}
              }}
            }}
          }}
        }}
      }}
    }}
    """

    repositories: list[dict[str, Any]] = []
    cursor: str | None = None

    while True:
        repository_data = graphql_request(
            token,
            repository_query,
            {
                "login": username,
                "after": cursor,
            },
        )

        repository_user = repository_data.get("user")
        if not repository_user:
            raise GitHubAPIError(f"GitHub user '{username}' was not found.")

        connection = repository_user["repositories"]

        for repository in connection["nodes"]:
            if repository["isPrivate"] and not include_private:
                continue
            repositories.append(repository)

        page_info = connection["pageInfo"]
        if not page_info["hasNextPage"]:
            break

        cursor = page_info["endCursor"]
        if not cursor:
            raise GitHubAPIError(
                "GitHub reported another repository page but returned no cursor."
            )

    user["repositories"] = {"nodes": repositories}
    return user


def collect_languages(
    repositories: Iterable[dict[str, Any]],
    include_forks: bool,
    include_archived: bool,
    excluded_repos: set[str],
    excluded_languages: set[str],
) -> tuple[Counter[str], dict[str, str]]:
    totals: Counter[str] = Counter()
    colors: dict[str, str] = {}

    for repo in repositories:
        if repo["name"] in excluded_repos:
            continue
        if repo["isFork"] and not include_forks:
            continue
        if repo["isArchived"] and not include_archived:
            continue

        for edge in repo["languages"]["edges"]:
            language = edge["node"]["name"]
            if language in excluded_languages:
                continue

            totals[language] += int(edge["size"])
            color = edge["node"].get("color")
            if color:
                colors[language] = color

    return totals, colors


def month_labels(start: datetime, end: datetime) -> list[tuple[int, int]]:
    labels: list[tuple[int, int]] = []
    cursor = date(start.year, start.month, 1)
    end_month = date(end.year, end.month, 1)

    while cursor <= end_month:
        labels.append((cursor.year, cursor.month))
        if cursor.month == 12:
            cursor = date(cursor.year + 1, 1, 1)
        else:
            cursor = date(cursor.year, cursor.month + 1, 1)

    # A 364-day period can touch 13 calendar months. Show the latest 12.
    return labels[-12:]


def collect_monthly_contributions(
    weeks: list[dict[str, Any]],
    start: datetime,
    end: datetime,
) -> list[dict[str, Any]]:
    counts: defaultdict[tuple[int, int], int] = defaultdict(int)

    for week in weeks:
        for day in week["contributionDays"]:
            parsed = date.fromisoformat(day["date"])
            counts[(parsed.year, parsed.month)] += int(day["contributionCount"])

    result = []
    for year, month in month_labels(start, end):
        result.append(
            {
                "year": year,
                "month": month,
                "label": f"{calendar.month_abbr[month]}",
                "count": counts[(year, month)],
            }
        )
    return result


def escape(value: Any) -> str:
    return html.escape(str(value), quote=True)


def svg_text(
    x: float,
    y: float,
    text: Any,
    *,
    size: int = 14,
    fill: str = "#c9d1d9",
    weight: int = 400,
    anchor: str = "start",
    family: str = "-apple-system,BlinkMacSystemFont,Segoe UI,Helvetica,Arial,sans-serif",
) -> str:
    return (
        f'<text x="{x:.1f}" y="{y:.1f}" font-family="{escape(family)}" '
        f'font-size="{size}" font-weight="{weight}" fill="{fill}" '
        f'text-anchor="{anchor}">{escape(text)}</text>'
    )


def svg_card_start(width: int, height: int, theme: Theme, title: str) -> list[str]:
    return [
        (
            f'<svg xmlns="http://www.w3.org/2000/svg" '
            f'width="{width}" height="{height}" viewBox="0 0 {width} {height}" '
            f'role="img" aria-label="{escape(title)}">'
        ),
        "<style>"
        "text{font-variant-ligatures:none}"
        "@media (prefers-reduced-motion: reduce){*{animation:none!important}}"
        "</style>",
        (
            f'<rect x="0.5" y="0.5" width="{width - 1}" height="{height - 1}" '
            f'rx="12" fill="{theme.background}" stroke="{theme.border}"/>'
        ),
    ]


def render_stack_svg(
    language_totals: Counter[str],
    language_colors: dict[str, str],
    top_n: int,
    theme: Theme,
) -> str:
    width = 760
    row_height = 42
    padding_top = 82

    top = language_totals.most_common(max(top_n, 1))
    height = padding_top + max(len(top), 1) * row_height + 28
    svg = svg_card_start(width, height, theme, "Most used languages")

    total_bytes = sum(size for _, size in top)
    svg.append(svg_text(28, 38, "CODE DISTRIBUTION", size=18, fill=theme.title, weight=700))
    svg.append(
        svg_text(
            28,
            61,
            "Based on language bytes across selected accessible repositories",
            size=12,
            fill=theme.muted,
        )
    )

    if not top or total_bytes == 0:
        svg.append(
            svg_text(
                width / 2,
                112,
                "No language data found",
                size=15,
                fill=theme.muted,
                anchor="middle",
            )
        )
        svg.append("</svg>")
        return "\n".join(svg)

    label_x = 28
    bar_x = 160
    bar_width = 500
    percent_x = 724

    for index, (language, size) in enumerate(top):
        y = padding_top + index * row_height
        percentage = size / total_bytes * 100
        filled_width = max(2, bar_width * percentage / 100)
        language_color = language_colors.get(language, theme.accent)

        svg.append(
            svg_text(label_x, y + 17, language, size=14, fill=theme.text, weight=600)
        )
        svg.append(
            f'<rect x="{bar_x}" y="{y + 5}" width="{bar_width}" height="14" '
            f'rx="7" fill="{theme.bar_background}"/>'
        )
        svg.append(
            f'<rect x="{bar_x}" y="{y + 5}" width="{filled_width:.1f}" height="14" '
            f'rx="7" fill="{escape(language_color)}"/>'
        )
        svg.append(
            svg_text(
                percent_x,
                y + 17,
                f"{percentage:.1f}%",
                size=13,
                fill=theme.muted,
                weight=600,
                anchor="end",
            )
        )

    svg.append("</svg>")
    return "\n".join(svg)


def nice_maximum(value: int) -> int:
    if value <= 0:
        return 10

    magnitude = 10 ** math.floor(math.log10(value))
    normalized = value / magnitude

    if normalized <= 1:
        nice = 1
    elif normalized <= 2:
        nice = 2
    elif normalized <= 5:
        nice = 5
    else:
        nice = 10

    return int(nice * magnitude)


def render_activity_svg(
    monthly: list[dict[str, Any]],
    contributions: dict[str, Any],
    theme: Theme,
) -> str:
    width = 760
    height = 390
    svg = svg_card_start(width, height, theme, "GitHub development activity")

    total = int(contributions["contributionCalendar"]["totalContributions"])
    commits = int(contributions["totalCommitContributions"])
    pull_requests = int(contributions["totalPullRequestContributions"])
    reviews = int(contributions["totalPullRequestReviewContributions"])
    issues = int(contributions["totalIssueContributions"])
    restricted = int(contributions.get("restrictedContributionsCount", 0))

    svg.append(svg_text(28, 38, "DEVELOPMENT ACTIVITY", size=18, fill=theme.title, weight=700))
    svg.append(
        svg_text(
            28,
            61,
            "Monthly GitHub contributions over the latest 12-month range",
            size=12,
            fill=theme.muted,
        )
    )

    metrics = [
        ("Contributions", total),
        ("Commits", commits),
        ("Pull requests", pull_requests),
        ("Reviews", reviews),
        ("Issues", issues),
    ]
    metric_width = (width - 56) / len(metrics)

    for index, (label, value) in enumerate(metrics):
        x = 28 + metric_width * index
        svg.append(svg_text(x, 101, f"{value:,}", size=22, fill=theme.title, weight=700))
        svg.append(svg_text(x, 122, label, size=11, fill=theme.muted))

    chart_left = 48
    chart_right = width - 28
    chart_top = 158
    chart_bottom = 328
    chart_width = chart_right - chart_left
    chart_height = chart_bottom - chart_top

    maximum = nice_maximum(max((item["count"] for item in monthly), default=0))

    for step in range(5):
        fraction = step / 4
        y = chart_bottom - chart_height * fraction
        value = round(maximum * fraction)
        svg.append(
            f'<line x1="{chart_left}" y1="{y:.1f}" x2="{chart_right}" y2="{y:.1f}" '
            f'stroke="{theme.grid}" stroke-width="1"/>'
        )
        svg.append(
            svg_text(
                chart_left - 10,
                y + 4,
                value,
                size=10,
                fill=theme.muted,
                anchor="end",
            )
        )

    if monthly:
        point_gap = chart_width / max(len(monthly) - 1, 1)
        points: list[tuple[float, float]] = []

        for index, item in enumerate(monthly):
            x = chart_left + index * point_gap
            y = chart_bottom - (item["count"] / maximum) * chart_height
            points.append((x, y))

        path = " ".join(
            ("M" if index == 0 else "L") + f" {x:.1f} {y:.1f}"
            for index, (x, y) in enumerate(points)
        )
        area_path = (
            f"M {points[0][0]:.1f} {chart_bottom:.1f} "
            + " ".join(f"L {x:.1f} {y:.1f}" for x, y in points)
            + f" L {points[-1][0]:.1f} {chart_bottom:.1f} Z"
        )

        svg.append(
            "<defs>"
            f'<linearGradient id="activityFill" x1="0" y1="0" x2="0" y2="1">'
            f'<stop offset="0%" stop-color="{theme.accent}" stop-opacity="0.30"/>'
            f'<stop offset="100%" stop-color="{theme.accent}" stop-opacity="0.02"/>'
            "</linearGradient>"
            "</defs>"
        )
        svg.append(f'<path d="{area_path}" fill="url(#activityFill)"/>')
        svg.append(
            f'<path d="{path}" fill="none" stroke="{theme.accent}" '
            f'stroke-width="3" stroke-linecap="round" stroke-linejoin="round"/>'
        )

        for index, ((x, y), item) in enumerate(zip(points, monthly)):
            svg.append(
                f'<circle cx="{x:.1f}" cy="{y:.1f}" r="4" '
                f'fill="{theme.background}" stroke="{theme.accent}" stroke-width="2"/>'
            )
            svg.append(
                f"<title>{escape(item['label'])}: {item['count']} contributions</title>"
            )
            svg.append(
                svg_text(
                    x,
                    chart_bottom + 24,
                    item["label"],
                    size=10,
                    fill=theme.muted,
                    anchor="middle",
                )
            )

    footer = "Authenticated GitHub activity"
    if restricted:
        footer += f" · {restricted:,} restricted contributions"
    svg.append(svg_text(28, 368, footer, size=11, fill=theme.muted))
    svg.append("</svg>")
    return "\n".join(svg)


def write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(content + "\n", encoding="utf-8")
    temporary.replace(path)


def main() -> int:
    username = os.getenv("GH_USERNAME", "").strip()
    token = os.getenv("GH_TOKEN", "").strip()

    if not username:
        print("Error: GH_USERNAME is required.", file=sys.stderr)
        return 2
    if not token:
        print("Error: GH_TOKEN is required.", file=sys.stderr)
        return 2

    include_forks = env_bool("INCLUDE_FORKS")
    include_archived = env_bool("INCLUDE_ARCHIVED")
    include_private = env_bool("INCLUDE_PRIVATE")
    include_org_repos = env_bool("INCLUDE_ORG_REPOS", True)
    excluded_repos = env_csv("EXCLUDED_REPOS")
    excluded_languages = env_csv("EXCLUDED_LANGUAGES", "HTML,CSS,Shell")

    try:
        top_languages = max(1, int(os.getenv("TOP_LANGUAGES", "6")))
    except ValueError:
        print("Error: TOP_LANGUAGES must be an integer.", file=sys.stderr)
        return 2

    theme_name = os.getenv("CARD_THEME", "dark").strip().lower()
    theme = THEMES.get(theme_name)
    if theme is None:
        print(
            f"Error: CARD_THEME must be one of: {', '.join(THEMES)}.",
            file=sys.stderr,
        )
        return 2

    try:
        user = fetch_profile_data(
            username,
            token,
            include_private,
            include_org_repos,
        )
    except GitHubAPIError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    repositories = user["repositories"]["nodes"]
    language_totals, language_colors = collect_languages(
        repositories=repositories,
        include_forks=include_forks,
        include_archived=include_archived,
        excluded_repos=excluded_repos,
        excluded_languages=excluded_languages,
    )

    start, end = get_date_range()
    contributions = user["contributionsCollection"]
    monthly = collect_monthly_contributions(
        contributions["contributionCalendar"]["weeks"],
        start,
        end,
    )

    write_text(
        OUTPUT_DIR / "stack.svg",
        render_stack_svg(language_totals, language_colors, top_languages, theme),
    )
    write_text(
        OUTPUT_DIR / "activity.svg",
        render_activity_svg(monthly, contributions, theme),
    )

    print(f"Generated {OUTPUT_DIR / 'stack.svg'}")
    print(f"Generated {OUTPUT_DIR / 'activity.svg'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
