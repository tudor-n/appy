"""Live check: does the model call the memory tools when (and only when) it should?

Sends a few prompts to the LLM in .env (BASE_URL / MODEL) with the real system prompt
plus a sample memory block and all 4 tool schemas, the way main.py does, and prints
which tools each run called. Nothing is saved anywhere; the tools are never executed.

    .venv/bin/python dev/check_memory_tools.py [runs per prompt, default 3]
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
from openai import OpenAI

import tools
from memory_client import system_prompt

SAMPLE = {
    "core": "- Tudor lives in Sibiu.",
    "index": ("appy | Appy | Tudor's voice assistant project | 2026-11-26\n"
              "lab-printer | Lab printer | Prusa MK4 setup | 2026-11-20"),
}
# (prompt, expected tool or None, argument checks)
CASES = [
    ("remember that my bench PSU is a Riden RD6024", "memory_remember", {}),
    ("what do you know about the appy project?", "memory_read", {"slug": "appy"}),
    ("forget that I live in Sibiu", "memory_forget", {"slug": "core"}),
    ("what's the weather?", "get_weather", {}),
    ("I bought a new keyboard today", None, {}),
]
MEMORY_WRITES = {"memory_remember", "memory_forget"}


def main():
    load_dotenv(ROOT / ".env")
    if not os.getenv("BASE_URL"):  # never fall back to the OpenAI client's default host
        sys.exit("BASE_URL is not set (.env)")
    runs = int(sys.argv[1]) if len(sys.argv) > 1 else 3
    base = (ROOT / os.getenv("PROMPT_FILE", "prompt.txt")).read_text(encoding="utf-8").strip()
    prompt = system_prompt(base, SAMPLE)
    client = OpenAI(base_url=os.getenv("BASE_URL"),
                    api_key=os.getenv("API_KEY", "ollama"))
    model = os.getenv("MODEL", "google/gemma-4-26b-a4b")
    schemas = tools.TOOL_SCHEMAS + tools.MEMORY_TOOL_SCHEMAS
    print(f"model {model}, {len(schemas)} tools, system prompt {len(prompt)} chars\n")

    all_ok = True
    for text, expected, checks in CASES:
        hits = 0
        print(f"> {text}   (expect {expected or 'no memory write'})")
        for _ in range(runs):
            r = client.chat.completions.create(
                model=model,
                messages=[{"role": "system", "content": prompt},
                          {"role": "user", "content": f"[09:00 PM] {text}"}],
                tools=schemas, temperature=0.4, reasoning_effort="none", max_tokens=300,
            )
            msg = r.choices[0].message
            calls = [(c.function.name, c.function.arguments) for c in msg.tool_calls or ()]
            print(f"    {calls or '(no call) ' + (msg.content or '')[:80]!s}")
            names = {name for name, _ in calls}
            if expected is None:
                ok = not names & MEMORY_WRITES
            else:
                ok = any(name == expected and all(f'"{k}":' in args.replace(" ", "")
                                                  and f'"{v}"' in args for k, v in checks.items())
                         for name, args in calls)
            hits += ok
        need = runs if expected is None else -(-2 * runs // 3)
        passed = hits >= need
        all_ok &= passed
        print(f"    {'PASS' if passed else 'FAIL'} {hits}/{runs}\n")
    print("ALL PASS" if all_ok else "SOME FAILED")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
