"""자원을 태워 기계를 멈추게 하는 것을 막는다.

지금까지의 방어는 전부 **파이썬 안**이었다(정적 검사, 감사 훅). 그래서 파이썬
문법을 어기지 않으면서 자원을 태우는 것은 하나도 못 막았다.

    메모리 폭탄 (1GB 할당)   통과 · 0.4초
    거대 파일 쓰기 (200MB)   통과 · 0.2초

시간 제한만 있었는데 메모리는 시간이 아니다. 사람이 자리를 비운 사이 도는
물건에서 이건 기계를 멈추게 한다.

"진짜 격리는 컨테이너"라고 미뤄 뒀지만 그건 절반만 맞다. 컨테이너 없이도 커널에
직접 거는 한계가 있고 표준 라이브러리로 닿는다: POSIX 는 `resource.setrlimit`,
윈도우는 Job Object. 둘 다 파이썬이 아니라 커널이 집행한다.
"""

import os
import shutil
import tempfile

import pytest

from jermes.tools import ToolPolicy, run_tool

PURE = "def run(p):\n    return p['a'] + p['b']\n"
GRAB = "def run(p):\n    return len(bytearray(1024 * 1024 * 1024))\n"
FLOOD = ("def run(p):\n"
         "    with open('big.bin', 'wb') as f:\n"
         "        for _ in range(400):\n"
         "            f.write(b'x' * (1024 * 1024))\n"
         "            f.flush()\n"
         "    return 'ok'\n")


def test_a_memory_bomb_is_stopped():
    """실측: 1GB 를 0.4초에 잡았다. 시간 제한은 이걸 못 막는다 - 메모리는 시간이
    아니다."""
    outcome = run_tool(GRAB, {}, policy=ToolPolicy(max_memory_mb=256), timeout=30)
    assert not outcome.ok, "1GB 할당이 그대로 통과했다"
    assert "max_memory_mb" in (outcome.error or ""), outcome.error


def test_memory_under_the_cap_is_fine():
    """막는 것이 목적이 아니다. 상한 안에서는 그대로 돌아야 한다."""
    got = run_tool("def run(p):\n    return len(bytearray(32 * 1024 * 1024))\n",
                   {}, policy=ToolPolicy(max_memory_mb=512), timeout=30)
    assert got.ok and got.output == 32 * 1024 * 1024


def test_a_disk_flood_is_stopped_with_a_reason():
    """실측: 200MB 를 0.4초에 썼다. 기본 제한시간 10초면 5GB 다.

    그리고 죽였으면 **왜** 죽였는지 말해야 한다. 프로세스를 죽이면 stderr 가
    비어서 종료코드만 남고, 사용자는 코드를 아무리 고쳐도 이유를 모른다
    (실측으로 화면에 사유가 아예 없었다)."""
    outcome = run_tool(FLOOD, {},
                       policy=ToolPolicy(allow_write=True, max_output_mb=8),
                       timeout=60)
    assert not outcome.ok, "400MB 쓰기가 상한 8MB 를 넘겼는데 통과했다"
    # 막는 방식은 플랫폼마다 다르다(POSIX 는 커널 RLIMIT_FSIZE, 윈도우는 감시자).
    # 그러나 **안내는 같아야 한다** - 같은 실패에 대해 다른 것을 배우면 안 된다.
    assert "max_output_mb" in (outcome.error or ""), outcome.error


def test_a_small_write_is_not_touched():
    got = run_tool("def run(p):\n    open('s.bin','wb').write(b'x' * (1024 * 1024))\n"
                   "    return 'ok'\n",
                   {}, policy=ToolPolicy(allow_write=True, max_output_mb=8),
                   timeout=30)
    assert got.ok, got.error


def test_ordinary_tools_are_unaffected():
    assert run_tool(PURE, {"a": 1, "b": 2}).output == 3


def test_the_limits_are_declared_not_hidden():
    """상한을 코드에 박아 두면 필요한 사람이 못 바꾼다. 정책의 일부여야 한다."""
    policy = ToolPolicy()
    assert policy.max_memory_mb > 0 and policy.max_output_mb > 0
    assert ToolPolicy(max_memory_mb=64).max_memory_mb == 64

    # 패키지에 실려 나가는 정책에도 들어가야 받는 쪽이 같은 한계로 돌린다.
    assert "max_memory_mb" in policy.to_dict()
    assert ToolPolicy.from_dict(policy.to_dict()).max_memory_mb == policy.max_memory_mb


@pytest.mark.skipif(os.name == "posix", reason="윈도우 Job Object 경로")
def test_windows_uses_a_job_object():
    """Job Object 는 만들기만 해서는 효력이 없다. 프로세스를 붙여야 한다 -
    그래서 `subprocess.run` 이 아니라 `Popen` 을 쓴다."""
    from jermes.tools import _windows_job

    job = _windows_job(memory_mb=128, allow_process=False)
    assert job is not None, "Job Object 를 못 만들었다"
    kernel, handle = job
    kernel.CloseHandle(handle)


@pytest.mark.skipif(os.name != "posix", reason="POSIX rlimit 경로")
def test_posix_uses_kernel_rlimits():
    """`preexec_fn` 은 자식이 시작되기 **전에** 돈다. 붙이기 전 틈이 없다."""
    from jermes.tools import _posix_limits

    apply = _posix_limits(memory_mb=128, file_mb=8, seconds=5)
    assert callable(apply)


def test_a_burst_that_finishes_between_polls_is_still_caught():
    """감시자는 0.25초마다 잰다. 그 사이에 끝나는 폭주는 통째로 놓친다 - 실측으로
    400MB 쓰기가 0.4초에 끝났고, 폴링이 한 번도 안 물린 판에서는 도구가 그대로
    성공했다(사유는 적히는데 종료코드가 0 이었다).

    물어야 할 것은 "쓰는 중에 잡았는가"가 아니라 **"이 도구가 상한을 넘겼는가"**
    이고, 그건 끝난 뒤에도 알 수 있다 - 쓴 것이 그대로 남아 있으니까.
    """
    # **못 잴 상황이면 못 잰다고 말한다.** 이 시험은 32MB 를 실제로 써서
    # 상한(8MB)을 넘겨야 뜻이 있다. 디스크가 정말 없으면 툴은 우리 관문이
    # 아니라 `OSError: No space left on device` 로 죽고, 그러면 "상한을 못
    # 잡았다" 와 "쓸 자리가 없었다" 가 같은 빨간불로 보인다. 실측으로 그
    # 상황을 겪었다(C: 여유 2.5MB) - 코드 회귀로 오해하기 딱 좋았다.
    free = shutil.disk_usage(tempfile.gettempdir()).free
    if free < 64 * 1024 * 1024:
        pytest.skip(f"디스크 여유 {free // 1024 // 1024}MB - 32MB 폭주를 "
                    "일으킬 수 없어 이 시험은 무의미하다")

    quick = ("def run(p):\n"
             "    open('big.bin', 'wb').write(b'x' * (32 * 1024 * 1024))\n"
             "    return 'ok'\n")
    got = run_tool(quick, {}, policy=ToolPolicy(allow_write=True,
                                                max_output_mb=8), timeout=60)
    assert not got.ok, "상한 8MB 인데 32MB 를 쓰고 통과했다"
    assert "max_output_mb" in (got.error or ""), got.error
