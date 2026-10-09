"""Daily run: scrape, compare with the previous run, save history and build the page.

Usage: python3 tracker/run.py [--offline]
"""

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from scrape import ISRAEL_TZ, collect, scrape  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
CONFIG = json.loads((ROOT / "tracker" / "config.json").read_text(encoding="utf-8"))
MAX_CHANGELOG = 300
# Kept out of git (the repo is public): anyone who knows an ntfy topic can read it.
SECRETS = json.loads((ROOT / "tracker" / "secrets.json").read_text(encoding="utf-8")) \
    if (ROOT / "tracker" / "secrets.json").exists() else {}


def notify_ntfy(topic, title, message, click):
    """Send a phone notification through ntfy.sh. The free tier is rate limited per IP, so retry."""
    body = json.dumps({"topic": topic, "title": title, "message": message, "click": click, "tags": ["school"]})
    for attempt in range(6):
        try:
            req = urllib.request.Request("https://ntfy.sh/", data=body.encode("utf-8"), method="POST",
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=20):
                return True
        except (urllib.error.URLError, TimeoutError) as e:
            print(f"ntfy attempt {attempt + 1} failed: {e}")
            time.sleep(15 * (attempt + 1))
    return False


def lesson_key(l):
    return f"{l['date']}|{l['hour']}|{l['subject']}"


def describe(l):
    return f"{l['date']} שעה {l['hour']} · {l['subject']} · {', '.join(l['classes'])}"


def reasons(l):
    texts = sorted({c["text"] for c in l["changes"]})
    return f" — {', '.join(texts)}" if texts else ""


def fill(text):
    """The site writes a fill-in as "original <- substitute"; say it the way people do."""
    left, sep, right = text.partition("<-")
    return f"{right.strip()} במקום {left.strip()}" if sep else text


def hours_label(hours):
    """[1, 2, 3, 5] -> "1–3, 5"."""
    hours, parts, i = sorted(set(hours)), [], 0
    while i < len(hours):
        j = i
        while j + 1 < len(hours) and hours[j + 1] == hours[j] + 1:
            j += 1
        parts.append(str(hours[i]) if i == j else f"{hours[i]}–{hours[j]}")
        i = j + 1
    return ", ".join(parts)


def dm(iso):
    return f"{iso[8:10]}.{iso[5:7]}"


def homeroom_line(hr, day):
    info = (hr or {}).get("days", {}).get(day)
    if not info:
        return []
    now, reg = info.get("last_lesson", info.get("end")), info.get("regular_end")
    end = f"מסיימת {now['end']}" if now else "אין שיעורים היום"
    if reg and (not now or now["hour"] != reg["hour"]):
        end += f" (בדרך כלל {reg['end']})"
    lines = [f"🏫 {hr['class']}: {end}"]
    reasons = sorted({fill(c["text"]) for c in info["changes"] if c["type"] != "מילוי מקום"})
    if info["removed"]:
        lines.append(f"   לא מתקיים: שעות {hours_label([r['hour'] for r in info['removed']])}"
                     + (f" — {', '.join(reasons)}" if reasons else ""))
    elif reasons:
        lines.append(f"   {', '.join(reasons)}")
    fills = sorted({fill(c["text"]) for c in info["changes"] if c["type"] == "מילוי מקום"})
    if fills:
        lines.append(f"   מילוי מקום: {', '.join(fills)}")
    return lines


def morning_summary(latest, today):
    """Short text of today's lessons, or None when there is nothing on today."""
    day = next((d for d in latest["days"] if d["date"] == today), None)
    todays = [l for l in latest["lessons"] if l["date"] == today]
    hr_lines = homeroom_line(latest.get("homeroom"), today)
    if not day or (not todays and not hr_lines):
        return None
    blocks = []
    for l in todays:
        b = blocks[-1] if blocks else None
        if b and b["subject"] == l["subject"] and b["classes"] == l["classes"] and b["status"] == l.get("status") \
                and b["last"] + 1 == l["hour"]:
            b["last"], b["end"] = l["hour"], l["end"]
            b["changes"] += [c for c in l["changes"] if c not in b["changes"]]
            continue
        blocks.append({"subject": l["subject"], "classes": l["classes"], "status": l.get("status"), "last": l["hour"],
                       "start": l["start"], "end": l["end"], "changes": list(l["changes"])})
    active = [b for b in blocks if b["status"] != "cancelled"]
    count = sum(1 for l in todays if l.get("status") != "cancelled")
    head = f"📅 {day['weekday']} {dm(today)}"
    if active:
        head += f" · {'שעה אחת' if count == 1 else f'{count} שעות'} · סיום {active[-1]['end']}"
    else:
        head += " · אין לך שיעורים"
    lines = [head] + [f"⚠️ {h}" for h in day.get("holidays", [])]
    for b in blocks:
        where = (", ".join(b["classes"]) + " · ") if b["classes"] else ""
        notes = sorted({fill(c["text"]) for c in b["changes"]})
        if b["status"] == "cancelled":
            lines.append(f"❌ {b['start']} {where}{b['subject']} — לא מתקיים" + (f" ({', '.join(notes)})" if notes else ""))
        else:
            lines.append(f"{b['start']}–{b['end']} {where}{b['subject']}" + (f" ({', '.join(notes)})" if notes else ""))
    # Exams in the teacher's classes, and events that fall on one of the teacher's lessons.
    for a in [a for a in latest.get("agenda", []) if a["date"] == today and (a["kind"] == "מבחן" or a["my_lessons"])][:3]:
        lines.append(f"📝 {a['kind']}: {a['title']} ({', '.join(a['my_classes'])})")
    return "\n".join(lines + hr_lines)


def todays_changes(found, today):
    """Only changes that land on today are worth a notification; the rest wait on the page."""
    out = []
    for f in found:
        if f["kind"] == "שעת סיום":  # grade-wide end times; the homeroom class is reported on its own
            continue
        if f["date"] == today or (not f["date"] and dm(today) + "." + today[:4] in f["text"]):
            out.append(f)
    return out


def names(items):
    return [i["name"] if isinstance(i, dict) else i for i in items]


def diff(old, new, today):
    """List what changed in upcoming lessons between two runs."""
    # Lessons added by hand in the config aren't on the site, so they are never reported as changes.
    old_by = {lesson_key(l): l for l in old.get("lessons", []) if l["date"] >= today and not l.get("manual")}
    new_by = {lesson_key(l): l for l in new["lessons"] if l["date"] >= today and not l.get("manual")}
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
    if old.get("homeroom") and new.get("homeroom") and old["homeroom"]["class"] == new["homeroom"]["class"]:
        cls = new["homeroom"]["class"]
        for day, info in new["homeroom"]["days"].items():
            before = old["homeroom"]["days"].get(day)
            if day < today or day not in shared or before is None:
                continue
            was, now = before.get("last_lesson", before.get("end")), info.get("last_lesson")
            if (was or {}).get("hour") != (now or {}).get("hour"):
                found.append({"kind": "כיתת חינוך", "date": day,
                              "text": f"{cls} מסיימת {now['end'] if now else 'ללא לימודים'} (קודם {was['end'] if was else 'ללא לימודים'})"})
            old_removed = {(r["hour"], r["subject"], r["teacher"]) for r in before.get("removed", [])}
            gone = [r for r in info["removed"] if (r["hour"], r["subject"], r["teacher"]) not in old_removed]
            if gone:
                found.append({"kind": "כיתת חינוך", "date": day,
                              "text": f"{cls} · לא מתקיים: " + ", ".join(f"שעה {r['hour']} {r['subject']}" for r in gone[:6])})
            old_changes = {(c["hour"], c["text"]) for c in before.get("changes", [])}
            # Fill-ins don't move the end of the day, so they wait for the morning summary.
            new_changes = [c for c in info["changes"] if (c["hour"], c["text"]) not in old_changes and c["type"] != "מילוי מקום"]
            if new_changes and not gone:
                found.append({"kind": "כיתת חינוך", "date": day,
                              "text": f"{cls} · " + ", ".join(sorted({fill(c['text']) for c in new_changes}))})
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


def run_person(person, raw, today, offline, morning):
    data = ROOT / person["data_dir"]
    previous = load_json(data / "latest.json", {})
    changelog = load_json(data / "changelog.json", [])
    history = load_json(data / "history.json", {})

    if offline:
        latest, found = previous, []
    else:
        latest = scrape(person["name_key"], raw=raw, end_grades=person.get("end_time_grades", []),
                        extra_lessons=person.get("extra_lessons", []), homeroom=person.get("homeroom"))
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

    # Notifications: a morning summary of the day, and later only changes that land on today.
    today_found = todays_changes(found, today)
    if offline:
        title, text = None, None
    elif morning:
        title, text = "☀️ היום שלך", morning_summary(latest, today)
    elif today_found:
        title, text = "⚠️ שינוי במערכת להיום", "\n".join(f"• {f['kind']}: {fill(f['text'])}" for f in today_found[:12])
    else:
        title, text = None, None
    note = ""
    topic = SECRETS.get("ntfy_topics", {}).get(person["display_name"])
    if text and topic:
        sent = notify_ntfy(topic, title, text, person.get("artifact_url", ""))
        note = f"ntfy notification to {person['display_name']}: {'sent' if sent else 'FAILED'}"
        print(note)
    print(f"=== {person['display_name']} ===")
    print(f"lessons found: {len(latest['lessons'])} across {len(latest['days'])} days")
    print(f"changes since last run: {len(found)}")
    for f in found:
        print(f"  [{f['kind']}] {f['text']}")
    print(f"changes for today (notified): {len(today_found)}")
    print(f"page: {out.relative_to(ROOT)}")
    if person.get("owner") and text:
        return f"{title}\n{text}"
    if note.endswith("FAILED"):
        return f"⚠️ ההתראה ל{person['display_name']} לא נשלחה"
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--offline", action="store_true", help="skip scraping and rebuild the pages from saved data")
    ap.add_argument("--morning", action="store_true", help="send the daily summary (default before 08:00)")
    args = ap.parse_args()

    now = datetime.now(ISRAEL_TZ)
    raw = None
    if not args.offline:
        raw = collect(now=now)
        if not raw["classes"] or not raw["days"]:
            sys.exit("scrape returned no classes or days; keeping the previous data")
    morning = args.morning or now.hour < 8
    push = [m for m in (run_person(p, raw, now.date().isoformat(), args.offline, morning) for p in CONFIG["people"]) if m]
    # The routine that runs this sends the block below to the owner's phone as is.
    if push:
        print("=== PUSH FOR OWNER ===")
        print("\n\n".join(push))
        print("=== END PUSH ===")
    else:
        print("=== NO PUSH ===")


if __name__ == "__main__":
    main()
