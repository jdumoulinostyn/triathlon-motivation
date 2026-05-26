#!/usr/bin/env python3
"""
Dagelijkse triathlon motivatie — Jonas Dumoulin
Leest TrainingPeaks data en stuurt een contextgebonden push-notificatie naar iPhone via ntfy.

Gebruik:
  python daily_motivation.py                → volledige versie met TrainingPeaks
  python daily_motivation.py --test         → testmodus, zonder TP-data
  python daily_motivation.py --check-tomorrow → controle morgen (geen notificatie)
"""

import asyncio
import json
import random
import sys
import urllib.request
from datetime import date, timedelta
from pathlib import Path

# UTF-8 output op Windows
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

# tp_mcp importeerbaar maken
sys.path.insert(0, str(Path(__file__).parent / "src"))

# ─── Configuratie ──────────────────────────────────────────────────────────────
NTFY_TOPIC  = "jonas-triathlon-mindset"
NTFY_URL    = "https://ntfy.sh"
QUOTES_FILE = Path(__file__).parent / "quotes.json"

# Drempels groene reeks / slechte week
GREEN_STREAK_MIN        = 4     # min. opeenvolgende groene sessies
BAD_WEEK_CONSECUTIVE    = 2     # opeenvolgende rood/oranje triggert slechte_week
BAD_WEEK_TOTAL          = 3     # of totaal X in 7 dagen
COMPLETION_GREEN        = 0.85  # >= 85% = groen
COMPLETION_RED          = 0.60  # < 60%  = rood, daartussen = oranje

# Sport-detectie keywords (op basis van titel + beschrijving)
BIKE_KW  = ["ride", "fiets", "zwift", "cycling", "bike", "pedal", "watt",
            "lsd ride", "base endurance", "vo2max", "threshold", "losrijden"]
RUN_KW   = ["run", "lopen", "running", "jog", "marathon", "loopje",
            "before breakfast run", "lsd run", "base building"]
SWIM_KW  = ["swim", "zwem", "pool", "water", "zwemmen"]

# VO2max / Threshold detectie in de titel
HARD_TITLE_KW = ["vo2max", "vo2", "threshold"]

# Eenvoudige / herstel sessie keywords
EASY_KW = ["base endurance", "base building", "losrijden", "easy",
           "before breakfast", "zone 1", "z1", "zone1", "lsd"]
# ──────────────────────────────────────────────────────────────────────────────


def days_to(target: date) -> int:
    return (target - date.today()).days


def load_quotes() -> dict:
    with open(QUOTES_FILE, encoding="utf-8") as f:
        return json.load(f)


# ─── Sport detectie ────────────────────────────────────────────────────────────

def detect_sport(title: str, description: str = "") -> str:
    """Detecteert sport op basis van titel en beschrijving (swim/bike/run/unknown)."""
    text = (title + " " + (description or "")).lower()
    if any(k in text for k in SWIM_KW):
        return "swim"
    if any(k in text for k in RUN_KW):
        return "run"
    if any(k in text for k in BIKE_KW):
        return "bike"
    return "unknown"


def is_hard_session(title: str) -> bool:
    """True als de titel een VO2max of drempeltraining aangeeft."""
    t = title.lower()
    return any(k in t for k in HARD_TITLE_KW)


def is_easy_session(title: str, description: str = "") -> bool:
    """True als het een herstel/basis sessie is."""
    text = (title + " " + (description or "")).lower()
    return any(k in text for k in EASY_KW)


# ─── Voltooiingsgraad ──────────────────────────────────────────────────────────

def get_completion(workout: dict) -> float | None:
    """
    Berekent voltooiingsgraad (0.0 – 1.0+).
    Geeft None terug voor toekomstige/onbepaalde trainingen.
    Geeft 0.0 terug voor overgeslagen geplande trainingen (datum verstreken).
    """
    wtype = workout.get("type", "")
    wdate = workout.get("date", "")
    today = date.today().isoformat()

    if wtype == "planned":
        if wdate and wdate < today:
            return 0.0   # gepland maar niet gedaan
        return None      # nog in de toekomst

    # Afgeronde training — gebruik TSS als beschikbaar, anders duur
    tss_p = workout.get("tss_planned")
    tss_a = workout.get("tss_actual")
    if tss_p and tss_a:
        return tss_a / tss_p

    dur_p = workout.get("duration_planned")
    dur_a = workout.get("duration_actual")
    if dur_p and dur_a:
        return dur_a / dur_p

    return 1.0  # voltooide training zonder details = groen


# ─── Streaks en slechte week ───────────────────────────────────────────────────

def analyse_recent(recent_workouts: list[dict]) -> dict:
    """
    Analyseert de afgelopen 7 dagen (fiets + loop, geen zwemmen).
    Geeft terug: green_streak, bad_total, bad_consecutive.
    """
    today = date.today().isoformat()

    # Filter: enkel fiets en loop, enkel verleden
    relevant = [
        w for w in recent_workouts
        if w.get("date", "") <= today
        and detect_sport(w.get("title", ""), w.get("description", "")) in ("bike", "run")
    ]
    # Sorteer oudste → nieuwste
    relevant.sort(key=lambda w: w.get("date", ""))

    green_streak = 0
    bad_total = 0
    bad_consecutive = 0
    max_consecutive = 0

    for w in relevant:
        comp = get_completion(w)
        if comp is None:
            continue
        if comp >= COMPLETION_GREEN:
            green_streak += 1
            bad_consecutive = 0
        else:
            bad_total += 1
            bad_consecutive += 1
            max_consecutive = max(max_consecutive, bad_consecutive)
            green_streak = 0  # reset streak bij slechte training

    return {
        "green_streak":     green_streak,
        "bad_total":        bad_total,
        "bad_consecutive":  max_consecutive,
    }


def yesterday_was_bad(recent_workouts: list[dict]) -> bool:
    """True als gisteren een fiets- of looptraining rood/oranje was."""
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    for w in recent_workouts:
        if w.get("date", "") != yesterday:
            continue
        if detect_sport(w.get("title", ""), w.get("description", "")) not in ("bike", "run"):
            continue
        comp = get_completion(w)
        if comp is not None and comp < COMPLETION_GREEN:
            return True
    return False


# ─── TrainingPeaks data ophalen ────────────────────────────────────────────────

async def get_training_context() -> dict:
    """
    Haalt alle benodigde data op uit TrainingPeaks:
    - Fitness (CTL/ATL/TSB)
    - Vandaag's trainingen
    - Afgelopen 10 dagen (voor streak/slechte-week analyse)
    - Eerstvolgende wedstrijd + A-race
    """
    try:
        from tp_mcp.tools.fitness  import tp_get_fitness
        from tp_mcp.tools.workouts import tp_get_workouts
        from tp_mcp.tools.events   import tp_get_next_event, tp_get_focus_event

        today    = date.today()
        tomorrow = today + timedelta(days=1)
        ten_ago  = today - timedelta(days=10)

        # Fitness
        fitness = await tp_get_fitness(days=14)
        current = fitness.get("current") or {}
        tsb = current.get("tsb", 0.0)
        ctl = current.get("ctl", 0.0)
        atl = current.get("atl", 0.0)

        # Vandaag
        w_today = await tp_get_workouts(start_date=str(today), end_date=str(today))
        workouts_today = w_today.get("workouts", []) if isinstance(w_today, dict) else []

        # Afgelopen 10 dagen (inclusief vandaag)
        w_recent = await tp_get_workouts(start_date=str(ten_ago), end_date=str(today))
        workouts_recent = w_recent.get("workouts", []) if isinstance(w_recent, dict) else []

        # Wedstrijden
        next_raw    = await tp_get_next_event()
        focus_raw   = await tp_get_focus_event()
        next_event  = next_raw.get("event")  if isinstance(next_raw,  dict) else None
        focus_event = focus_raw.get("event") if isinstance(focus_raw, dict) else None

        return {
            "tsb": round(tsb, 1), "ctl": round(ctl, 1), "atl": round(atl, 1),
            "workouts_today":  workouts_today,
            "workouts_recent": workouts_recent,
            "next_event":      next_event,
            "focus_event":     focus_event,
            "error":           None,
        }

    except Exception as exc:
        return {"error": str(exc)}


# ─── Hulpfuncties events ───────────────────────────────────────────────────────

def _event_days(event: dict | None) -> int | None:
    if not event:
        return None
    raw = event.get("eventDate", "")
    if not raw:
        return None
    try:
        return days_to(date.fromisoformat(raw.split("T")[0]))
    except ValueError:
        return None


def _is_a_race(event: dict | None) -> bool:
    return bool(event) and event.get("atpPriority") == "A"


# ─── Categorie bepalen ─────────────────────────────────────────────────────────

def determine_category(context: dict) -> str:
    """
    Prioriteitsvolgorde:
    1. Racedag A  2. Racedag B  3. Raceweek A  4. Raceweek B
    5. Slechte week  6. VO2max/Threshold  7. LSD ride  8. LSD run
    9. Groene reeks  10. Gisteren rood  11. Goggins (zeer vermoeid)
    12. Herstel  13. Comeback  14. Rustdag  15. Na werkdag  16. General
    """
    # ── Wedstrijden ──────────────────────────────────────────────────────────
    next_event  = context.get("next_event")
    focus_event = context.get("focus_event")
    days_next   = _event_days(next_event)
    days_focus  = _event_days(focus_event)

    if days_next == 0:
        return "race_day_a" if _is_a_race(next_event) else "race_day_b"

    if days_focus is not None and 0 < days_focus <= 7:
        return "race_week_a"

    if days_next is not None and 0 < days_next <= 5 and not _is_a_race(next_event):
        return "race_week_b"

    # ── Trainingsdata ────────────────────────────────────────────────────────
    if context.get("error"):
        return "general"

    tsb              = context.get("tsb", 0.0)
    ctl              = context.get("ctl", 0.0)
    workouts_today   = context.get("workouts_today", [])
    workouts_recent  = context.get("workouts_recent", [])

    analysis = analyse_recent(workouts_recent)
    green_streak    = analysis["green_streak"]
    bad_total       = analysis["bad_total"]
    bad_consecutive = analysis["bad_consecutive"]

    # ── Slechte week (2 opeenvolgend of 3 totaal) ───────────────────────────
    if bad_consecutive >= BAD_WEEK_CONSECUTIVE or bad_total >= BAD_WEEK_TOTAL:
        return "slechte_week"

    # ── Geen training vandaag ────────────────────────────────────────────────
    if not workouts_today:
        return "rest_day"

    # Analyseer de training(en) van vandaag
    today_titles = " ".join(w.get("title", "") for w in workouts_today)
    today_desc   = " ".join(w.get("description", "") or "" for w in workouts_today)
    today_sport  = detect_sport(today_titles, today_desc)

    # Totale geplande duur in uren
    total_dur_h = sum(
        w.get("duration_planned") or 0
        for w in workouts_today
    )

    # ── VO2max / Threshold sessie ────────────────────────────────────────────
    if is_hard_session(today_titles):
        return "pre_vo2max"

    # ── Lange duurtraining ───────────────────────────────────────────────────
    if today_sport == "bike" and total_dur_h >= 3.0 and not is_hard_session(today_titles):
        return "lsd_ride"

    if today_sport == "run" and total_dur_h >= 1.5 and not is_hard_session(today_titles):
        return "lsd_run"

    # ── Groene reeks ─────────────────────────────────────────────────────────
    if green_streak >= GREEN_STREAK_MIN:
        return "groene_reeks"

    # ── Gisteren rood/oranje ─────────────────────────────────────────────────
    if yesterday_was_bad(workouts_recent):
        return "vorige_training_rood"

    # ── Goggins: TSB zeer negatief ───────────────────────────────────────────
    if tsb < -20:
        return "goggins"

    # ── Herstel: rustige/korte sessie of laag TSB ────────────────────────────
    if is_easy_session(today_titles, today_desc) or total_dur_h < 1.0 or tsb < -10:
        return "herstel"

    # ── Comeback: lage CTL, TSB herstellend ──────────────────────────────────
    if ctl < 55 and tsb > 3:
        return "comeback"

    # ── Standaard avondtraining ──────────────────────────────────────────────
    return "na_werkdag"


# ─── Quote kiezen ──────────────────────────────────────────────────────────────

def pick_quote(category: str, quotes: dict) -> dict:
    pool = quotes.get(category) or quotes.get("general", [])
    if not pool:
        return {"text": "Every day is a step forward.", "source": ""}
    return random.choice(pool)


# ─── Bericht samenstellen ──────────────────────────────────────────────────────

def build_payload(quote: dict, category: str, context: dict | None = None) -> dict:
    context     = context or {}
    focus_event = context.get("focus_event")
    next_event  = context.get("next_event")
    days_focus  = _event_days(focus_event)
    days_next   = _event_days(next_event)
    a_name      = (focus_event or {}).get("name", "WK Nice") if focus_event else "WK Nice"
    countdown   = f"{days_focus} dagen" if days_focus is not None else "?"
    next_name   = (next_event or {}).get("name", "Race")    if next_event else "Race"

    titles = {
        "race_day_a":        f"🏁 RACEDAG — {a_name}",
        "race_day_b":        f"🏁 RACEDAG — {next_name}",
        "race_week_a":       f"🎯 Race Week — {a_name} over {countdown}",
        "race_week_b":       f"🎯 Race Week — {next_name} over {days_next} dagen" if days_next else "🎯 Race Week",
        "pre_vo2max":        "🔥 VO2max / Threshold vandaag — ga ervoor",
        "lsd_ride":          "🚴 Lange rit vandaag — settle in",
        "lsd_run":           "🏃 Lange run vandaag — vertrouw je benen",
        "groene_reeks":      "💚 Groene reeks — stay locked in",
        "slechte_week":      "⚠️ Herpak je — Nu",
        "vorige_training_rood": "🔁 Gisteren oranje — vandaag groen",
        "herstel":           "🧘 Hersteldag — bescherm de opbouw",
        "comeback":          f"📈 Weg naar {a_name} — {countdown}",
        "rest_day":          "⚡ Rustdag — mentaal laden",
        "goggins":           "💪 Push door — geen excuses",
        "na_werkdag":        f"⏱ {a_name} — {countdown}",
        "general":           f"⏱ {a_name} — {countdown}",
    }

    title = titles.get(category, f"⏱ {a_name} — {countdown}")

    text    = quote.get("text", "")
    source  = quote.get("source", "")
    message = f'"{text}"' if not source else f'"{text}"\n— {source}'

    priority = 4 if category in ("race_day_a", "race_day_b", "race_week_a") else 3

    tags_map = {
        "race_day_a":           ["fire", "trophy"],
        "race_day_b":           ["fire", "calendar"],
        "race_week_a":          ["fire", "calendar"],
        "race_week_b":          ["calendar"],
        "pre_vo2max":           ["muscle", "fire"],
        "lsd_ride":             ["bike"],
        "lsd_run":              ["runner"],
        "groene_reeks":         ["white_check_mark"],
        "slechte_week":         ["warning"],
        "vorige_training_rood": ["arrows_counterclockwise"],
        "herstel":              ["zzz"],
        "comeback":             ["arrow_up"],
        "rest_day":             ["seedling"],
        "goggins":              ["muscle"],
        "na_werkdag":           ["stopwatch"],
        "general":              ["stopwatch"],
    }

    return {
        "topic":    NTFY_TOPIC,
        "title":    title,
        "message":  message,
        "priority": priority,
        "tags":     tags_map.get(category, ["stopwatch"]),
    }


# ─── Versturen ────────────────────────────────────────────────────────────────

def send_notification(payload: dict) -> bool:
    data = json.dumps(payload).encode("utf-8")
    req  = urllib.request.Request(
        NTFY_URL,
        data=data,
        headers={"Content-Type": "application/json"},
    )
    try:
        r = urllib.request.urlopen(req, timeout=10)
        return r.status == 200
    except Exception as exc:
        print(f"[FOUT] ntfy: {exc}")
        return False


# ─── Check-tomorrow modus ─────────────────────────────────────────────────────

async def run_check_tomorrow() -> None:
    """
    Controleert of morgen's training al in TrainingPeaks staat.
    Geeft een voorspelling van de categorie van morgen.
    Stuurt GEEN notificatie — enkel logging.
    """
    try:
        from tp_mcp.tools.fitness  import tp_get_fitness
        from tp_mcp.tools.workouts import tp_get_workouts
        from tp_mcp.tools.events   import tp_get_next_event, tp_get_focus_event
    except ImportError as exc:
        print(f"[FOUT] tp_mcp niet gevonden: {exc}")
        sys.exit(1)

    today    = date.today()
    tomorrow = today + timedelta(days=1)
    ten_ago  = today - timedelta(days=10)

    print(f"[CHECK] Controle voor morgen: {tomorrow} (CEST 13:00 dagelijkse check)")

    try:
        fitness  = await tp_get_fitness(days=14)
        current  = fitness.get("current") or {}
        tsb = current.get("tsb", 0.0)
        ctl = current.get("ctl", 0.0)
        atl = current.get("atl", 0.0)

        w_tomorrow = await tp_get_workouts(start_date=str(tomorrow), end_date=str(tomorrow))
        workouts_tomorrow = w_tomorrow.get("workouts", []) if isinstance(w_tomorrow, dict) else []

        w_recent = await tp_get_workouts(start_date=str(ten_ago), end_date=str(today))
        workouts_recent = w_recent.get("workouts", []) if isinstance(w_recent, dict) else []

        next_raw    = await tp_get_next_event()
        focus_raw   = await tp_get_focus_event()
        next_event  = next_raw.get("event")  if isinstance(next_raw,  dict) else None
        focus_event = focus_raw.get("event") if isinstance(focus_raw, dict) else None

    except Exception as exc:
        print(f"[FOUT] TrainingPeaks niet bereikbaar: {exc}")
        sys.exit(1)

    # Toon fitness
    print(f"[CHECK] TSB: {round(tsb, 1)}  |  CTL: {round(ctl, 1)}  |  ATL: {round(atl, 1)}")

    # Toon morgen's trainingen
    print(f"[CHECK] Trainingen morgen: {len(workouts_tomorrow)}")
    for w in workouts_tomorrow:
        title = w.get("title", "?")
        dur   = w.get("duration_planned") or w.get("duration_actual")
        dur_s = f"{round(dur * 60)}min" if dur else "?"
        sport = detect_sport(title, w.get("description", ""))
        print(f"         [{sport.upper()}] {title} — {dur_s}")

    if not workouts_tomorrow:
        print("[WARN]  Geen trainingen gevonden voor morgen.")
        print("[WARN]  Coach heeft mogelijk nog niet geladen — check TrainingPeaks.")

    # Voorspel categorie (context met morgen's workouts als 'vandaag')
    context_tomorrow = {
        "tsb":              round(tsb, 1),
        "ctl":              round(ctl, 1),
        "atl":              round(atl, 1),
        "workouts_today":   workouts_tomorrow,
        "workouts_recent":  workouts_recent,
        "next_event":       next_event,
        "focus_event":      focus_event,
        "error":            None,
    }
    category = determine_category(context_tomorrow)
    quotes   = load_quotes()
    example  = pick_quote(category, quotes)

    print(f"[CHECK] Verwachte categorie morgen : {category}")
    print(f"[CHECK] Voorbeeldquote             : {example['text']}")
    if example.get("source"):
        print(f"[CHECK] Bron                       : {example['source']}")


# ─── Main ──────────────────────────────────────────────────────────────────────

async def main():
    test_mode           = "--test"           in sys.argv
    check_tomorrow_mode = "--check-tomorrow" in sys.argv

    # ── Check-tomorrow modus (13:00 dagelijkse controle, geen notificatie) ──
    if check_tomorrow_mode:
        await run_check_tomorrow()
        return

    # ── Testmodus ────────────────────────────────────────────────────────────
    if test_mode:
        print("[TEST] Testmodus — TrainingPeaks overgeslagen")
        context  = {"error": "test"}
        category = "general"
    else:
        print("[INFO] TrainingPeaks data ophalen...")
        context = await get_training_context()

        if context.get("error"):
            print(f"[WARN] TP niet bereikbaar ({context['error']}) — fallback naar 'general'")
        else:
            analysis = analyse_recent(context.get("workouts_recent", []))
            print(f"[INFO] TSB: {context['tsb']}  |  CTL: {context['ctl']}  |  ATL: {context['atl']}")
            print(f"[INFO] Trainingen vandaag: {len(context.get('workouts_today', []))}")
            print(f"[INFO] Groene reeks: {analysis['green_streak']}  |  "
                  f"Slechte week: {analysis['bad_total']} totaal, "
                  f"{analysis['bad_consecutive']} op rij")

        category = determine_category(context)

    print(f"[INFO] Categorie: {category}")

    quotes  = load_quotes()
    quote   = pick_quote(category, quotes)
    payload = build_payload(quote, category, context)

    print(f"[INFO] Titel:  {payload['title']}")
    print(f"[INFO] Quote:  {quote['text']}")
    if quote.get("source"):
        print(f"[INFO] Bron:   {quote['source']}")

    success = send_notification(payload)
    print("[OK]  Notificatie verstuurd!" if success else "[FOUT] Versturen mislukt.")
    if not success:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
