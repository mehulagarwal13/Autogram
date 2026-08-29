import httpx
from app.core.config import ADZUNA_APP_ID, ADZUNA_APP_KEY

BASE_URL = "https://api.adzuna.com/v1/api/jobs"


def fetch_jobs(query: str, country: str = "in", location: str | None = None, results: int = 20) -> list[dict]:
    """
    Fetches raw job listings from Adzuna for a given search query.
    country: Adzuna uses country codes like 'gb', 'us', 'in', etc.
    """
    url = f"{BASE_URL}/{country}/search/1"
    params = {
        "app_id": ADZUNA_APP_ID,
        "app_key": ADZUNA_APP_KEY,
        "what": query,
        "results_per_page": results,
    }
    if location:
        params["where"] = location

    response = httpx.get(url, params=params, timeout=15)
    response.raise_for_status()
    data = response.json()

    return data.get("results", [])