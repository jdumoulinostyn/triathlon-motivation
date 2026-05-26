import sys, asyncio
sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, r"C:\Users\jdumoulin\Documents\trainingpeaks-mcp\src")
from tp_mcp.tools.workouts import tp_get_workouts

async def run():
    result = await tp_get_workouts(start_date="2026-05-05", end_date="2026-05-26")
    workouts = result.get("workouts", [])
    print(f"{'Datum':<12} {'Type':<10} {'Titel':<32} {'Gep':>5} {'Eff':>5} {'TSSa':>6} {'%':>5}  Beschrijving")
    print("-" * 130)
    for w in workouts:
        dur_p = w.get("duration_planned")
        dur_a = w.get("duration_actual")
        tss_a = w.get("tss_actual")
        tss_p = w.get("tss_planned")
        completion = None
        if tss_p and tss_a:
            completion = round(tss_a / tss_p * 100)
        elif dur_p and dur_a:
            completion = round(dur_a / dur_p * 100)
        dp = f"{round(dur_p*60)}m" if dur_p else "  ?"
        da = f"{round(dur_a*60)}m" if dur_a else "  ?"
        cp = f"{completion}%" if completion else "?"
        desc = (w.get("description") or "")[:60].replace("\n", " ")
        title = (w.get("title") or "?")[:30]
        wtype = (w.get("type") or "?")[:9]
        print(f"{w.get('date','?'):<12} {wtype:<10} {title:<32} {dp:>5} {da:>5} {str(tss_a or '?'):>6} {cp:>5}  {desc}")

asyncio.run(run())
