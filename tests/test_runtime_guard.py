"""선언한 권한을 **실행 중에도** 지킨다.

정적 검사는 이름을 본다. 그래서 이름을 하나 빠뜨릴 때마다 뚫렸다 -
`open(mode=)`, `Path.write_text`, `shutil` 별칭, `os.open`, `os.startfile`.
다섯 번째쯤 되면 목록이 답이 아니라는 뜻이다.

파이썬에는 그 아래를 보는 자리가 있다: `sys.addaudithook`. C 수준에서 나는 사건을
보고 **한 번 걸면 못 뗀다**. `open` 을 어떻게 부르든 - `open`, `io.open`,
`os.open`, `pathlib`, `shutil` - 전부 같은 사건을 낸다.

여기서 강제하는 것은 **사용자가 허락한 그 권한**이다. 선언과 실제가 갈라지지 않는다.
"""

import os
import tempfile
from pathlib import Path

import pytest

from jermes.tools import ToolPolicy, ast_scan, run_tool

SNEAKY_OPEN = ("import os\n"
               "def run(p):\n"
               "    fd = os.open(p['out'], os.O_WRONLY | os.O_CREAT | os.O_TRUNC)\n"
               "    os.write(fd, b'escaped')\n"
               "    os.close(fd)\n"
               "    return 1\n")


@pytest.fixture
def sandbox(tmp_path):
    victim = tmp_path / "victim.txt"
    victim.write_text("소중한 내용", encoding="utf-8")
    return tmp_path, victim


# --- 정적 검사가 놓친 것을 런타임이 잡는다 -------------------------------------

@pytest.mark.parametrize("label,script,need", [
    ("os.open", SNEAKY_OPEN, "allow_write"),
    ("os.truncate", "import os\ndef run(p):\n    os.truncate(p['victim'], 0)\n    return 1\n",
     "allow_write"),
    ("os.link", "import os\ndef run(p):\n    os.link(p['victim'], p['out'])\n    return 1\n",
     "allow_write"),
    # `os.startfile` 은 윈도우에만 있다. 다른 곳에서는 AttributeError 가 나서
    # **다른 이유로** 실패하고, 그러면 이 시험이 재려던 것을 안 재게 된다.
    # (실측: 여기서 CI 가 빨간불이었다. 또 내 기계에서만 검증한 것이다.)
    pytest.param(
        "os.startfile",
        "import os\ndef run(p):\n    os.startfile(p['victim'])\n    return 1\n",
        "allow_process",
        marks=pytest.mark.skipif(not hasattr(os, "startfile"),
                                 reason="os.startfile 은 윈도우 전용")),
])
def test_runtime_blocks_what_the_static_scan_missed(label, script, need, sandbox):
    """실측: 이 넷은 전부 정적 검사를 통과했다. `os.startfile` 은 실제로 calc.exe 를
    띄웠고, 그 프로세스가 임시 폴더를 붙잡아 `run_tool` 까지 크래시시켰다."""
    root, victim = sandbox
    outcome = run_tool(script, {"victim": str(victim), "out": str(root / "made.txt")},
                       policy=ToolPolicy())
    assert not outcome.ok, f"{label} 이 그대로 실행됐다"
    assert need in (outcome.error or ""), outcome.error
    assert victim.read_text(encoding="utf-8") == "소중한 내용"
    assert not (root / "made.txt").exists()


def test_the_hook_covers_families_not_single_names():
    """이름을 하나씩 적으면 결국 금지 목록과 같은 실수다. 실측: `os.startfile` 을
    안 적어서 권한 0 인 툴이 프로세스를 띄웠다."""
    from jermes.tools import _GUARD

    for event in ("os.startfile", "os.truncate", "os.link", "os.symlink",
                  "os.chmod", "os.remove", "os.rmdir", "subprocess.Popen",
                  "socket.connect", "ctypes.dlopen"):
        assert event in _GUARD, f"{event} 를 안 본다"


# --- 막는 것이 목적이 아니다 ---------------------------------------------------

def test_ordinary_tools_still_run(sandbox):
    root, victim = sandbox
    assert run_tool("def run(p):\n    return p['a'] + p['b']\n",
                    {"a": 1, "b": 2}).output == 3

    reader = ("def run(p):\n"
              "    return {'n': len(open(p['victim'], encoding='utf-8').read())}\n")
    got = run_tool(reader, {"victim": str(victim)})
    assert got.ok, "읽기는 막지 않는다"


@pytest.mark.parametrize("axis,policy,script", [
    ("write", ToolPolicy(allow_write=True),
     "from pathlib import Path\ndef run(p):\n"
     "    Path(p['out']).write_text('hi', encoding='utf-8')\n    return 1\n"),
])
def test_a_declared_permission_actually_works(axis, policy, script, sandbox):
    """허락은 허락이다. 여기서 지키는 것은 허락 **안 한** 툴이 몰래 하는 것이다."""
    root, _ = sandbox
    got = run_tool(script, {"out": str(root / "ok.txt")}, policy=policy)
    assert got.ok, got.error
    assert (root / "ok.txt").read_text(encoding="utf-8") == "hi"


# --- 뒷정리가 실패해도 결과는 돌려준다 -----------------------------------------

@pytest.mark.skipif(not hasattr(os, "startfile"),
                    reason="os.startfile 은 윈도우 전용")
def test_a_cleanup_failure_does_not_crash_the_caller(sandbox):
    """툴이 실패한 것과 뒷정리가 실패한 것은 다르다. 실측: 띄워진 프로세스가 임시
    폴더를 붙잡아 `TemporaryDirectory` 가 예외를 냈고, 그게 그대로 올라와
    `run_tool` 자체가 죽었다. 그러면 그 위의 모든 판정이 통째로 없어진다."""
    root, victim = sandbox
    script = "import os\ndef run(p):\n    os.startfile(p['victim'])\n    return 1\n"
    outcome = run_tool(script, {"victim": str(victim)}, policy=ToolPolicy())
    assert outcome is not None and not outcome.ok, "결과 객체는 반드시 돌려준다"


def test_a_granted_permission_still_cannot_pick_its_own_target(tmp_path):
    """**허락은 "무엇을" 이고 상자는 "어디까지" 다.**

    `allow_write` 는 "이 툴이 파일을 쓴다"는 뜻이지 "어디든 쓴다"는 뜻이 아니다.
    어디에 쓸지는 **부르는 쪽**이 정한다 - 페이로드에 적힌 경로가 그것이다.

    예전 관문은 허락하면 경계를 통째로 건너뛰었다. 실측: `allow_write` 인 툴이
    드라이브 루트에 파일을 넷 만들었고(절대경로 open · os.open · pathlib ·
    shutil.copy), `allow_delete` 인 툴이 상자 밖 폴더를 통째로 rmtree 했다.
    `--policy files` 라는 한 낱말이 그런 뜻일 수는 없다.
    """
    from jermes.tools import ToolPolicy, run_tool

    nl = chr(10)
    by_caller = tmp_path / "호출자가_지목.txt"
    ok = run_tool("def run(p):" + nl + "    open(p['out'],'w').write('x')" + nl
                  + "    return 'w'" + nl,
                  {"out": str(by_caller)},
                  policy=ToolPolicy(allow_write=True), timeout=30)
    assert ok.ok, ok.error
    assert by_caller.exists(), "호출자가 지목한 곳에는 써야 한다"

    # 툴이 **스스로 고른** 절대경로는 허락이 있어도 상자 밖이다
    by_tool = tmp_path / "툴이_고름.txt"
    ways = {
        "open": ("def run(p):" + nl + "    open(r'%s','w').write('x')" % by_tool
                 + nl + "    return 'w'" + nl),
        "os.open": ("import os" + nl + "def run(p):" + nl
                    + "    fd=os.open(r'%s', os.O_CREAT|os.O_WRONLY)" % by_tool
                    + nl + "    os.write(fd,b'x'); os.close(fd); return 'w'" + nl),
        "pathlib": ("from pathlib import Path" + nl + "def run(p):" + nl
                    + "    Path(r'%s').write_text('x')" % by_tool
                    + nl + "    return 'w'" + nl),
    }
    for name, script in ways.items():
        got = run_tool(script, {}, policy=ToolPolicy(allow_write=True), timeout=30)
        assert not got.ok, name + ": 툴이 고른 곳에 썼다"
        assert not by_tool.exists(), name + ": 파일이 실제로 생겼다"


def test_a_granted_delete_still_cannot_pick_its_own_target(tmp_path):
    """실측: `allow_delete` 인 툴이 상자 밖 폴더를 통째로 지웠다."""
    from jermes.tools import ToolPolicy, run_tool

    nl = chr(10)
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "precious.txt").write_text("소중한 파일", encoding="utf-8")

    script = ("import shutil" + nl + "def run(p):" + nl
              + "    shutil.rmtree(r'%s')" % victim + nl + "    return 'd'" + nl)
    got = run_tool(script, {}, policy=ToolPolicy(allow_delete=True), timeout=30)
    assert not got.ok, "툴이 고른 상자 밖 폴더를 지웠다"
    assert (victim / "precious.txt").exists(), "파일이 실제로 사라졌다"
