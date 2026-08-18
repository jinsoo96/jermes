

def test_correction_hints_can_be_extended_for_other_languages(monkeypatch):
    """이 목록은 우리말과 영어뿐이다. 다른 말로 일하는 사람은 교정이 **조용히**
    0건이 된다 - 토크나이저에서 이미 한 번 겪은 실패다.

    목록으로 두는 것 자체는 재 보고 정했다(실세션 12개): 낱말 규칙 193건 대
    구조 규칙("도구 쓰던 중 끼어듦") 53건이고, 구조 규칙이 잡은 것의 대부분은
    교정이 아니라 시스템 알림이었다. 그러면 목록의 주인이라도 사용자여야 한다.
    """
    from jermes.sources.claude_code import _looks_like_correction

    assert not _looks_like_correction("それは違います")
    monkeypatch.setenv("JERMES_CORRECTION_HINTS", "違います, ちがう")
    assert _looks_like_correction("それは違います")
