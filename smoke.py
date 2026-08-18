"""로컬에서 **사용자가 하듯이** 전 기능을 한 번씩 돌린다.

테스트(pytest)는 함수를 부른다. 이건 다르다 - 설치된 `jermes` 명령을 별도 프로세스로
띄워서, README 에 적힌 것이 **적힌 그대로 되는지** 본다. 둘은 다른 것을 잡는다:
pytest 는 로직을, 이건 배선(진입점·인자·경로·개행·인코딩)을 잡는다.

실제로 여기서만 잡히는 것들: 콘솔 인코딩, argparse 인자 이름, `pip install -e .` 로
깔린 진입점, 파일 경로 처리, 하위명령 사이의 상태 전달.

    python smoke.py            # LLM 없이 되는 것만
    python smoke.py --llm      # LLM 이 필요한 것까지 (툴 단조·ask)

⚠️ 임시 홈에서 돈다. 사용자의 `~/.jermes` 를 건드리지 않는다.
"""

from __future__ import annotations

import json
import re
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PASS, FAIL, SKIP = "OK  ", "실패", "건너뜀"


class Smoke:
    def __init__(self, use_llm: bool) -> None:
        self.use_llm = use_llm
        self.home = Path(tempfile.mkdtemp(prefix="jermes-smoke-"))
        self.results: list[tuple[str, str, str]] = []
        self.env = {
            **os.environ,
            "JERMES_HOME": str(self.home / "home"),
            # 사용자의 실제 스킬·MCP 설정을 안 건드리도록 빈 곳을 가리킨다.
            "JERMES_SKILL_PATH": str(self.home / "none"),
            "JERMES_MCP_CONFIG": str(self.home / "none.json"),
            "JERMES_CLAUDE_PROJECTS": str(self.home / "sessions"),
            # 원천을 늘려도 임시 홈이 사용자의 진짜 기록을 안 읽게 한다.
            "JERMES_SOURCES": "claude-code",
            "PYTHONUTF8": "1",
            "PYTHONIOENCODING": "utf-8",
        }

    def run(self, label: str, *args: str, expect: int = 0,
            contains: str | list[str] = "", llm: bool = False) -> str:
        """`jermes <args>` 를 별도 프로세스로. 반환은 stdout."""
        if llm and not self.use_llm:
            self.results.append((label, SKIP, "--llm 없이 실행"))
            return ""
        done = subprocess.run([sys.executable, "-m", "jermes.cli", *args],
                              capture_output=True, text=True, encoding="utf-8",
                              errors="replace", env=self.env, cwd=str(ROOT),
                              timeout=300)
        out = (done.stdout or "") + (done.stderr or "")
        problems = []
        if done.returncode != expect:
            problems.append(f"종료코드 {done.returncode} (기대 {expect})")
        for needle in ([contains] if isinstance(contains, str) else contains):
            if needle and needle not in out:
                problems.append(f"출력에 {needle!r} 없음")
        if "Traceback" in out:
            problems.append("트레이스백")
        self.results.append((label, FAIL if problems else PASS,
                             " · ".join(problems) or out.strip().splitlines()[-1][:70]
                             if out.strip() else ""))
        return out

    def report(self) -> int:
        print(f"\n{'항목':<34}{'':<7}비고")
        print("-" * 88)
        for label, status, note in self.results:
            print(f"{label:<34}{status:<7}{note[:46]}")
        failed = [r for r in self.results if r[1] == FAIL]
        skipped = [r for r in self.results if r[1] == SKIP]
        print(f"\n통과 {len(self.results) - len(failed) - len(skipped)} · "
              f"실패 {len(failed)} · 건너뜀 {len(skipped)}")
        shutil.rmtree(self.home, ignore_errors=True)
        return 1 if failed else 0


def main() -> int:
    smoke = Smoke("--llm" in sys.argv)
    print(f"임시 홈: {smoke.home}  (사용자의 ~/.jermes 는 안 건드립니다)")

    # ── LLM 없이 되어야 하는 것 ────────────────────────────────────────────
    smoke.run("맨손 jermes (현황)", contains="다음에 할 것")
    smoke.run("demo (게이트가 가르는지)", "demo", contains="promoted")
    smoke.run("list (빈 원장)", "list", contains="비어 있습니다")
    smoke.run("law (규약)", "law", contains="never_learn")
    smoke.run("memory (빈 기억)", "memory")
    smoke.run("capabilities (근처 능력)", "capabilities", contains="능력")
    smoke.run("sessions (배울 거리 없음)", "sessions", expect=1)

    # 케이스 파일 세 형식이 다 읽히는가
    cases_dir = smoke.home / "cases"
    cases_dir.mkdir(parents=True, exist_ok=True)
    rows = [(i, 1, i + 1) for i in range(12)]
    (cases_dir / "c.csv").write_text(
        "a,b,expect\n" + "".join(f"{a},{b},{e}\n" for a, b, e in rows), encoding="utf-8")
    (cases_dir / "c.jsonl").write_text(
        "".join(json.dumps({"a": a, "b": b, "expect": e}) + "\n" for a, b, e in rows),
        encoding="utf-8")
    (cases_dir / "c.json").write_text(
        json.dumps([[{"a": a, "b": b}, e] for a, b, e in rows]), encoding="utf-8")
    script = cases_dir / "add.py"
    script.write_text("def run(payload):\n    return payload['a'] + payload['b']\n",
                      encoding="utf-8")

    for suffix in ("csv", "jsonl", "json"):
        smoke.run(f"tool --script ({suffix} 케이스)", "tool", f"adder-{suffix}",
                  "--task", "두 수를 더한다", "--cases", str(cases_dir / f"c.{suffix}"),
                  "--script", str(script), contains="promoted")

    smoke.run("run (만든 툴 실행)", "run", "adder-csv", "--payload", '{"a": 40, "b": 2}',
              contains="42")
    smoke.run("list (툴이 보이나)", "list", contains="adder-csv")
    smoke.run("show (본문)", "show", "adder-csv", contains="script")
    smoke.run("improve --check-only (LLM 불필요)", "improve", "adder-csv",
              "--check-only", contains="unchanged")
    smoke.run("route (과제로 고르기)", "route", "두 수를 더하기", contains="adder")

    out_dir = smoke.home / "out"
    smoke.run("export (표준 패키지)", "export", "adder-csv", "--out", str(out_dir),
              contains="agentskills.io")
    ok = (out_dir / "adder-csv" / "scripts" / "tool.py").exists()
    smoke.results.append(("export 결과가 실행 가능한가", PASS if ok else FAIL,
                          "scripts/tool.py " + ("있음" if ok else "없음")))

    install_dir = smoke.home / "installed"
    smoke.run("install (다른 에이전트 자리로)", "install", "--into", str(install_dir),
              contains="설치")
    smoke.run("install 한 것이 다시 발견되나", "capabilities", contains="adder-csv")

    # import 왕복
    smoke.run("import 이 검증을 덮지 않나", "import",
              str(out_dir / "adder-csv" / "SKILL.md"), expect=1,
              contains="이미 있는 이름")
    smoke.run("import --as (다른 이름)", "import",
              str(out_dir / "adder-csv" / "SKILL.md"), "--as", "adder-copy",
              contains="믿지 않습니다")

    # 근처 MCP 서버에 **붙어서** 도구를 받아오는가. 이 경로가 죽어 있던 동안
    # 설정에 적힌 서버는 영원히 미해결이었고 route·ask 가 하나도 못 골랐다.
    # (사용자의 실제 MCP 설정이 없는 기계에서는 붙을 서버가 없으므로 건너뛴다.)
    live = smoke.run("capabilities --live (실제 접속)", "capabilities", "--live")
    # **붙은 서버가 있을 때만** 캐시를 따진다. 이 임시 홈은 PATH 를 최소로 주므로
    # npx 로 뜨는 서버는 여기서 아예 못 뜬다 - 그걸 실패로 세면 검사가 거짓말을 한다.
    if re.search(r"mcp-live:[^:\n]+: [1-9]\d*개", live):
        smoke.results.append(
            ("라이브로 받은 도구가 캐시에 남나",
             PASS if (smoke.home / "home" / "mcp-tools.json").exists() else FAIL, ""))
        smoke.run("접속 없이 그 도구가 다시 보이나", "capabilities",
                  contains="mcp-cache")
    else:
        smoke.results.append(("capabilities --live (실제 접속)", SKIP,
                              "이 기계에 붙을 MCP 서버가 없음"))

    # MCP 서버가 실제로 말하는가
    smoke.results.append(("serve (MCP tools/list · tools/call)", *_mcp_check(smoke)))

    # ── LLM 이 있어야 하는 것 ──────────────────────────────────────────────
    smoke.run("tool (LLM 이 스크립트 작성)", "tool", "adder-llm",
              "--task", "a 와 b 를 더한다", "--cases", str(cases_dir / "c.csv"),
              contains="promoted", llm=True)
    smoke.run("ask (쿼리 한 줄로 끝까지)", "ask", "두 수를 더하기 40 이랑 2",
              contains="42", llm=True)

    return smoke.report()


def _mcp_check(smoke: Smoke) -> tuple[str, str]:
    """서버를 띄워 실제 JSON-RPC 로 말을 걸어 본다."""
    process = subprocess.Popen(
        [sys.executable, "-m", "jermes.cli", "serve"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", bufsize=1, env=smoke.env, cwd=str(ROOT))
    try:
        for message in (
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
             "params": {"name": "adder-csv", "arguments": {"a": 40, "b": 2}}},
        ):
            process.stdin.write(json.dumps(message) + "\n")
        process.stdin.flush()
        replies = [json.loads(process.stdout.readline()) for _ in range(3)]
    except Exception as exc:
        return FAIL, f"{type(exc).__name__}: {exc}"[:60]
    finally:
        process.stdin.close()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
    tools = [t["name"] for t in replies[1].get("result", {}).get("tools", [])]
    answer = replies[2].get("result", {}).get("content", [{}])[0].get("text", "")
    if "adder-csv" not in tools:
        return FAIL, f"tools/list 에 없음: {tools}"
    if answer.strip() != "42":
        return FAIL, f"tools/call 이 {answer!r}"
    return PASS, f"도구 {len(tools)}개 · 호출 결과 42"


if __name__ == "__main__":
    raise SystemExit(main())
