"""Scrape the Shahaf timetable site and extract one teacher's lessons.

The site is server-rendered: every class has a "מערכת ושינויים" tab
(?cls=<id>&tab=changestable&week=<offset>) holding the week grid with changes
merged in, and a "שינויים" tab (?cls=<id>&tab=changes) listing changes as text.
Only the standard library is used so the scraper runs anywhere.
"""

import html
import re
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone

BASE_URL = "https://techni.shahaf.site/"
USER_AGENT = "Mozilla/5.0 (lesson-tracker)"
REQUEST_DELAY = 0.3

CHANGE_TYPES = {
    "TableFillChange": "מילוי מקום",
    "TableFreeChange": "ביטול / שעה חופשית",
    "TableExamChange": "מבחן",
    "TableEventChange": "אירוע",
    "TableRemoteChange": "למידה מרחוק",
}

ISRAEL_TZ = timezone(timedelta(hours=3))  # close enough for picking "today"; DST shift is irrelevant at 07:00


def fetch(url, attempts=5):
    last_error = None
    for i in range(attempts):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=40) as resp:
                text = resp.read().decode("utf-8")
            time.sleep(REQUEST_DELAY)
            return text
        except (urllib.error.URLError, ConnectionError, TimeoutError) as e:
            last_error = e
            time.sleep(2 ** i)
    raise RuntimeError(f"failed to fetch {url}: {last_error}")


def clean(fragment):
    """Strip tags, decode entities and collapse whitespace."""
    text = re.sub(r"<br\s*/?>", "\n", fragment)
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)
    lines = [re.sub(r"\s+", " ", line).strip() for line in text.split("\n")]
    return "\n".join(line for line in lines if line)


def parse_classes(page):
    select = re.search(r'<select[^>]*name="cls"[^>]*>(.*?)</select>', page, re.S)
    if not select:
        raise RuntimeError("class selector not found on the home page")
    return [
        {"id": cid, "name": clean(name)}
        for cid, name in re.findall(r'<option value="(\d+)"[^>]*>(.*?)</option>', select.group(1), re.S)
    ]


def resolve_date(day_month, today):
    """Turn "04.10" into a date, picking the year closest to today."""
    d, m = (int(x) for x in day_month.split("."))
    candidates = []
    for year in (today.year - 1, today.year, today.year + 1):
        try:
            candidates.append(date(year, m, d))
        except ValueError:
            pass
    return min(candidates, key=lambda c: abs((c - today).days))


def parse_lesson(fragment):
    subject = re.search(r"<b>(.*?)</b>", fragment, re.S)
    subject = clean(subject.group(1)) if subject else ""
    head, _, tail = fragment.partition("<br")
    tail = tail.split(">", 1)[1] if ">" in tail else ""
    room = re.search(r"</b>\s*\((.*?)\)", head, re.S)
    return {
        "subject": subject,
        "room": clean(room.group(1)) if room else "",
        "teacher": clean(tail).replace("\n", ", "),
    }


def parse_grid(page, today):
    """Return {"days": [...], "cells": [...]} for one class/week grid."""
    table = re.search(r'<table class="TTTable".*?</table>', page, re.S)
    if not table:
        return {"days": [], "cells": []}
    table = table.group(0)

    days = {}
    for idx, inner in re.findall(r'<td class="CTitle" data-day="(\d+)">(.*?)</td>', table, re.S):
        holidays = [clean(h) for h in re.findall(r'<span class="Holiday">(.*?)</span>', inner, re.S)]
        title = clean(re.sub(r'<span class="Holiday">.*?</span>', "", inner, flags=re.S))
        dm = re.search(r"(\d{1,2}\.\d{1,2})", title)
        if not dm:
            continue
        days[idx] = {
            "date": resolve_date(dm.group(1), today).isoformat(),
            "weekday": title.replace(dm.group(1), "").strip(),
            "holidays": [h for h in holidays if h],
        }

    cells = []
    for row in re.findall(r"<tr>(.*?)</tr>", table, re.S):
        name = re.search(r'<td class="CName"[^>]*>(.*?)</td>', row, re.S)
        if not name or 'class="TTCell"' not in row:
            continue
        hour = re.search(r"<b>\s*(\d+)", name.group(1))
        times = re.findall(r'<span class="hour-time">(.*?)</span>', name.group(1))
        for idx, inner in re.findall(r'<td class="TTCell" data-day="(\d+)">(.*?)</td>', row, re.S):
            if idx not in days:
                continue
            lessons, changes = [], []
            for cls, body in re.findall(r'<div class="([^"]+)"[^>]*>(.*?)</div>', inner, re.S):
                if cls.split()[0] == "TTLesson":
                    lessons.append(parse_lesson(body))
                elif "TTChange" in cls:
                    kind = next((CHANGE_TYPES[c] for c in cls.split() if c in CHANGE_TYPES), "שינוי")
                    changes.append({"type": kind, "text": clean(body).replace("\n", " · ")})
            if lessons or changes:
                cells.append({
                    "date": days[idx]["date"],
                    "hour": int(hour.group(1)) if hour else None,
                    "start": times[0] if times else "",
                    "end": times[1] if len(times) > 1 else "",
                    "lessons": lessons,
                    "changes": changes,
                })
    return {"days": list(days.values()), "cells": cells}


def parse_changes_tab(page):
    """Return the text items of the "שינויים" tab, each tagged with its date heading."""
    start = page.find("nav-tabs")
    end = page.find('class="UpdateDate"', start)
    section = page[page.find("</ul>", start) + 5: end if end > 0 else None]
    if "EmptyList" in section:
        return []
    items, current_date = [], ""
    for cls, body in re.findall(r'class="(ChangesDate|ChangesInfo)[^"]*"[^>]*>(.*?)</(?:div|span|td|li|p|h\d)>', section, re.S):
        text = clean(body).replace("\n", " · ")
        if cls == "ChangesDate":
            current_date = text
        elif text:
            items.append({"date_label": current_date, "text": text})
    if not items:
        # Unknown markup: keep the raw lines so nothing is silently lost.
        for line in clean(section).split("\n"):
            items.append({"date_label": "", "text": line})
    return items


def parse_update_date(page):
    m = re.search(r'class="UpdateDate">(.*?)</div>', page, re.S)
    return clean(m.group(1)) if m else ""


def scrape(name_key, weeks=(0, 1), now=None):
    """Scrape every class and return the teacher's lessons plus raw context."""
    now = now or datetime.now(ISRAEL_TZ)
    today = now.date()
    home = fetch(BASE_URL)
    classes = parse_classes(home)

    days = {}
    slots = {}          # (date, hour) -> list of (class, cell)
    class_changes = []  # items from the "שינויים" tab of classes the teacher meets
    site_update = parse_update_date(home)
    tab_items = {}

    for c in classes:
        for w in weeks:
            url = f"{BASE_URL}?cls={c['id']}&tab=changestable" + (f"&week={w}" if w else "")
            page = fetch(url)
            site_update = parse_update_date(page) or site_update
            grid = parse_grid(page, today)
            for d in grid["days"]:
                days.setdefault(d["date"], d)
            for cell in grid["cells"]:
                slots.setdefault((cell["date"], cell["hour"]), []).append((c["name"], cell))
        tab_items[c["name"]] = parse_changes_tab(fetch(f"{BASE_URL}?cls={c['id']}&tab=changes"))

    def is_me(text):
        return name_key in text

    lessons = {}
    for (day, hour), entries in slots.items():
        for class_name, cell in entries:
            mine = [l for l in cell["lessons"] if is_me(l["teacher"])]
            noted = [ch for ch in cell["changes"] if is_me(ch["text"])]
            if not mine and noted:
                # A substitution or event naming the teacher in a slot without a regular lesson.
                mine = [{"subject": noted[0]["text"], "room": "", "teacher": name_key}]
            for l in mine:
                key = (day, hour, l["subject"])
                entry = lessons.setdefault(key, {
                    "date": day, "hour": hour, "start": cell["start"], "end": cell["end"],
                    "subject": l["subject"], "classes": [], "rooms": [], "co_teachers": [],
                    "other_lessons": [], "changes": [],
                })
                if class_name not in entry["classes"]:
                    entry["classes"].append(class_name)
                if l["room"] and l["room"] not in entry["rooms"]:
                    entry["rooms"].append(l["room"])
                for other in cell["lessons"]:
                    if is_me(other["teacher"]) or not other["teacher"]:
                        continue
                    if other["subject"] == l["subject"]:
                        co = {"name": other["teacher"], "room": other["room"]}
                        if co not in entry["co_teachers"]:
                            entry["co_teachers"].append(co)
                    else:
                        item = {"class": class_name, **other}
                        if item not in entry["other_lessons"]:
                            entry["other_lessons"].append(item)
                for ch in cell["changes"]:
                    item = {"class": class_name, **ch, "mentions_me": is_me(ch["text"])}
                    if item not in entry["changes"]:
                        entry["changes"].append(item)

    my_classes = sorted({c for l in lessons.values() for c in l["classes"]})
    for class_name, items in tab_items.items():
        for it in items:
            if class_name in my_classes or is_me(it["text"]):
                class_changes.append({"class": class_name, **it, "mentions_me": is_me(it["text"])})

    ordered = sorted(lessons.values(), key=lambda l: (l["date"], l["hour"] if l["hour"] is not None else 99, l["subject"]))
    for l in ordered:
        l["classes"].sort()
        l["co_teachers"].sort(key=lambda c: c["name"])
    return {
        "scraped_at": now.isoformat(timespec="minutes"),
        "site_update": site_update,
        "class_count": len(classes),
        "days": sorted(days.values(), key=lambda d: d["date"]),
        "lessons": ordered,
        "class_changes": class_changes,
    }
