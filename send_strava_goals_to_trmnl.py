#!/usr/bin/env python3
import datetime as dt
import json
import os
import sys
from typing import Any, Dict, List, Optional, Tuple, TypeAlias, cast

import requests
import yaml
from dotenv import load_dotenv
from stravalib.client import Client

load_dotenv()

if os.environ.get("CI") and os.environ.get("STRAVA_CREDENTIALS"):
    with open(".strava-credentials", "w") as f:
        f.write(os.environ["STRAVA_CREDENTIALS"])


class StravaError(Exception):
    """Base exception for all Strava-related errors."""

    pass


class ConfigError(StravaError):
    """Raised when configuration is invalid or missing."""

    pass


class AuthError(StravaError):
    """Raised for authentication-related errors."""

    pass


class Config:
    def __init__(self, client_id: str, client_secret: str) -> None:
        self.client_id = client_id
        self.client_secret = client_secret
        self.credentials_file = ".strava-credentials"
        self.token_refresh_hours = 6

    @classmethod
    def from_environment(cls) -> "Config":
        """Creates a config object from environment variables.

        :raises ConfigError: If required environment variables are missing
        """
        required = ["STRAVA_CLIENT_ID", "STRAVA_CLIENT_SECRET"]
        missing = [var for var in required if var not in os.environ]

        if missing:
            raise ConfigError(
                f"Missing required environment variables: {', '.join(missing)}\n"
                f"Please set in .env: STRAVA_CLIENT_ID, STRAVA_CLIENT_SECRET"
            )

        return cls(
            client_id=os.environ["STRAVA_CLIENT_ID"],
            client_secret=os.environ["STRAVA_CLIENT_SECRET"],
        )


# Define a type alias for the token dictionary
TokenDict: TypeAlias = Dict[str, Any]


class TokenManager:
    def __init__(self, config: Config):
        self.config = config

    def get_valid_token(self) -> TokenDict:
        """Returns a valid access token, performing auth flow if needed."""
        if not os.path.exists(self.config.credentials_file):
            token = self._perform_oauth_flow()
            self._save_token(token)
            return token

        token = self._load_token()
        if self._needs_refresh(token):
            token = self._refresh_token(token["refresh_token"])
            self._save_token(token)
        return token

    def _perform_oauth_flow(self) -> TokenDict:
        """Performs the initial OAuth flow."""
        if os.environ.get("CI"):
            raise AuthError(
                "Running in CI environment, please configure token manually first"
            )

        # Original OAuth flow for local development
        client = Client()
        auth_url = client.authorization_url(
            client_id=int(self.config.client_id),
            redirect_uri="http://localhost:8000/authorized",
            scope=["read_all", "profile:read_all", "activity:read_all"],
        )
        print(f"Please visit: {auth_url}")
        code = input("Enter the code from the redirect URL: ")

        token = client.exchange_code_for_token(
            client_id=int(self.config.client_id),
            client_secret=self.config.client_secret,
            code=code,
        )
        if not token:
            raise AuthError("Failed to obtain access token")

        return cast(TokenDict, token)

    def _needs_refresh(self, token: TokenDict) -> bool:
        """Checks if the token needs refreshing based on file modification time."""
        hours_since_refresh = (
            dt.datetime.now().timestamp()
            - os.path.getmtime(self.config.credentials_file)
        ) / 3600.0
        return hours_since_refresh > self.config.token_refresh_hours

    def _refresh_token(self, refresh_token: str) -> TokenDict:
        """Refreshes an expired access token."""
        response = requests.post(
            "https://www.strava.com/api/v3/oauth/token",
            data={
                "client_id": self.config.client_id,
                "client_secret": self.config.client_secret,
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
            },
        )
        if not response.ok:
            raise AuthError("Failed to refresh token")
        return cast(TokenDict, response.json())

    def _save_token(self, token: TokenDict) -> None:
        """Saves token data to disk."""
        with open(self.config.credentials_file, "w") as f:
            json.dump(token, f)

    def _load_token(self) -> TokenDict:
        """Loads token data from disk."""
        try:
            with open(self.config.credentials_file, "r") as f:
                return json.load(f)
        except (IOError, json.JSONDecodeError) as e:
            raise AuthError(f"Failed to load credentials: {e}")


class Strava:
    def __init__(self, config: Config):
        self.config = config
        self.token_manager = TokenManager(config)
        self._client: Optional[Client] = None

    @property
    def client(self) -> Client:
        """Lazy-loaded Strava client instance."""
        if self._client is None:
            token = self.token_manager.get_valid_token()
            self._client = Client(access_token=str(token["access_token"]))
            assert self._client is not None
        return self._client

    def get_summary(self) -> Dict[str, Any]:
        """Structures summary of running activity for TRMNL."""
        stats = self.client.get_athlete_stats()
        ytd_distance = self._get_ytd_distance(stats)
        runs = self._get_weekly_runs()

        if not runs:
            return {}

        week_distance = self._calculate_weekly_distance(runs)
        return self._structure_summary(week_distance, ytd_distance)

    def _get_ytd_distance(self, stats: Any) -> float:
        """Extracts year-to-date running distance in kilometers."""
        ytd = getattr(stats, "ytd_run_totals", None)
        return (float(ytd.distance) / 1000.0) if ytd else 0

    def _get_weekly_runs(self) -> List[Any]:
        """Fetches this week's running activities."""
        today = dt.datetime.now()
        week_start = today - dt.timedelta(days=today.weekday())
        week_start = week_start.replace(hour=0, minute=0, second=0, microsecond=0)

        activities = self.client.get_activities(after=week_start)
        return [act for act in activities if act.type == "Run"]

    def _calculate_weekly_distance(self, runs: List[Any]) -> float:
        """Calculates total distance for provided runs in kilometers."""
        return sum(float(run.distance) for run in runs) / 1000.0

    def _load_goals(self) -> Tuple[float, float]:
        """Loads running goals from goals.yml file."""
        try:
            with open("goals.yml", "r") as f:
                goals = yaml.safe_load(f)
                return float(goals.get("weekly", 0)), float(goals.get("yearly", 1000))
        except (IOError, yaml.YAMLError):
            return 0, 0

    def _structure_summary(
        self, week_distance: float, ytd_distance: float
    ) -> Dict[str, Any]:
        """Structures summary of running activity for TRMNL."""
        weekly_goal, yearly_goal = self._load_goals()
        variables = {
            "merge_variables": {
                "weekly_distance": round(week_distance, 1),
                "weekly_goal": weekly_goal,
                "yearly_distance": round(ytd_distance, 1),
                "yearly_goal": yearly_goal,
            }
        }

        return variables


def send_strava_goals_to_trmnl() -> None:
    try:
        config = Config.from_environment()
        strava = Strava(config)
        variables = strava.get_summary()
        url = (
            f"https://usetrmnl.com/api/custom_plugins/{os.environ["TRMNL_PLUGIN_UUID"]}"
        )
        response = requests.post(url, json=variables)
        print(response.json())
    except StravaError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    send_strava_goals_to_trmnl()
