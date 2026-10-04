"""Daily run: scrape, compare with the previous run, save history and build the page.

Usage: python3 tracker/run.py [--offline]
"""

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from scrape import ISRAEL_TZ, collect, scrape  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
CONFIG = json.loads((ROOT / "tracker" / "config.json").read_text(encoding="utf-8"))
MAX_CHANGELOG = 300


def lesson_key(l):
    return f"{l['date']}|{l['hour']}|{l['subject']}"


def describe(l):
    return f"{l['date']} שעה {l['hour']} · {l['subject']} · {', '.join(l['classes'])}"


def reasons(l):
    texts = sorted({c["text"] for c in l["changes"]})
    return f" — {', '.join(texts)}" if texts else ""


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
        status = l.get("status", "regular")
        if k not in old_by:
            found.append({"kind": "לא מתקיים" if status == "cancelled" else "נוסף", "date": l["date"],
                          "text": describe(l) + reasons(l)})
            continue
        before = old_by[k]
        if before.get("status", "regular") != status:
            found.append({"kind": "לא מתקיים" if status == "cancelled" else "חזר למערכת", "date": l["date"],
                          "text": describe(l) + reasons(l)})
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
    if "class_end" in old:
        for day, rows in new.get("class_end", {}).items():
            if day < today or day not in shared:
                continue
            before = {r["class"]: r["now"] for r in old["class_end"].get(day, [])}
            for r in rows:
                was, now = before.get(r["class"]), r["now"]
                if r["class"] in before and was != now:
                    found.append({"kind": "שעת סיום", "date": day,
                                  "text": f"{day} · {r['class']} מסיים {now['end'] if now else 'ללא לימודים'} (במקום {was['end'] if was else 'ללא לימודים'})"})
    if "agenda" in old:  # the first run with agenda support would otherwise report every item as new
        old_agenda = {(a["kind"], a["date"], a["title"]) for a in old["agenda"]}
        for a in new.get("agenda", []):
            if (a["kind"], a["date"], a["title"]) not in old_agenda:
                found.append({"kind": f"{a['kind']} חדש", "date": a["date"],
                              "text": f"{a['date']} · {a['title']} · {', '.join(a['my_classes'])}"})
    return found


def load_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


def build_page(person, latest, changelog, history):
    template = (ROOT / "tracker" / "template.html").read_text(encoding="utf-8")
    payload = {
        "teacher": person["display_name"],
        "source": CONFIG["site"],
        "end_grades": person.get("end_time_grades", []),
        "latest": latest,
        "changelog": changelog,
        "history": history,
    }
    blob = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")
    out = ROOT / person["page"]
    out.parent.mkdir(parents=True, exist_ok=True)
    page = template.replace("/*__DATA__*/null", blob).replace("__TITLE__", person["title"])
    out.write_text(page, encoding="utf-8")
    return out


def run_person(person, raw, today, offline):
    data = ROOT / person["data_dir"]
    previous = load_json(data / "latest.json", {})
    changelog = load_json(data / "changelog.json", [])
    history = load_json(data / "history.json", {})

    if offline:
        latest, found = previous, []
    else:
        latest = scrape(person["name_key"], raw=raw, end_grades=person.get("end_time_grades", []))
        found = diff(previous, latest, today) if previous else []
        for f in found:
            f["detected_at"] = latest["scraped_at"]
        changelog = (found + changelog)[:MAX_CHANGELOG]
        write_json(data / "latest.json", latest)
        write_json(data / "changelog.json", changelog)
        # One entry per school day: the most recent view of that day wins, so past days stay on record.
        for d in latest["days"]:
            history[d["date"]] = {
                **d,
                "recorded_at": latest["scraped_at"],
                "lessons": [l for l in latest["lessons"] if l["date"] == d["date"]],
            }
        write_json(data / "history.json", history)
    out = build_page(person, latest, changelog, [history[k] for k in sorted(history)])

    print(f"=== {person['display_name']} ===")
    print(f"lessons found: {len(latest['lessons'])} across {len(latest['days'])} days")
    print(f"changes since last run: {len(found)}")
    for f in found:
        print(f"  [{f['kind']}] {f['text']}")
    print(f"page: {out.relative_to(ROOT)}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--offline", action="store_true", help="skip scraping and rebuild the pages from saved data")
    args = ap.parse_args()

    now = datetime.now(ISRAEL_TZ)
    raw = None
    if not args.offline:
        raw = collect(now=now)
        if not raw["classes"] or not raw["days"]:
            sys.exit("scrape returned no classes or days; keeping the previous data")
    for person in CONFIG["people"]:
        run_person(person, raw, now.date().isoformat(), args.offline)


if __name__ == "__main__":
    main()
