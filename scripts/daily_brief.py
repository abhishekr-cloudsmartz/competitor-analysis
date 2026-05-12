#!/usr/bin/env python3
"""
Daily competitive brief generator for CloudSmartz.
Reads config.json, fetches RSS feeds, calls Claude API, appends to data.json.
Designed to run in GitHub Actions — no local dependencies required.
"""

import json
import os
import re
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta

import anthropic
import requests

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(REPO_ROOT, "config.json")
DATA_PATH = os.path.join(REPO_ROOT, "data.json")

ATOM_NS = {"atom": "http://www.w3.org/2005/Atom"}
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; CloudSmartz-CI/1.0)"}


def fetch_rss(url: str) -> list[dict] | str:
    """Fetch a Google Alerts Atom feed and return entries from the last 48h."""
    try:
        r = requests.get(url, headers=HEADERS, timeout=20)
        r.raise_for_status()
        root = ET.fromstring(r.content)
        cutoff = datetime.now(timezone.utc) - timedelta(hours=48)
        entries = []
        for entry in root.findall("atom:entry", ATOM_NS):
            raw_date = (
                entry.findtext("atom:published", "", ATOM_NS)
                or entry.findtext("atom:updated", "", ATOM_NS)
            )
            try:
                published = datetime.fromisoformat(raw_date.replace("Z", "+00:00"))
            except Exception:
                continue
            if published < cutoff:
                continue
            title = entry.findtext("atom:title", "", ATOM_NS)
            link_el = entry.find("atom:link", ATOM_NS)
            link = link_el.get("href", "") if link_el is not None else ""
            entries.append({"title": title, "link": link, "published": raw_date})
        return entries
    except Exception as exc:
        return f"error: {exc}"


def build_competitor_context(config: dict, rss_data: dict) -> str:
    lines = []
    for comp in config["competitors"]:
        cid = comp["id"]
        name = comp["name"]
        notes = comp.get("notes", "")
        entries = rss_data.get(cid)

        if entries is None:
            status = "no RSS configured — use training knowledge conservatively"
        elif isinstance(entries, str):
            status = f"RSS fetch failed ({entries}) — use training knowledge conservatively"
        elif len(entries) == 0:
            status = "RSS returned 0 entries in last 48h"
        else:
            status = f"RSS: {len(entries)} recent entries"

        lines.append(f"\n### {name} (id: {cid}) [{status}]")
        lines.append(f"Notes: {notes}")
        if isinstance(entries, list) and entries:
            for e in entries[:6]:
                lines.append(f"  - {e['published'][:10]}: {e['title']}")
    return "\n".join(lines)


def build_prompt(config: dict, history: list, rss_data: dict,
                 today: str, today_label: str, timestamp: str) -> str:
    competitor_ids = [c["id"] for c in config["competitors"]]
    comp_context = build_competitor_context(config, rss_data)
    recent_json = json.dumps(history[-3:], indent=2)

    comp_schema_lines = ",\n".join(
        f'    "{cid}": {{"activity": "string ≤120 chars", "channel": "press|blog|linkedin|event|youtube|null", "relevance": "string or null"}}'
        for cid in competitor_ids
    )

    return f"""You are generating a daily competitive intelligence brief for CloudSmartz (cloudsmartz.net), a telecom BSS/OSS/CPQ/NaaS solutions company.

Today: {today} — {today_label} — {timestamp} UTC

COMPETITOR SIGNALS (RSS feeds, last 48h only):
{comp_context}

RECENT HISTORY (last 3 entries — for deduplication and quote rotation):
{recent_json}

Generate a single JSON object. Rules:
- Only report what is actually in the RSS feed data above. Do not invent activity.
- For competitors with no RSS or 0 entries: set activity = "Nothing detected in last 48h.", channel = null, relevance = null
- activity must be ≤120 characters
- channel must be exactly one of: press, blog, linkedin, event, youtube, or null
- quote must be sharp, original, and not repeat any quote in recent history
- topSignal, channelPulse, actionItem: 1-2 sentences each, or null if nothing notable
- Return ONLY the JSON object — no markdown fences, no explanation

Required schema:
{{
  "date": "{today}",
  "label": "{today_label}",
  "timestamp": "{timestamp}",
  "quote": "...",
  "topSignal": "..." or null,
  "channelPulse": "..." or null,
  "actionItem": "..." or null,
  "competitors": {{
{comp_schema_lines}
  }}
}}"""


def call_claude(prompt: str) -> dict:
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=3000,
        messages=[{"role": "user", "content": prompt}],
    )
    text = message.content[0].text.strip()
    # Strip any accidental markdown fences
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    return json.loads(text)


def main():
    with open(CONFIG_PATH) as f:
        config = json.load(f)

    with open(DATA_PATH) as f:
        history = json.load(f)

    now = datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%d")
    today_label = now.strftime("%B ") + str(now.day) + now.strftime(", %Y")
    timestamp = now.strftime("%H:%M")

    if any(e["date"] == today for e in history):
        print(f"Brief for {today} already exists — skipping.")
        sys.exit(0)

    print(f"Fetching RSS feeds for {len(config['competitors'])} competitors...")
    rss_data = {}
    for comp in config["competitors"]:
        url = comp.get("googleAlertRssUrl")
        if url and not url.startswith("PASTE_"):
            entries = fetch_rss(url)
            count = len(entries) if isinstance(entries, list) else entries
            print(f"  {comp['name']}: {count}")
            rss_data[comp["id"]] = entries
        else:
            print(f"  {comp['name']}: no RSS")
            rss_data[comp["id"]] = None

    print("Calling Claude API...")
    prompt = build_prompt(config, history, rss_data, today, today_label, timestamp)
    new_entry = call_claude(prompt)

    history.append(new_entry)
    with open(DATA_PATH, "w") as f:
        json.dump(history, f, indent=2)

    print(f"\n✅ Brief written for {today}")
    print(f"Top signal: {new_entry.get('topSignal') or 'None'}")
    active = [
        cid for cid, d in new_entry.get("competitors", {}).items()
        if "Nothing detected" not in (d.get("activity") or "Nothing detected")
    ]
    print(f"Active signals: {', '.join(active) if active else 'None'}")


if __name__ == "__main__":
    main()
