"""첫 화면이 빨리 뜨는가, 그리고 그 빠르기가 다시 느려지지 않는가.

실측한 결함(세션 14,320개인 실제 기계):
    status  8.07초 · doctor 9.5~11초 · sessions 5.13초
    로컬 LLM 이 없는 사람은 **16.5초**를 기다린 뒤에야 "못 찾았습니다"

시간을 직접 재는 시험은 기계마다 달라 못 믿는다. 그래서 **느려지는 원인**을 잰다:
파일마다 stat 하는가, 엔드포인트를 차례로 두드리는가, 개수만 필요한데 정렬하는가.
"""

import time
from pathlib import Path

import pytest

from jermes import cli
from jermes.sources import claude_code

# conftest 의 자동 픽스처가 `cli.discover_endpoint` 를 스텁으로 바꾼다(시험이 떠
# 있는 LLM 을 우연히 물지 않게). 여기서는 **그 함수 자체**를 재야 하므로 픽스처가
# 돌기 전인 임포트 시점에 원본을 붙잡아 둔다.
_real_discover = cli.discover_endpoint


def test_listing_sessions_does_not_stat_every_file_twice(tmp_path, monkeypatch):
    """`rglob` 뒤에 파일마다 `Path.stat()` 을 부르면 14,320번이다. `scandir` 은
    디렉터리를 읽을 때 그 정보를 같이 받아 온다. 실측 3.39초 -> 0.84초(4.0배)."""
    root = tmp_path / "projects"
    (root / "a").mkdir(parents=True)
    (root / "b").mkdir()
    for i in range(30):
        (root / ("a" if i % 2 else "b") / f"s{i}.jsonl").write_text("{}\n",
                                                                    encoding="utf-8")

    calls = []
    real = Path.stat

    def counted(self, *args, **kwargs):
        calls.append(self.name)
        return real(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", counted)
    files = claude_code.iter_session_files(root)
    monkeypatch.undo()

    assert len(files) == 30
    per_file = [c for c in calls if c.endswith(".jsonl")]
    assert not per_file, f"파일마다 stat 을 {len(per_file)}번 불렀다 - scandir 이 이미 안다"


def test_listing_is_newest_first_and_skips_empty(tmp_path):
    """빠르게 만들면서 계약이 바뀌면 안 된다."""
    root = tmp_path / "projects"
    root.mkdir()
    old = root / "old.jsonl"
    new = root / "new.jsonl"
    empty = root / "empty.jsonl"
    old.write_text("{}\n", encoding="utf-8")
    empty.write_text("", encoding="utf-8")
    time.sleep(0.02)
    new.write_text("{}\n", encoding="utf-8")

    files = claude_code.iter_session_files(root)
    assert [p.name for p in files] == ["new.jsonl", "old.jsonl"]


def test_endpoints_are_probed_at_once_not_one_by_one(monkeypatch):
    """차례로 두드리면 앞의 것들이 죽어 있을 때 그 대기시간을 다 문다. 실측:
    로컬 LLM 이 아예 없는 사람이 16.5초를 기다렸다 -> 2.13초."""
    import urllib.request

    started: list[float] = []

    def slow_dead(url, timeout=None):
        started.append(time.perf_counter())
        time.sleep(0.15)
        raise OSError("연결 거부")

    monkeypatch.setattr(cli, "LOCAL_ENDPOINTS",
                        tuple(f"http://127.0.0.1:{p}/v1" for p in range(9001, 9006)))
    monkeypatch.setattr(cli, "_remembered_endpoint", lambda: "")
    monkeypatch.setattr(urllib.request, "urlopen", slow_dead)

    began = time.perf_counter()
    assert _real_discover(timeout=1.0) == ("", "")
    spent = time.perf_counter() - began

    assert len(started) == 5, "다섯 곳을 다 봐야 한다"
    # 차례대로면 0.75초, 동시면 0.15초 언저리. 넉넉히 잡아도 절반 밑이어야 한다.
    assert spent < 0.35, f"차례로 두드리고 있다({spent:.2f}초)"


def test_endpoints_use_ipv4_not_localhost():
    """`localhost` 는 IPv6(::1) 를 먼저 푼다. 거기서 듣는 것이 없으면 그 대기시간을
    통째로 문다. 실측: 죽은 포트 하나에 localhost 4.09초 · 127.0.0.1 2.03초."""
    assert all("127.0.0.1" in base for base in cli.LOCAL_ENDPOINTS)
    assert not any("localhost" in base for base in cli.LOCAL_ENDPOINTS)


def test_the_first_choice_wins_even_when_a_later_one_answers_first(monkeypatch):
    """빨리 답한 쪽이 이기게 하면 실행할 때마다 다른 모델에 붙는다."""
    import json
    import urllib.request

    class _Response:
        def __init__(self, body):
            self._body = body

        def read(self):
            return json.dumps(self._body).encode()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def answers(url, timeout=None):
        if "9001" in url:
            time.sleep(0.12)             # 느리지만 목록에서 먼저다
            return _Response({"data": [{"id": "first"}]})
        return _Response({"data": [{"id": "second"}]})

    monkeypatch.setattr(cli, "LOCAL_ENDPOINTS",
                        ("http://127.0.0.1:9001/v1", "http://127.0.0.1:9002/v1"))
    monkeypatch.setattr(cli, "_remembered_endpoint", lambda: "")
    monkeypatch.setattr(cli, "_remember_endpoint", lambda base: None)
    monkeypatch.setattr(urllib.request, "urlopen", answers)

    base, model = _real_discover(timeout=1.0)
    assert model == "first", "목록 순서가 아니라 빠르기로 골랐다"


def test_a_remembered_endpoint_skips_the_scan(monkeypatch, tmp_path):
    """어제 붙은 곳이 오늘도 붙는 것이 보통이다. 목록 순서를 지키느라 앞의 죽은
    포트를 매번 기다릴 이유가 없다."""
    import json
    import urllib.request

    monkeypatch.setenv("JERMES_HOME", str(tmp_path))
    seen: list[str] = []

    class _Response:
        def read(self):
            return json.dumps({"data": [{"id": "m"}]}).encode()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def spy(url, timeout=None):
        seen.append(str(url))
        return _Response()

    monkeypatch.setattr(urllib.request, "urlopen", spy)
    cli._remember_endpoint("http://127.0.0.1:9999/v1")

    base, _ = _real_discover(timeout=1.0)
    assert base == "http://127.0.0.1:9999/v1"
    assert len(seen) == 1, f"기억한 곳만 봐야 하는데 {len(seen)}곳을 봤다"


def test_a_request_never_outlives_the_budget(monkeypatch):
    """예산은 **시작할지 말지**만 정하는 것이 아니라 진행 중인 일도 묶어야 한다.

    실측: 논리적 호출 하나가 3단계(기본 -> 자리 넓힘 -> 사고 끔) × 재시도 4회 ×
    120초 = 최악 24분이었고, 그동안 `--max-seconds 1200` 은 한 번도 검사되지
    않았다(호출 **전에**만 보니까). 도구 단조 두 건이 25분 벽에 걸려 판정 한 줄
    없이 잘렸다.
    """
    import urllib.request

    from jermes.drafter import Budget, openai_chat_completer

    seen = []

    class _Fake:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            import json
            return json.dumps({"choices": [{"message": {"content": "ok"},
                                            "finish_reason": "stop"}]}).encode()

    def fake_open(request, timeout=None):
        seen.append(timeout)
        return _Fake()

    monkeypatch.setattr(urllib.request, "urlopen", fake_open)
    budget = Budget(max_seconds=30)
    budget.started -= 25          # 5초 남았다
    complete = openai_chat_completer("http://x/v1", "m", timeout=120.0,
                                     remaining=lambda: budget.remaining_seconds)
    complete("안녕")
    assert seen and seen[0] <= 5.1, seen


def test_several_endpoints_actually_fail_over():
    """문서에 "쉼표로 여러 개를 주면 장애조치가 됩니다"라고 적혀 있었는데, 정작
    `failover_completer` 에 완성기 목록이 아니라 주소 목록을 넘기고 있어서
    `--base-url a,b` 가 늘 TypeError 로 죽었다."""
    from jermes.cli import build_completer, build_parser

    args = build_parser().parse_args(["learn"])
    args.base_url = "http://127.0.0.1:9/v1,http://127.0.0.1:10/v1"
    args.model = "a,b"
    assert callable(build_completer(args, None))


def test_the_room_that_worked_is_remembered(monkeypatch):
    """사고형 모델은 좁은 자리를 받으면 사고에 다 쓰고 **빈 채로** 온다. 그때마다
    넓혀 다시 묻는데, 그 사실을 안 기억하면 어려운 과제마다 호출이 늘 2배다.
    실측: 한글 조사 도구를 단조할 때 첫 호출이 120초를 쓰고 빈 채로 왔다."""
    import json
    import urllib.request

    from jermes.drafter import openai_chat_completer

    rooms = []

    class _Fake:
        def __init__(self, text):
            self.text = text

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps({"choices": [{"message": {"content": self.text},
                               "finish_reason": "length" if not self.text
                                                else "stop"}]}).encode()

    def fake_open(request, timeout=None):
        body = json.loads(request.data.decode("utf-8"))
        rooms.append(body.get("max_tokens"))
        return _Fake("" if body.get("max_tokens") == 4000 else "답")

    monkeypatch.setattr(urllib.request, "urlopen", fake_open)
    complete = openai_chat_completer("http://x/v1", "m",
                                     extra={"max_tokens": 4000})
    assert complete("첫 번째") == "답"
    assert rooms == [4000, 8000], rooms
    assert complete("두 번째") == "답"
    assert rooms == [4000, 8000, 8000], "넓혀야 했던 것을 기억해야 한다"


def test_the_meter_counts_what_actually_left_the_machine(monkeypatch):
    """논리적 호출 하나가 재시도·자리넓힘을 타면 서버에는 여러 번 나간다. 계량기가
    1 로 세면 `--max-calls` 는 상한 구실을 못 하고 화면의 "LLM 호출 N회"도 사실이
    아니다. 실측: 도구 단조에서 타임아웃 요청 4번이 나갔는데 계량기는 "1회"였다."""
    import json
    import urllib.request

    from jermes.drafter import Budget, metered, openai_chat_completer

    class _Fake:
        def __init__(self, text):
            self.text = text

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps({"choices": [{"message": {"content": self.text},
                               "finish_reason": "length" if not self.text
                                                else "stop"}]}).encode()

    def fake_open(request, timeout=None):
        body = json.loads(request.data.decode("utf-8"))
        return _Fake("" if body.get("max_tokens") == 4000 else "답")

    monkeypatch.setattr(urllib.request, "urlopen", fake_open)
    budget = Budget()
    complete = metered(openai_chat_completer("http://x/v1", "m",
                                             extra={"max_tokens": 4000}), budget)
    complete("한 번 물었다")
    assert budget.calls == 2, f"좁은 자리 1회 + 넓힌 1회 = 2인데 {budget.calls}"
