"""한국어 윈도우 콘솔에서 켜지는가.

실측한 결함: `PYTHONIOENCODING=cp949 python -m jermes.cli --help` 가 첫 줄에서
`UnicodeEncodeError: 'cp949' codec can't encode character '\\u2014'` 로 죽었다.
`status` 도 같았다. 개발 내내 모든 명령에 `PYTHONUTF8=1` 을 붙여 다녀서 못 봤다.

안 켜지는 프로그램에는 다른 어떤 장점도 의미가 없다. 그래서 이 시험은 **소스에
그 글자가 다시 들어오는 것 자체**를 막는다. 명령을 하나 더 늘릴 때마다 사람이
기억해서 피할 수는 없다.
"""

import subprocess
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src" / "jermes"


def _unencodable(text: str) -> set[str]:
    bad = set()
    for ch in set(text):
        try:
            ch.encode("cp949")
        except UnicodeEncodeError:
            bad.add(ch)
    return bad


def test_no_source_character_is_unprintable_on_a_korean_console():
    """`—`(em 대시) 288곳과 `⚠` 17곳이 있었다. 주석이든 문자열이든 가리지 않는다 -
    주석은 언젠가 문자열로 옮겨지고, 모듈 docstring 은 argparse 가 그대로 찍는다."""
    offenders = {}
    for path in sorted(SRC.rglob("*.py")):
        bad = _unencodable(path.read_text(encoding="utf-8"))
        if bad:
            offenders[path.name] = sorted(f"U+{ord(c):04X} {c!r}" for c in bad)
    assert not offenders, f"cp949 로 못 쓰는 글자: {offenders}"


def test_em_dash_never_comes_back():
    """사용자가 명시적으로 금지한 글자이기도 하다."""
    hits = [p.name for p in SRC.rglob("*.py")
            if chr(0x2014) in p.read_text(encoding="utf-8")]
    assert not hits, f"em 대시가 돌아왔다: {hits}"


@pytest.mark.parametrize("command", [["--help"], ["status"], ["demo"],
                                     ["law"], ["memory"], ["list"]])
def test_the_cli_survives_a_cp949_console(command, tmp_path):
    """실제로 cp949 로 내보내 본다. 글자 검사만으로는 인코딩 설정이 빠진 것을
    못 잡는다 - 예상 못 한 글자가 하나 섞여도 죽지 않아야 한다."""
    root = Path(__file__).resolve().parent.parent
    env = {
        "PATH": __import__("os").environ.get("PATH", ""),
        "SYSTEMROOT": __import__("os").environ.get("SYSTEMROOT", ""),
        "PYTHONPATH": str(root / "src"),
        "PYTHONIOENCODING": "cp949",
        "JERMES_HOME": str(tmp_path / "home"),
        "JERMES_SOURCES": "own",          # 사용자의 진짜 세션을 읽지 않는다
    }
    done = subprocess.run([sys.executable, "-m", "jermes.cli", *command],
                          capture_output=True, env=env, timeout=120)
    err = done.stderr.decode("cp949", errors="replace")
    assert "UnicodeEncodeError" not in err, err[-400:]
