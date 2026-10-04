"""Daily run: scrape, compare with the previous run, save history and build the page.

Usage: python3 tracker/run.py [--offline data/latest.json]
"""

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from scrape import ISRAEL_TZ, scrape  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
DAYS = DATA / "days"
CONFIG = json.loads((ROOT / "tracker" / "config.json").read_text(encoding="utf-8"))
MAX_CHANGELOG = 300


def lesson_key(l):
    return f"{l['date']}|{l['hour']}|{l['subject']}"


def describe(l):
    return f"{l['date']} שעה {l['hour']} · {l['subject']} · {', '.join(l['classes'])}"


def names(items):
    return [i["name"] if isinstance(i, dict) else i for i in items]


def diff(old, new, today):
    """List what changed in upcoming lessons between two runs."""
    old_by = {lesson_key(l): l for l in old.get("lessons", []) if l["date"] >= today}
    new_by = {lesson_key(l): l for l in new["lessons"] if l["date"] >= today}
    # Only compare dates both runs could see, so the week rolling forward isn't reported as "new lessons".
    old_dates = {d["date"] for d in old.get("days", [])}
    new_dates = {d["date"] for d in new["days"]}
    shared = old_dates & new_dates
    found = []
    for k, l in new_by.items():
        if l["date"] not in shared:
            continue
        if k not in old_by:
            found.append({"kind": "נוסף", "date": l["date"], "text": describe(l)})
            continue
        before = old_by[k]
        for field, label in (("classes", "כיתות"), ("rooms", "חדר"), ("co_teachers", "מורים שותפים")):
            was, now = names(before.get(field, [])), names(l[field])
            if was != now:
                found.append({"kind": "עודכן", "date": l["date"],
                              "text": f"{describe(l)} — {label}: {', '.join(was) or '—'} ← {', '.join(now) or '—'}"})
        old_changes = {(c["type"], c["text"]) for c in before["changes"]}
        for c in l["changes"]:
            if (c["type"], c["text"]) not in old_changes:
                found.append({"kind": c["type"], "date": l["date"], "text": f"{describe(l)} — {c['text']}"})
    for k, l in old_by.items():
        if l["date"] in shared and k not in new_by:
            found.append({"kind": "הוסר", "date": l["date"], "text": describe(l)})
    old_tab = {(c["class"], c["date_label"], c["text"]) for c in old.get("class_changes", [])}
    for c in new["class_changes"]:
        if (c["class"], c["date_label"], c["text"]) not in old_tab:
            found.append({"kind": "שינוי בדף השינויים", "date": "", "text": f"{c['class']}: {c['date_label']} {c['text']}".strip()})
    return found


def load_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


def build_page(latest, changelog, history):
    template = (ROOT / "tracker" / "template.html").read_text(encoding="utf-8")
    payload = {
        "teacher": CONFIG["display_name"],
        "source": CONFIG["site"],
        "latest": latest,
        "changelog": changelog,
        "history": history,
    }
    blob = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")
    out = ROOT / "site" / "index.html"
    out.parent.mkdir(exist_ok=True)
    out.write_text(template.replace("/*__DATA__*/null", blob), encoding="utf-8")
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--offline", help="skip scraping and rebuild the page from this snapshot")
    args = ap.parse_args()

    now = datetime.now(ISRAEL_TZ)
    today = now.date().isoformat()
    previous = load_json(DATA / "latest.json", {})
    changelog = load_json(DATA / "changelog.json", [])

    if args.offline:
        latest = load_json(Path(args.offline), None)
        found = []
    else:
        latest = scrape(CONFIG["name_key"])
        if latest["class_count"] == 0 or not latest["days"]:
            sys.exit("scrape returned no classes or days; keeping the previous data")
        found = diff(previous, latest, today) if previous else []
        for f in found:
            f["detected_at"] = latest["scraped_at"]
        changelog = (found + changelog)[:MAX_CHANGELOG]
        write_json(DATA / "latest.json", latest)
        write_json(DATA / "changelog.json", changelog)
        # One file per school day: the most recent view of that day wins, so past days stay on record.
        for d in latest["days"]:
            write_json(DAYS / f"{d['date']}.json", {
                **d,
                "recorded_at": latest["scraped_at"],
                "lessons": [l for l in latest["lessons"] if l["date"] == d["date"]],
            })

    history = [load_json(p, {}) for p in sorted(DAYS.glob("*.json"))]
    out = build_page(latest, changelog, history)

    print(f"lessons found: {len(latest['lessons'])} across {len(latest['days'])} days")
    print(f"changes since last run: {len(found)}")
    for f in found:
        print(f"  [{f['kind']}] {f['text']}")
    print(f"page: {out.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
