#!/usr/bin/env python3
"""
Daily competitive brief generator for CloudSmartz.

Provider selection (first key found wins):
  GEMINI_API_KEY   → Gemini 2.5 Flash (primary), Flash-Lite (fallback after retry)
  ANTHROPIC_API_KEY → Claude claude-sonnet-4-6

Dedup logic (multiple runs per day supported):
  - Fetches RSS on every run
  - Compares fetched links against rssSnapshot stored in the last entry for today
  - If new links exist → regenerates brief and replaces today's entry
  - If no new links → skips (prints summary of current entry)
  This allows the Gemini cron to run at 10 AM and a manual Claude run later in the day
  to pick up afternoon signals without doubling entries.

Retry logic (Gemini only):
  Attempt 1: gemini-2.5-flash
  Attempt 2: gemini-2.5-flash  (after 30s delay)
  Attempt 3: gemini-2.5-flash-lite (immediate fallback)
"""

import json
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta

import requests

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(REPO_ROOT, "config.json")
DATA_PATH   = os.path.join(REPO_ROOT, "data.json")

ATOM_NS  = {"atom": "http://www.w3.org/2005/Atom"}
HEADERS  = {"User-Agent": "Mozilla/5.0 (compatible; CloudSmartz-CI/1.0)"}

GEMINI_PRIMARY  = "gemini-2.5-flash"
GEMINI_FALLBACK = "gemini-2.5-flash-lite"


# ── RSS ──────────────────────────────────────────────────────────────────────

def fetch_rss(url: str) -> list[dict] | str:
    """Return Atom entries published in the last 48h, or an error string."""
    try:
        r = requests.get(url, headers=HEADERS, timeout=20)
        r.raise_for_status()
        root    = ET.fromstring(r.content)
        cutoff  = datetime.now(timezone.utc) - timedelta(hours=48)
        entries = []
        for entry in root.findall("atom:entry", ATOM_NS):
            raw = (entry.findtext("atom:published", "", ATOM_NS)
                   or entry.findtext("atom:updated",   "", ATOM_NS))
            try:
                published = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            except Exception:
                continue
            if published < cutoff:
                continue
            link_el = entry.find("atom:link", ATOM_NS)
            entries.append({
                "title":     entry.findtext("atom:title", "", ATOM_NS),
                "link":      link_el.get("href", "") if link_el is not None else "",
                "published": raw,
            })
        return entries
    except Exception as exc:
        return f"error: {exc}"


# ── Snapshot helpers ──────────────────────────────────────────────────────────

def build_rss_snapshot(rss_data: dict) -> dict:
    """Extract set of item links per competitor for dedup comparison."""
    return {
        cid: [e["link"] for e in entries] if isinstance(entries, list) else []
        for cid, entries in rss_data.items()
    }


def has_new_rss_content(current_snapshot: dict, today_entry: dict) -> bool:
    """Return True if any competitor has links not seen in the previous run."""
    prev = today_entry.get("rssSnapshot", {})
    for cid, links in current_snapshot.items():
        prev_set = set(prev.get(cid, []))
        if any(link not in prev_set for link in links):
            return True
    return False


def extract_linkedin_signals(rss_data: dict) -> dict:
    """Count RSS items sourced from linkedin.com per competitor.

    Google Alerts occasionally picks up LinkedIn posts. Over time these
    counts form a posting-frequency trend per competitor.
    """
    return {
        cid: sum(1 for e in entries if "linkedin.com" in e.get("link", ""))
        if isinstance(entries, list) else 0
        for cid, entries in rss_data.items()
    }


# ── Prompt builder ────────────────────────────────────────────────────────────

def build_prompt(config: dict, history: list, rss_data: dict,
                 today: str, today_label: str, timestamp: str,
                 yt_data: dict | None = None,
                 blog_data: dict | None = None) -> str:
    yt_data   = yt_data   or {}
    blog_data = blog_data or {}
    lines = []
    for comp in config["competitors"]:
        cid     = comp["id"]
        entries = rss_data.get(cid)
        if entries is None:
            status = "no RSS — use training knowledge conservatively"
        elif isinstance(entries, str):
            status = f"RSS failed ({entries}) — use training knowledge conservatively"
        elif not entries:
            status = "RSS: 0 entries in last 48h"
        else:
            status = f"RSS: {len(entries)} entries"
        lines.append(f"\n### {comp['name']} (id: {cid}) [{status}]")
        lines.append(f"Notes: {comp.get('notes', '')}")
        if isinstance(entries, list):
            for e in entries[:6]:
                lines.append(f"  - {e['published'][:10]}: {e['title']}")
        yt = yt_data.get(cid)
        if isinstance(yt, list) and yt:
            lines.append(f"  YouTube ({len(yt)} new videos):")
            for e in yt[:3]:
                lines.append(f"    - {e['published'][:10]}: {e['title']}")
        blog = blog_data.get(cid)
        if isinstance(blog, list) and blog:
            lines.append(f"  Blog ({len(blog)} new posts):")
            for e in blog[:3]:
                lines.append(f"    - {e['published'][:10]}: {e['title']}")

    comp_schema = ",\n".join(
        f'    "{c["id"]}": {{"activity": "≤120 chars", '
        f'"pubDate": "YYYY-MM-DD|null", '
        f'"channel": "press|blog|linkedin|event|youtube|null", "relevance": "string|null"}}'
        for c in config["competitors"]
    )

    return f"""You are generating a daily competitive intelligence brief for CloudSmartz (cloudsmartz.net), a telecom BSS/OSS/CPQ/NaaS solutions company.

Today: {today} — {today_label} — {timestamp} UTC

COMPETITOR SIGNALS (RSS feeds, last 48h only):
{"".join(lines)}

RECENT HISTORY (last 3 entries — for deduplication and quote rotation):
{json.dumps(history[-3:], indent=2)}

Generate a single JSON object. Rules:
- Only report activity that appears in the RSS data above. Do not invent.
- Competitors with no RSS or 0 entries: activity = "Nothing detected in last 48h.", channel = null, pubDate = null, relevance = null
- activity ≤ 120 characters
- pubDate: the published date (YYYY-MM-DD) of the RSS article being summarised; null if nothing detected
- channel must be exactly: press, blog, linkedin, event, youtube, or null — match the source section (YouTube section → youtube, Blog section → blog, Google Alert → press)
- quote: sharp, original, not repeated from recent history
- topSignal / channelPulse / actionItem: 1–2 sentences or null
- Return ONLY the raw JSON — no markdown fences, no explanation

Schema:
{{
  "date": "{today}",
  "label": "{today_label}",
  "timestamp": "{timestamp}",
  "quote": "...",
  "topSignal": "..." | null,
  "channelPulse": "..." | null,
  "actionItem": "..." | null,
  "competitors": {{
{comp_schema}
  }}
}}"""


# ── LLM providers ─────────────────────────────────────────────────────────────

def _parse_json(text: str) -> dict:
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    return json.loads(text)


def call_gemini(prompt: str) -> dict:
    import google.generativeai as genai  # pip install google-generativeai
    genai.configure(api_key=os.environ["GEMINI_API_KEY"])

    attempts = [
        (GEMINI_PRIMARY,  0),   # first try
        (GEMINI_PRIMARY,  30),  # retry same model after 30s
        (GEMINI_FALLBACK, 0),   # fall back to lighter model
    ]

    last_err = None
    for model_id, delay in attempts:
        if delay:
            print(f"  [{model_id}] transient error — waiting {delay}s before retry...")
            time.sleep(delay)
        try:
            print(f"  Calling {model_id}...")
            model    = genai.GenerativeModel(model_id)
            response = model.generate_content(prompt)
            return _parse_json(response.text)
        except Exception as exc:
            last_err = exc
            err_str = str(exc).lower()
            transient = any(code in err_str for code in ("503", "500", "502", "504",
                                                          "unavailable", "internal"))
            print(f"  {model_id} failed ({'transient' if transient else 'non-transient'}): {exc}")
            if not transient:
                raise

    raise RuntimeError(f"All Gemini attempts exhausted. Last error: {last_err}")


def call_claude(prompt: str) -> dict:
    import anthropic  # pip install anthropic
    client  = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=3000,
        messages=[{"role": "user", "content": prompt}],
    )
    return _parse_json(message.content[0].text)


def call_llm(prompt: str) -> dict:
    if os.environ.get("GEMINI_API_KEY"):
        print("Provider: Gemini")
        return call_gemini(prompt)
    if os.environ.get("ANTHROPIC_API_KEY"):
        print("Provider: Anthropic (Claude)")
        return call_claude(prompt)
    raise EnvironmentError(
        "No LLM API key found. Set GEMINI_API_KEY or ANTHROPIC_API_KEY."
    )


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    with open(CONFIG_PATH) as f:
        config = json.load(f)
    with open(DATA_PATH) as f:
        history = json.load(f)

    now         = datetime.now(timezone.utc)
    today       = now.strftime("%Y-%m-%d")
    today_label = now.strftime("%B ") + str(now.day) + now.strftime(", %Y")
    timestamp   = now.strftime("%H:%M")

    print(f"Fetching RSS feeds ({len(config['competitors'])} competitors)...")
    rss_data = {}       # googleAlertRssUrl entries (press/news signals)
    yt_data  = {}       # youtubeRssUrl entries
    blog_data = {}      # blogRssUrl entries

    for comp in config["competitors"]:
        cid = comp["id"]

        alert_url = comp.get("googleAlertRssUrl")
        if alert_url and not alert_url.startswith("PASTE_"):
            result = fetch_rss(alert_url)
            count  = len(result) if isinstance(result, list) else result
            print(f"  {comp['name']} [alert]: {count}")
            rss_data[cid] = result
        else:
            rss_data[cid] = None

        yt_url = comp.get("youtubeRssUrl")
        if yt_url:
            result = fetch_rss(yt_url)
            count  = len(result) if isinstance(result, list) else result
            print(f"  {comp['name']} [youtube]: {count}")
            yt_data[cid] = result

        blog_url = comp.get("blogRssUrl")
        if blog_url:
            result = fetch_rss(blog_url)
            count  = len(result) if isinstance(result, list) else result
            print(f"  {comp['name']} [blog]: {count}")
            blog_data[cid] = result

    rss_snapshot     = build_rss_snapshot({**rss_data, **yt_data, **blog_data})
    linkedin_signals = extract_linkedin_signals(rss_data)

    # Dedup: find existing entry for today (if any)
    today_entry = next((e for e in history if e["date"] == today), None)

    if today_entry:
        if not has_new_rss_content(rss_snapshot, today_entry):
            print(f"No new RSS content since last run at {today_entry['timestamp']} — skipping.")
            print(f"Top signal: {today_entry.get('topSignal') or 'None'}")
            sys.exit(0)
        print(f"New RSS content detected since {today_entry['timestamp']} — regenerating brief...")
        history = [e for e in history if e["date"] != today]

    print("Generating brief...")
    prompt    = build_prompt(config, history, rss_data, today, today_label, timestamp,
                             yt_data=yt_data, blog_data=blog_data)
    new_entry = call_llm(prompt)

    # Attach metadata not generated by the LLM
    new_entry["rssSnapshot"]     = rss_snapshot
    new_entry["linkedinSignals"] = linkedin_signals

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
    li_active = [cid for cid, n in linkedin_signals.items() if n > 0]
    if li_active:
        print(f"LinkedIn signals: {', '.join(li_active)}")


if __name__ == "__main__":
    main()
