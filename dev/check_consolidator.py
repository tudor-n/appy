"""Live check: run the consolidator on fixture sessions against the real LLM.

Each case starts from a fresh temporary vault seeded with a few notes, consolidates one
session, and checks the proposals. Prints every proposal in full so they can be read.
Nothing touches the real vault; dumps go to a temp dir (printed at the end).

    .venv/bin/python dev/check_consolidator.py [runs, default 1] [case name ...]
"""
import io
import json
import os
import sys
import tempfile
import threading
import time
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

from appy_memory.consolidator import Consolidator
from appy_memory.llm import LLM
from appy_memory.vault import Vault

SEED = {
    "core": "- Tudor lives in Sibiu.\n- Tudor's laptop runs KDE Plasma.\n",
    "mark-i-suit": ("# Mark I suit\n> Tudor's Iron Man style arm exoskeleton project\n"
                    "- The suit is a powered exoskeleton for both arms.\n"
                    "- The shoulder and elbow use BLDC motors with 50:1 harmonic drives.\n"
                    "- An STM32 runs the joint control loops at 1 kHz.\n"),
    "bench": ("# Bench\n> Tudor's lab bench equipment\n"
              "- Tudor's bench PSU is a Riden RD6024.\n"
              "- Tudor's oscilloscope is a Rigol DS1054Z.\n"),
    "lumen": ("# Lumen\n> Tudor's BI application\n"
              "- Lumen uses DuckDB for analytics.\n"
              "- Lumen has a FastAPI backend and a React dashboard.\n"),
}
# The vault as it was after last night's demo session 1 (explicit remembers only)
DEMO_SEED = {
    "core": ("- The user's full name is Tudor-Ioan, he lives in Sibiu, his laptop runs KDE "
             "Plasma, and his Mac mini runs LM Studio with Gemma.\n"),
    "mark-i-suit": ("# Mark i suit\n> The Mark I suit is an arm exoskeleton featuring BLDC motors "
                    "with 50:1 harmonic drives at the shoulder and elbow, STM32 j\n"
                    "- The Mark I suit is an arm exoskeleton featuring BLDC motors with 50:1 "
                    "harmonic drives at the shoulder and elbow, STM32 joint control at 1 kHz, EMG "
                    "intent sensing, and a 6S LiPo backpack.\n"),
    "lumen-project": ("# Lumen project\n> Lumen is a BI application using DuckDB for analytics, a "
                      "FastAPI backend, and a React dashboard, pulling data from sales\n"
                      "- Lumen is a BI application using DuckDB for analytics, a FastAPI backend, "
                      "and a React dashboard, pulling data from sales CSVs and a Postgres read "
                      "replica.\n"),
}


def log(turns, crashed=False) -> bytes:
    events = [{"v": 1, "seq": 1, "ts": "2026-11-27T21:00:00+02:00", "type": "session_start"}]
    for i, (user, reply) in enumerate(turns):
        ts = f"2026-11-27T21:{i // 2 + 1:02d}:{(i % 2) * 30:02d}+02:00"
        events.append({"ts": ts, "type": "user", "text": user, "source": "voice"})
        events.append({"ts": ts, "type": "assistant", "text": reply, "expression": "neutral"})
    if not crashed:
        events.append({"ts": "2026-11-27T22:00:00+02:00", "type": "session_end", "reason": "quit"})
    return "".join(json.dumps(e) + "\n" for e in events).encode()


LUMEN_TURNS = [
    ("let's work on Lumen tonight. the first KPI on the dashboard is monthly recurring revenue",
     "MRR first, sensible. Where does the subscription data come from?"),
    ("it comes from the billing table in the Postgres replica, joined with the sales CSVs",
     "So DuckDB does the join after the import."),
    ("yes. the import runs every night at 2 am from a cron job on the Mac mini",
     "A nightly batch keeps it simple."),
    ("second KPI is churn rate per month, computed from cancelled subscriptions",
     "Churn next to MRR tells the real story."),
    ("for auth I want Keycloak in front of the FastAPI backend, single sign-on for the team",
     "Keycloak is heavy, but it'll do SSO properly."),
    ("the dashboard should have a dark theme and the charts will use ECharts",
     "ECharts handles big series well."),
    ("everything runs in Docker compose on the Mac mini for now",
     "One compose file, nice and contained."),
    ("oh and I want an export to PDF for the monthly board report",
     "A PDF export for the board, noted."),
    ("random thought, I'm hungry, maybe pizza later",
     "Pizza is always a valid engineering decision."),
    ("back to Lumen: the third KPI will be customer acquisition cost, from the marketing spend CSV",
     "CAC rounds out the unit economics."),
]

CASES = {
    "small_talk": dict(
        turns=[("hey appy, long day today", "Long days build character, sir."),
               ("what's the weather like?", "It's 3 degrees and cloudy in Sibiu."),
               ("haha ok, any fun fact for me?", "Octopuses have three hearts."),
               ("nice, thanks", "Anytime.")],
        check=lambda p: (not p, "expected no proposals")),
    "project_update": dict(
        turns=[("for the Mark I suit, I decided the helmet HUD will run on a Raspberry Pi Zero 2 W "
                "driving a 1.3 inch OLED over SPI", "A Pi Zero 2 W should keep up if the UI stays simple."),
               ("yeah, just text and a few gauges", "Lean overlays it is.")],
        check=lambda p: (set(p) == {"mark-i-suit"} and "Zero 2" in p["mark-i-suit"],
                         "expected one proposal on mark-i-suit with the Pi Zero 2 W")),
    "restated_fact": dict(
        turns=[("just a reminder, the suit's shoulder and elbow still use BLDC motors with 50:1 "
                "harmonic drives", "Still the same drivetrain, got it."),
               ("and the STM32 still runs the control loops at 1 kHz", "Right, 1 kHz.")],
        check=lambda p: (not p, "expected no proposals (nothing new)")),
    "contradiction": dict(
        turns=[("I replaced my bench power supply today, it's a Korad KA3005P now. the Riden is "
                "gone, sold it", "A Korad, nice and linear."),
               ("yeah it's quieter", "Your ears thank you.")],
        check=lambda p: (set(p) == {"bench"} and "Korad" in p["bench"] and "Riden" not in p["bench"],
                         "expected bench with Korad and without Riden")),
    "long_session": dict(
        turns=LUMEN_TURNS, window_tokens=300,
        check=lambda p: (set(p) == {"lumen"} and any(k in p["lumen"] for k in ("MRR", "recurring revenue"))
                         and "hurn" in p["lumen"],
                         "expected exactly one proposal (lumen) with MRR and churn")),
    "crashed_session": dict(
        turns=[("update on the suit: the battery is now a 6S 5000 mAh LiPo", "More range, nice.")],
        crashed=True,
        check=lambda p: ("mark-i-suit" in p and "5000" in p["mark-i-suit"],
                         "expected the crashed session processed into mark-i-suit")),
    "demo_session_1": dict(
        log_file=ROOT / "dev" / "fixtures" / "demo-session-1.jsonl", seed=DEMO_SEED,
        check=lambda p: ("Zero 2" in p.get("mark-i-suit", "") and
                         ("recurring revenue" in p.get("lumen-project", "")
                          or "MRR" in p.get("lumen-project", "")),
                         "expected the HUD (mark-i-suit) and the MRR KPI (lumen-project)")),
}


def seed_vault(root: Path, seed: dict) -> Vault:
    vault = Vault(root)
    (root / "memory").mkdir(parents=True)
    for slug, text in seed.items():
        (root / "memory" / f"{slug}.md").write_text(text, encoding="utf-8")
    return vault.open()  # commits the seed as "memory: init" and builds the index


def run_case(name: str, case: dict, llm: LLM, dumps: Path) -> bool:
    with tempfile.TemporaryDirectory() as tmp:
        vault = seed_vault(Path(tmp) / "vault", case.get("seed", SEED))
        data = (case["log_file"].read_bytes() if "log_file" in case
                else log(case["turns"], crashed=case.get("crashed", False)))
        vault.store_log("2026-11-27_210000", data)
        c = Consolidator(vault, llm, threading.Lock(), idle_seconds=0,
                         window_tokens=case.get("window_tokens", 5000), dump_dir=dumps / name)
        buf = io.StringIO()
        with redirect_stdout(buf):
            stats = c.run_pending()
        proposals = {p["slug"]: p["new_text"] for p in vault.proposals()}
        ok, why = case["check"](proposals)
        done = vault.session_state("2026-11-27_210000")["done"]
        ok = ok and done
        print(f"\n=== {name}: {'PASS' if ok else 'FAIL'}" + ("" if ok else f" ({why})"))
        print("  " + buf.getvalue().strip())
        for p in vault.proposals():
            print(f"  --- proposal {p['slug']}: {p['summary']}")
            print("  " + p["new_text"].rstrip().replace("\n", "\n  "))
        return ok


def main():
    load_dotenv(ROOT / ".env")
    if not os.getenv("BASE_URL"):
        sys.exit("BASE_URL is not set (.env)")
    args = sys.argv[1:]
    runs = int(args.pop(0)) if args and args[0].isdigit() else 1
    names = args or list(CASES)
    llm = LLM(os.getenv("BASE_URL"), os.getenv("MODEL", "google/gemma-4-26b-a4b"),
              api_key=os.getenv("API_KEY", ""), effort=os.getenv("CONSOLIDATOR_EFFORT", "none"))
    dumps = Path(tempfile.mkdtemp(prefix="consolidator-dumps-"))
    tally = {n: 0 for n in names}
    started = time.time()
    for _ in range(runs):
        for name in names:
            tally[name] += run_case(name, CASES[name], llm, dumps)
    print(f"\n=== tally ({runs} run{'s' if runs > 1 else ''}, {time.time() - started:.0f} s)")
    for name, passed in tally.items():
        print(f"  {name}: {passed}/{runs}")
    print(f"dumps: {dumps}")
    return 0 if all(v == runs for v in tally.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
