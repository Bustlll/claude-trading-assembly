"""CODE TOOL: the only way the evolve and verify kinds touch the code.

  codetool.py backup <path under agent_code/>   copy to agent_code/versions/<name>.<stamp>.bak first
  codetool.py selftest                          run every unit test (paper, guard, engine)
  codetool.py guard-check                       compare guard/*.py against guard/MANIFEST.json

Rule for the agent: back up before you edit, run selftest after you edit, and report both.
"""
import hashlib
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

HOME = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HOME / "tools"))


def guard_hashes() -> dict:
    out = {}
    for p in sorted([*(HOME / "guard").glob("*.py"), *(HOME / "guard").glob("*.json")]):
        if p.name == "MANIFEST.json":
            continue
        out[p.name] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


def guard_check() -> tuple[bool, str]:
    manifest_path = HOME / "guard" / "MANIFEST.json"
    if not manifest_path.exists():
        return False, "no guard manifest; the driver writes it at bootstrap"
    recorded = json.loads(manifest_path.read_text())["hashes"]
    now = guard_hashes()
    changed = sorted(k for k in set(recorded) | set(now) if recorded.get(k) != now.get(k))
    return (not changed), ("guard unchanged" if not changed else f"guard changed: {changed}")


def backup(rel: str) -> str:
    src = (HOME / "agent_code" / rel).resolve()
    if HOME / "agent_code" not in src.parents or not src.exists():
        raise SystemExit(f"not a file under agent_code/: {rel}")
    dst = HOME / "agent_code" / "versions" / f"{src.name}.{time.strftime('%Y%m%dT%H%M%S')}.bak"
    shutil.copy2(src, dst)
    return str(dst)


def failures(out: str, limit: int = 12) -> list[str]:
    """unittest's FAIL/ERROR blocks, one line each: the test and the last line of its traceback."""
    lines, res, cur = out.splitlines(), [], None
    for i, line in enumerate(lines):
        if line.startswith(("FAIL: ", "ERROR: ")):
            cur = line
        elif cur and line.startswith(("-----", "=====")) and i > 1 and not lines[i - 1].strip():
            last = next((x.strip() for x in reversed(lines[:i]) if x.strip()), "")
            res.append(f"{cur} -> {last[:300]}")   # the traceback ends with a blank line, then a separator
            cur = None
    return res[:limit]


def selftest() -> int:
    ok = True
    for suite in (HOME / "tests", HOME / "agent_code" / "tests", HOME.parent / "selfcall" / "tests"):
        if not suite.exists():
            continue
        proc = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", str(suite)],
                              capture_output=True, text=True, cwd=suite.parent)
        out = (proc.stderr or proc.stdout).strip()
        tail = out.splitlines()[-1:]
        print(f"{suite}: rc={proc.returncode} {tail}")
        if proc.returncode:
            # 2026-10-18: an evolve and its verify both reported "1 failure" and neither could name it --
            # print which test failed and its assertion, so whoever broke it can fix it
            for line in failures(out):
                print("  " + line)
        ok = ok and proc.returncode == 0
    good, msg = guard_check()
    print("guard:", msg)
    return 0 if ok and good else 1


def main(argv=None) -> int:
    argv = argv or sys.argv[1:]
    if not argv:
        print(__doc__)
        return 2
    if argv[0] == "backup" and len(argv) == 2:
        print(backup(argv[1]))
        return 0
    if argv[0] == "selftest":
        return selftest()
    if argv[0] == "guard-check":
        good, msg = guard_check()
        print(msg)
        return 0 if good else 1
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
