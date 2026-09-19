"""Track how much of each API key you have used.

Why this is counted locally rather than asked for:

Google's Gemini API returns no rate-limit headers and offers no usage endpoint
for an API key, so there is no way to ask it how much of a free tier is left.
Groq and OpenAI do send x-ratelimit-* headers, and OpenRouter has a key endpoint,
and those are recorded when present - but the number that is always available is
the one we count ourselves.

Every call this app makes is appended to usage.jsonl, so the totals are exact
for this app. They do not include usage from anything else sharing the key.
"""

import json
import os
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional

from email_workflow.core.paths import resolve_project_file
from email_workflow.core.locking import synchronized

# Header names providers use for what is left. Different vendors, same idea.
RATE_LIMIT_HEADERS = (
    "x-ratelimit-remaining-requests",
    "x-ratelimit-remaining-tokens",
    "x-ratelimit-limit-requests",
    "x-ratelimit-limit-tokens",
    "x-ratelimit-reset-requests",
    "x-ratelimit-reset-tokens",
)


class UsageTracker:
    def __init__(self, store_path: str = "usage.jsonl"):
        self.store_path: Path = resolve_project_file(store_path)

    # --- writing ----------------------------------------------------------

    @synchronized
    def record(
        self,
        provider: str,
        model: str,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        status: str = "ok",
        kind: str = "",
        # Which API key this call was billed to. Keyword-only in effect: it is
        # added after the positional arguments callers already pass.
        key_env: str = "",
        headers: Optional[dict] = None,
        when: Optional[datetime] = None,
    ) -> None:
        """Append one call. Never raises: bookkeeping must not break a run."""
        entry = {
            "ts": (when or datetime.now()).isoformat(timespec="seconds"),
            "provider": provider,
            "model": model,
            "prompt_tokens": int(prompt_tokens or 0),
            "completion_tokens": int(completion_tokens or 0),
            "total_tokens": int(prompt_tokens or 0) + int(completion_tokens or 0),
            "status": status,
        }
        if key_env:
            entry["key_env"] = key_env
        if kind:
            entry["kind"] = kind

        if headers:
            lowered = {str(k).lower(): v for k, v in headers.items()}
            reported = {h: lowered[h] for h in RATE_LIMIT_HEADERS if h in lowered}
            if reported:
                entry["reported"] = reported

        try:
            self.store_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.store_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry) + "\n")
        except OSError:
            pass

    # --- reading ----------------------------------------------------------

    def entries(self, since: Optional[datetime] = None) -> List[dict]:
        if not self.store_path.exists():
            return []

        found = []
        try:
            with open(self.store_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                        stamp = datetime.fromisoformat(entry["ts"])
                    except (json.JSONDecodeError, KeyError, ValueError):
                        continue  # a torn line must not hide the rest
                    if since is None or stamp >= since:
                        found.append(entry)
        except OSError:
            return []
        return found

    def summarise(self, since: Optional[datetime] = None) -> Dict[str, dict]:
        """Totals per 'provider / model'."""
        totals: Dict[str, dict] = defaultdict(
            lambda: {
                "calls": 0,
                "failed": 0,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
                "quota_hits": 0,
                "reported": {},
                "last_used": "",
            }
        )

        for entry in self.entries(since):
            key = f"{entry.get('provider', '?')} / {entry.get('model', '?')}"
            # Keys are separate accounts with separate allowances, so they are
            # counted separately. Records written before keys were tracked have
            # no key_env and simply stay under the plain provider/model row.
            if entry.get("key_env"):
                key += f" [{entry['key_env']}]"
            row = totals[key]
            row["calls"] += 1
            if entry.get("status") != "ok":
                row["failed"] += 1
            if entry.get("kind") == "quota":
                row["quota_hits"] += 1
            row["prompt_tokens"] += entry.get("prompt_tokens", 0)
            row["completion_tokens"] += entry.get("completion_tokens", 0)
            row["total_tokens"] += entry.get("total_tokens", 0)
            if entry.get("reported"):
                row["reported"] = entry["reported"]  # keep the most recent
            row["last_used"] = max(row["last_used"], entry.get("ts", ""))

        return dict(totals)

    def totals_today(self) -> Dict[str, dict]:
        midnight = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        return self.summarise(since=midnight)

    def daily_counts(self, days: int = 7) -> List[tuple]:
        """[(date, calls, total_tokens)] oldest first, including empty days."""
        start = (datetime.now() - timedelta(days=days - 1)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        per_day: Dict[str, List[int]] = {}
        for offset in range(days):
            day = (start + timedelta(days=offset)).strftime("%Y-%m-%d")
            per_day[day] = [0, 0]

        for entry in self.entries(since=start):
            day = entry["ts"][:10]
            if day in per_day:
                per_day[day][0] += 1
                per_day[day][1] += entry.get("total_tokens", 0)

        return [(day, counts[0], counts[1]) for day, counts in sorted(per_day.items())]

    @synchronized
    def reset(self) -> None:
        if self.store_path.exists():
            self.store_path.unlink()
