"""한 질문을 여러 단계로 끝낸다 - 다만 선을 지키면서.

실측한 결함: `ask` 는 한 번에 한 능력만 썼다. 고르고, 입력을 뽑고, 부르고, 끝.
결과를 다시 보지 않으니 두 도구가 필요한 일은 사용자가 직접 두 번 물어야 했고,
첫 도구가 엉뚱한 것을 돌려줘도 그대로 답이 됐다. "쿼리 하나로 다 된다"가 사실은
"도구 하나로 끝나는 일에 한해서"라는 뜻이었다.

여기 있는 시험은 대부분 **안 하는 것**을 잰다. 루프는 만들기 쉽고 멈추게 하기가
어렵다.
"""

import json

import pytest

from jermes.act import Episode, Offer, Step, next_step, run_episode

OFFERS = [
    Offer("adder", "두 수를 더한다"),
    Offer("remover", "파일을 지운다", risk="dangerous", read_only=False),
]


def _adds(name, payload):
    return Step(name=name, payload=payload, ok=True,
                output=payload.get("a", 0) + payload.get("b", 0))


def _says(*answers):
    seen = []

    def complete(prompt):
        seen.append(prompt)
        return answers[min(len(seen) - 1, len(answers) - 1)]

    complete.prompts = seen
    return complete


# --- 결과를 보고 다음을 정한다 -------------------------------------------------

def test_the_result_of_one_step_reaches_the_next_decision():
    """이게 없으면 그냥 도구를 여러 번 부르는 것이지 이어서 하는 것이 아니다."""
    complete = _says(
        json.dumps({"next": "adder", "payload": {"a": 1, "b": 2}, "why": "먼저"}),
        json.dumps({"next": "adder", "payload": {"a": 3, "b": 10}, "why": "이어서"}),
        json.dumps({"done": True, "answer": "13 입니다"}))

    episode = run_episode(
        "1 더하기 2 하고 10 더해줘",
        decide=lambda ep, offers: next_step(complete, ep.query, ep, offers),
        execute=_adds, offers_for=lambda ep: OFFERS,
        approve=lambda *a: True, max_steps=4)

    assert [s.output for s in episode.steps] == [3, 13]
    assert "-> 3" in complete.prompts[1], "앞 결과가 다음 판단에 안 들어갔다"


def test_a_failure_reaches_the_next_decision_too():
    """실패야말로 다른 길을 찾아야 할 때다. 무엇을 넣었다 왜 깨졌는지가 넘어가야
    한다 - 이 물건이 학습 재료로 제일 값지게 치는 것도 같은 쌍이다."""
    seen = []

    def decide(episode, _offers):
        seen.append(episode.transcript())
        if len(seen) > 1:
            return {"done": True, "answer": "끝"}
        return {"next": "adder", "payload": {"a": 1, "b": 0}, "why": ""}

    run_episode("나눠줘", decide=decide,
                execute=lambda n, p: Step(name=n, payload=p, ok=False,
                                          error="ZeroDivisionError"),
                offers_for=lambda ep: OFFERS, approve=lambda *a: True, max_steps=3)
    assert "ZeroDivisionError" in seen[1]


# --- 없는 것을 지어내지 않는다 -------------------------------------------------

def test_a_capability_that_does_not_exist_stops_the_loop():
    """없는 도구를 부르는 시늉을 하느니 못 한다고 말하는 편이 낫다."""
    plan = next_step(_says(json.dumps({"next": "send_email", "payload": {}})),
                     "메일 보내줘", Episode(query="q"), OFFERS)
    assert plan["done"] and "send_email" in plan["answer"]


def test_a_non_json_answer_stops_instead_of_guessing():
    plan = next_step(_says("음... 잘 모르겠네요"), "q", Episode(query="q"), OFFERS)
    assert plan["done"] and "JSON" in plan["answer"]


# --- 승인 없이 하지 않는다 -----------------------------------------------------

def test_a_step_that_is_not_read_only_asks_first():
    """미리 다 받아 두는 승인은 승인이 아니다. 여러 단계의 본질이 "사람이 미리
    전부를 볼 수 없다"는 것이라, 무엇을 하려는지 보여 준 다음에 물어야 뜻이 있다."""
    asked = []
    episode = run_episode(
        "지워줘",
        decide=lambda ep, o: {"next": "remover", "payload": {"path": "/tmp/x"},
                              "why": ""},
        execute=_adds, offers_for=lambda ep: OFFERS,
        approve=lambda name, payload, offer: asked.append(name) or False,
        max_steps=3)

    assert asked == ["remover"]
    assert not episode.steps, "거절했는데 실행했다"
    assert "승인" in episode.stopped


def test_no_approver_means_no(monkeypatch):
    """물어볼 수 없는데 그냥 하는 것은 승인을 건너뛴 것이지 받은 것이 아니다."""
    episode = run_episode(
        "지워줘",
        decide=lambda ep, o: {"next": "remover", "payload": {}, "why": ""},
        execute=_adds, offers_for=lambda ep: OFFERS, approve=None, max_steps=3)
    assert not episode.steps


def test_a_read_only_step_does_not_nag():
    approvals = []
    run_episode("더해줘",
                decide=lambda ep, o: ({"next": "adder", "payload": {"a": 1, "b": 1},
                                       "why": ""} if not ep.steps
                                      else {"done": True, "answer": "2"}),
                execute=_adds, offers_for=lambda ep: OFFERS,
                approve=lambda *a: approvals.append(a) or True, max_steps=3)
    assert not approvals, "읽기전용인데 물어봤다"


# --- 반드시 끝난다 -------------------------------------------------------------

def test_the_same_call_twice_is_treated_as_spinning():
    """같은 것을 같은 입력으로 또 부르면 헛돌고 있는 것이다. 상한까지 기다리면
    그만큼 시간과 토큰을 버린다."""
    episode = run_episode(
        "무한",
        decide=lambda ep, o: {"next": "adder", "payload": {"a": 1, "b": 1}, "why": ""},
        execute=_adds, offers_for=lambda ep: OFFERS,
        approve=lambda *a: True, max_steps=20)
    assert len(episode.steps) == 1 and "반복" in episode.stopped


def test_the_step_cap_always_holds():
    counter = [0]

    def always_new(_ep, _offers):
        counter[0] += 1
        return {"next": "adder", "payload": {"a": counter[0], "b": 1}, "why": ""}

    episode = run_episode("끝없이", decide=always_new, execute=_adds,
                          offers_for=lambda ep: OFFERS,
                          approve=lambda *a: True, max_steps=3)
    assert len(episode.steps) == 3 and "상한" in episode.stopped


def test_the_budget_stops_it_before_the_first_step():
    def over():
        raise RuntimeError("토큰 상한")

    episode = run_episode(
        "예산",
        decide=lambda ep, o: {"next": "adder", "payload": {"a": 1, "b": 1}, "why": ""},
        execute=_adds, offers_for=lambda ep: OFFERS, approve=lambda *a: True,
        max_steps=9, check_budget=over)
    assert not episode.steps and "예산" in episode.stopped


def test_offers_are_asked_for_every_step():
    """앞 단계가 새 능력을 만들었을 수 있다. 목록을 한 번만 뜨면 그걸 놓친다."""
    asks = []

    def offers_for(episode):
        asks.append(len(episode.steps))
        return OFFERS

    counter = [0]

    def always_new(_ep, _offers):
        counter[0] += 1
        return {"next": "adder", "payload": {"a": counter[0], "b": 1}, "why": ""}

    run_episode("q", decide=always_new, execute=_adds, offers_for=offers_for,
                approve=lambda *a: True, max_steps=3)
    assert asks == [0, 1, 2], "단계마다 다시 안 물었다"


# --- CLI 의 승인 함수 자체 -----------------------------------------------------
# 위 시험들은 루프가 승인 함수를 **어떻게 쓰는지**를 잰다. 실제로 사람에게 묻는
# 함수는 CLI 에 있고, 그쪽은 시험도 실사용 커버리지도 없었다. 안전에 걸린 자리에서
# 검사도 안 도는 코드가 있으면 안 된다.

def test_the_approver_refuses_when_it_cannot_ask(monkeypatch, capsys):
    """물어볼 수 없는데 그냥 하는 것은 승인을 건너뛴 것이지 받은 것이 아니다.
    파이프나 크론에서 도는 경우가 정확히 이 상태다."""
    from jermes import cli

    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: False, raising=False)
    granted = cli._approve("remover", {"path": "/tmp/x"},
                           Offer("remover", "지운다", risk="dangerous",
                                 read_only=False))
    out = capsys.readouterr().out
    assert granted is False
    assert "remover" in out and "대화형이 아니" in out


@pytest.mark.parametrize("typed,granted", [("y", True), ("yes", True),
                                           ("ㅇ", True), ("", False),
                                           ("n", False), ("아니", False)])
def test_the_approver_defaults_to_no(monkeypatch, typed, granted):
    """기본이 예이면 엔터 한 번에 위험한 것이 지나간다."""
    from jermes import cli

    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True, raising=False)
    monkeypatch.setattr("builtins.input", lambda _p: typed)
    assert cli._approve("remover", {}, Offer("remover", "지운다",
                                             read_only=False)) is granted


def test_ctrl_c_at_the_prompt_is_a_no(monkeypatch):
    from jermes import cli

    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True, raising=False)

    def interrupted(_prompt):
        raise KeyboardInterrupt

    monkeypatch.setattr("builtins.input", interrupted)
    assert cli._approve("remover", {}, Offer("remover", "지운다",
                                             read_only=False)) is False


def test_yes_does_not_mean_hidden(capsys):
    """동의는 "물어보지 마"이지 "숨겨"가 아니다. 조용히 통과시키면 위험 등급이
    장식이 되고, 무엇이 일어났는지 화면에서 되짚을 수가 없다."""
    from jermes import cli

    assert cli._approve_without_asking(
        "remover", {"path": "/tmp/x"},
        Offer("remover", "지운다", risk="dangerous", read_only=False)) is True
    out = capsys.readouterr().out
    assert "remover" in out and "dangerous" in out and "/tmp/x" in out
