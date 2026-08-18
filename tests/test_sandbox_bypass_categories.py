"""탈출 경로를 **범주별로** 훑는다. `test_runtime_guard.py` 가 이미 확인한 것
(절대경로 open · os.open · pathlib · shutil.copy · shutil.rmtree)은 다시
안 적는다 - 여기는 그 뒤에 실제로 물어본, Windows 에서만 나오는 결들이다.

각 범주마다 **결과와 이유를 같이 남긴다** - "막혔다"만 적으면 다음 사람이 같은
질문을 또 물어야 한다. 실측(2026-08-17):

    경로 표기 우회      드라이브상대 "C:x.txt" · UNC `\\\\?\\` 접두 · rename/replace
    API 우회           shutil.move · copyfile · copytree(개별 함수 - copy 하나만
                       시험하면 나머지가 안 걸린 채로 남는다)
    링크 우회          symlink(가리키는 곳이 밖이면 통째로 거절 - 안에 만드는
                       것 자체를 막는다, 나중에 그 링크를 열 때 잡는 게 아니다)
    라이브러리 부작용   tempfile 모듈 - 기본 후보 여럿을 순서대로 확률하는데
                       그 확률 과정 자체가 만들고-지우는 자기 시험이라
                       allow_write 만으로는 안 되고 allow_delete 도 있어야
                       상자 **안**에서도 성공한다(상자 밖은 여전히 막힌다)
"""

import os
import shutil
import tempfile

import pytest

from jermes.tools import ToolPolicy, run_tool


@pytest.fixture
def victim(tmp_path):
    outside = tmp_path / "victim.txt"
    outside.write_text("소중한 내용", encoding="utf-8")
    return outside


# --- 경로 표기 우회 ---------------------------------------------------------

def test_drive_relative_path_does_not_escape(tmp_path):
    """`"C:x.txt"` 처럼 드라이브 문자만 있고 역슬래시가 없는 표기는 그 드라이브의
    "현재 디렉터리" 에 상대적이다. 상자(임시 폴더)가 놓인 드라이브를 가리키면
    **상자 안으로** 접히고(그 드라이브의 현재 디렉터리가 곧 상자니까), 상자가
    없는 드라이브를 가리키면 커널이 그 드라이브 루트로 떨어뜨리는데 그건
    `_inside()` 가 상자 밖으로 본다 - 실측으로 후자를 직접 확인했다(아래
    `test_drive_relative_path_to_a_different_drive_is_refused`)."""
    drive = os.path.splitdrive(str(tmp_path))[0]  # 예: "C:"
    script = (f"def run(p):\n"
             f"    open('{drive}probe.txt','w').write('x')\n"
             f"    return 1\n")
    got = run_tool(script, {}, policy=ToolPolicy(allow_write=True), timeout=15)
    assert got.ok, got.error
    # 상자 루트가 아니라 **드라이브 루트**에는 아무것도 안 생겼다 -
    # "성공했다"가 "상자 안에 접혔다"와 같은 뜻인지는 이걸로만 안다.
    assert not os.path.exists(f"{drive}\\probe.txt")


def test_drive_relative_path_to_a_different_drive_is_refused(tmp_path):
    """상자가 놓이지 않은 드라이브를 드라이브-상대로 가리키면 거절된다."""
    here = os.path.splitdrive(str(tmp_path))[0]
    other = "D:" if here.upper() != "D:" else "E:"
    if not os.path.exists(other + "\\"):
        pytest.skip(f"이 기계에 {other} 드라이브가 없다")
    script = f"def run(p):\n    open('{other}probe2.txt','w').write('x')\n    return 1\n"
    got = run_tool(script, {}, policy=ToolPolicy(allow_write=True), timeout=15)
    assert not got.ok, "다른 드라이브 루트에 쓰는 것을 허락했다"


def test_unc_long_path_prefix_does_not_escape(tmp_path, victim):
    r"""`\\?\` 접두는 일부 윈도우 API 에서 정규화를 건너뛰게 만드는 특수
    표기다. 같은 절대경로를 이 접두로 감싸도 여전히 막혀야 한다."""
    script = (f"def run(p):\n"
             f"    open(r'\\\\?\\{victim}','w').write('x')\n"
             f"    return 1\n")
    got = run_tool(script, {}, policy=ToolPolicy(allow_write=True), timeout=15)
    assert not got.ok
    assert victim.read_text(encoding="utf-8") == "소중한 내용"


def test_rename_to_outside_is_refused(victim):
    script = ("import os\n"
             "def run(p):\n"
             "    open('in.txt','w').write('x')\n"
             f"    os.rename('in.txt', r'{victim.parent}/renamed.txt')\n"
             "    return 1\n")
    got = run_tool(script, {}, policy=ToolPolicy(allow_write=True), timeout=15)
    assert not got.ok
    assert not (victim.parent / "renamed.txt").exists()


# --- API 우회 (shutil 은 함수마다 따로 걸린다 - copy 하나만 시험하면 모자란다) --

def test_shutil_move_to_outside_is_refused(tmp_path, victim):
    script = ("import shutil\n"
             "def run(p):\n"
             "    open('in.txt','w').write('x')\n"
             f"    shutil.move('in.txt', r'{tmp_path}/moved.txt')\n"
             "    return 1\n")
    got = run_tool(script, {}, policy=ToolPolicy(allow_write=True, allow_delete=True),
                   timeout=15)
    assert not got.ok
    assert not (tmp_path / "moved.txt").exists()


def test_shutil_copyfile_to_outside_is_refused(tmp_path):
    script = ("import shutil\n"
             "def run(p):\n"
             "    open('in.txt','w').write('x')\n"
             f"    shutil.copyfile('in.txt', r'{tmp_path}/copied.txt')\n"
             "    return 1\n")
    got = run_tool(script, {}, policy=ToolPolicy(allow_write=True), timeout=15)
    assert not got.ok
    assert not (tmp_path / "copied.txt").exists()


def test_shutil_copytree_to_outside_is_refused(tmp_path):
    script = ("import shutil, os\n"
             "def run(p):\n"
             "    os.mkdir('srcdir'); open('srcdir/a.txt','w').write('x')\n"
             f"    shutil.copytree('srcdir', r'{tmp_path}/copied_dir')\n"
             "    return 1\n")
    got = run_tool(script, {}, policy=ToolPolicy(allow_write=True), timeout=15)
    assert not got.ok
    assert not (tmp_path / "copied_dir").exists()


# --- 링크 우회 --------------------------------------------------------------

def test_symlink_pointing_outside_is_refused_at_creation(victim):
    """가리키는 곳이 상자 밖이면 링크 **자체를 상자 안에 만드는 것도** 거절한다.
    나중에 누가 그 링크를 열 때 잡는 게 아니라 만드는 순간 잡는다 - 더 이르고
    더 안전한 자리다."""
    script = (f"import os\n"
             f"def run(p):\n"
             f"    os.symlink(r'{victim}', 'link.txt')\n"
             f"    return 'created'\n")
    got = run_tool(script, {}, policy=ToolPolicy(allow_write=True), timeout=15)
    assert not got.ok, "밖을 가리키는 링크를 상자 안에 만드는 것을 허락했다"


# --- 라이브러리 부작용 ------------------------------------------------------

def test_tempfile_module_lands_inside_the_box_when_fully_permitted():
    """`tempfile.NamedTemporaryFile()` 은 인자 없이 부르면 후보 여러 개를 순서
    대로 확률한다(홈 폴더 · 시스템 임시폴더 · 상자, 이 순서). **확률 자체가
    만들고 지우는 자기 시험**이라 `allow_write` 만으로는 부족하다 - 마지막
    후보(상자)에서도 그 확률의 지우기 단계가 걸려 `FileNotFoundError` 로
    보인다(권한 문제가 아니라 우리 관문이 지우기를 막아서 난 오류다).
    `allow_delete` 를 같이 주면 그 확률까지 통과하고 상자 안에 실제로 생긴다.
    """
    script = ("import tempfile\n"
             "def run(p):\n"
             "    f = tempfile.NamedTemporaryFile(delete=False)\n"
             "    f.write(b'x'); f.close()\n"
             "    return f.name\n")

    write_only = run_tool(script, {}, policy=ToolPolicy(allow_write=True), timeout=15)
    assert not write_only.ok, "delete 없이도 통과했다면 확률 순서가 바뀐 것"

    both = run_tool(script, {},
                    policy=ToolPolicy(allow_write=True, allow_delete=True), timeout=15)
    assert both.ok, both.error
    # 밖의 진짜 임시폴더가 아니라 **상자 안**에 생겼다
    assert "jermes-tool-" in both.output
