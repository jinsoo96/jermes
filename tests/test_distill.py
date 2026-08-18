"""세션에서 사실을 뽑는 자리.

실측한 결함: 실세션(도구 2,007건)에서 `learn` 이 "사실 증류 0건 - 모델이 빈 배열"
을 냈다. 같은 세션·같은 프롬프트로 직접 부르면 5건이 나온다. 파고들어 보니 결함이
넷 겹쳐 있었고, 셋은 **틀린 진단**이었다. 틀린 이유는 없는 이유보다 나쁘다 -
사람이 엉뚱한 데를 본다.
"""

import io
import urllib.error

import pytest

from jermes.drafter import (Budget, _fact_text, _worth_retrying, distill_facts,
                            metered)
from jermes.model import RunTrace


def _trace():
    return RunTrace(run_id="x", scope="user", scope_key="", events=[],
                    lessons=[], refined_memory="", judge_score=None, success=True)


def _says(answer):
    def complete(_prompt):
        return answer
    return complete


# --- 그릇을 가리지 않는다 -----------------------------------------------------

def test_a_bare_string_is_a_fact_too():
    """프롬프트는 `{"text": ...}` 를 요구하지만 사고를 끄면 모델은 문자열 배열을
    낸다. 실측: 그렇게 온 쓸 만한 사실 9건이 통째로 버려졌다. 모델이 틀린 게 아니라
    같은 내용을 다른 그릇에 담아 온 것뿐인데 우리가 못 받았다."""
    facts = distill_facts(
        _says('["Bash 는 실패 후 재시도로 성공할 수 있다.", '
              '"TodoWrite 는 JSON 파싱 오류로 실패할 수 있다."]'),
        _trace())
    assert len(facts) == 2
    assert distill_facts.last_error == ""


@pytest.mark.parametrize("item,expected", [
    ("문장 그대로 온 사실이다", "문장 그대로 온 사실이다"),
    ({"text": "정석 모양"}, "정석 모양"),
    ({"fact": "다른 열쇠"}, "다른 열쇠"),
    ({"content": "또 다른 열쇠"}, "또 다른 열쇠"),
    ({"무관한열쇠": "안 받는다"}, ""),
    (42, ""),
])
def test_fact_text_reads_the_common_shapes(item, expected):
    assert _fact_text(item) == expected


# --- 0 건이면 왜 0 건인지 정확히 -----------------------------------------------

def test_an_empty_array_says_what_actually_arrived():
    """"빈 배열"만으로는 모델이 정말 빈 배열을 냈는지, 배열이 아닌 것을 냈는지
    구분이 안 된다. 사람이 볼 곳이 달라진다."""
    distill_facts(_says("죄송합니다, 뽑을 만한 사실이 없습니다."), _trace())
    assert "받은 것" in distill_facts.last_error
    assert "죄송합니다" in distill_facts.last_error


def test_the_length_reason_is_only_used_when_length_was_the_reason():
    """실측: 모양 때문에 버려 놓고 "길이 조건에 걸려 전부 버림"이라고 했다."""
    distill_facts(_says('["짧다", "응"]'), _trace())
    assert "너무 짧거나 빔 2" in distill_facts.last_error


def test_truncation_is_context_not_a_verdict():
    """실측으로 두 번 틀렸다. 두 번째는 첫 시도가 잘렸다고 "답이 잘렸습니다"라고
    한 것 - 그때 재질의는 **성공했고** 문제는 그 답이 배열이 아니었다."""
    complete = _says("[]")
    complete.last_truncated = True
    distill_facts(complete, _trace())
    assert "받은 것" in distill_facts.last_error, "실제로 받은 것이 먼저 나와야 한다"
    assert "잘렸" in distill_facts.last_error, "잘린 것은 배경으로 곁들인다"


# --- 감싸도 진단이 안 사라진다 -------------------------------------------------

def test_wrapping_does_not_swallow_the_diagnosis():
    """완성기가 남기는 진단은 **안쪽 함수**에 붙는다. 감싼 뒤에 부르는 쪽은 바깥을
    들고 있어 안 보였다. 실측: `last_truncated` 가 `learn` 에서 늘 False 로 읽혀,
    잘려서 0 건이 된 경우에도 틀린 이유가 나갔다."""
    inner = _says("답")
    inner.last_truncated = True
    inner.last_usage = {"prompt_tokens": 10, "completion_tokens": 5}

    wrapped = metered(inner, Budget())
    wrapped("무엇이든")
    assert getattr(wrapped, "last_truncated") is True
    assert getattr(wrapped, "last_usage")["prompt_tokens"] == 10


# --- 잘리면 자리를 넓혀 본다 ---------------------------------------------------

def test_a_truncated_answer_gets_more_room_before_losing_its_thinking(monkeypatch):
    """사고를 켠 이유가 품질이었다(실측: 사고끔 초안 2·사실 6 -> 사고켬 초안 3·
    사실 10). 잘릴 때마다 그 품질을 포기하면 길고 복잡한 세션일수록 나쁜 답을
    받는다 - 배울 게 제일 많은 쪽이다.

    실측: 사실 증류가 사고에 3,787 토큰을 썼다. 상한 4,000 의 95% 다. 모자란 것은
    생각이 아니라 자리였다."""
    import json

    from jermes.drafter import openai_chat_completer

    sent = []

    class _Response:
        def __init__(self, body):
            self._body = body

        def read(self):
            return json.dumps(self._body).encode()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(request, timeout=None):
        body = json.loads(request.data.decode())
        sent.append(body)
        if len(sent) == 1:          # 사고에 다 써서 잘린다
            return _Response({"choices": [{"message": {"content": ""},
                                           "finish_reason": "length"}]})
        return _Response({"choices": [{"message": {"content": "드디어 답"},
                                       "finish_reason": "stop"}]})

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    complete = openai_chat_completer("http://x/v1", "m", extra={"max_tokens": 4000})
    assert complete("질문") == "드디어 답"

    assert sent[1]["max_tokens"] == 8000, "자리를 안 넓혔다"
    assert "chat_template_kwargs" not in sent[1], "사고부터 껐다"


def test_thinking_is_only_dropped_as_a_last_resort(monkeypatch):
    """넓혀도 비면 그때 끈다. 답이 나쁜 것이 답이 없는 것보다는 낫다."""
    import json

    from jermes.drafter import openai_chat_completer

    sent = []

    class _Response:
        def __init__(self, body):
            self._body = body

        def read(self):
            return json.dumps(self._body).encode()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(request, timeout=None):
        sent.append(json.loads(request.data.decode()))
        if len(sent) < 3:
            return _Response({"choices": [{"message": {"content": ""},
                                           "finish_reason": "length"}]})
        return _Response({"choices": [{"message": {"content": "[]"},
                                       "finish_reason": "stop"}]})

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    complete = openai_chat_completer("http://x/v1", "m", extra={"max_tokens": 4000})
    complete("질문")

    assert len(sent) == 3
    assert sent[2]["chat_template_kwargs"] == {"enable_thinking": False}
