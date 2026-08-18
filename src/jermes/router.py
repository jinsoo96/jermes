"""SF11 - 과제에 맞는 능력을 **미리 골라서** 건넨다.

**왜 이게 필요한가 (실측 근거).**
- 도구가 10~15개를 넘으면 모델 정확도가 눈에 띄게 떨어지고, 49~741개 구간에서는
  7~85% 하락이 보고됐다. 전부 보여주는 건 확장되지 않는다.
- 검색으로 걸러서 주면 도구 선택 정확도가 13%→43% 로 올랐고 토큰은 절반이 됐다.
- 그런데 **약한 모델은 검색 도구를 스스로 부르지 않는다.** 플랫폼 실측 기록:
  로컬 Qwen3.6 은 `search_tools` 를 자율 호출하지 않는다(PD 가 비강제라서).
  즉 "모델이 필요하면 찾아 쓰겠지"는 강한 모델에서만 성립하는 가정이다.

그래서 여기서는 **모델에게 검색을 시키지 않는다.** 과제 문장을 받아 순위를 매기고
상위 몇 개만 컨텍스트에 넣는다. 모델이 아무것도 안 해도 도구가 손에 쥐여 있다.

**남들과 다른 점 - 유사도만으로 고르지 않는다.**
같은 말을 하는 능력이 둘이면, **확인된 쪽**을 올린다. 검증은 우리만 가진 신호다
(Hermes 는 효능 검증이 없고, agentskills 표준의 검사도 문법까지다). 유사도는 "말이
비슷하다"이고 검증은 "실제로 됐다"이다. 뒤엣것이 더 강한 근거다.

의존성 없음 - 임베딩 서버가 없어도 돌아야 첫 실행에서 결과까지 간다.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

from .discovery import Capability, render_all
from .model import verified_mark

# 정보가 거의 없는 말들. 이것만 겹쳐서 1등이 되면 안 된다.
# **뜻을 안 나르는 말은 한 자리에 적는다.** 예전에는 이 목록이 두 벌이었다 -
# 여기와 `memory._STOP`. 같은 것을 두 곳에서 손으로 관리하면 한쪽에만 낱말이
# 늘고, 어느 쪽이 맞는지는 아무도 모른다. 라우팅과 모순검출은 쓰는 데가 다르지만
# "이 낱말은 주제를 안 나른다" 는 판단은 같아야 한다.
STOPWORDS = {
    "the", "a", "an", "and", "or", "of", "to", "for", "in", "on", "with", "is", "are",
    "하다", "한다", "해줘", "해라", "하기", "것", "수", "때", "및", "그리고", "또는",
    "좀", "제발", "다시", "지금",
}
# 낱말. 예전에는 `[0-9A-Za-z_]+|[가-힣]+` 라 그 밖의 글자가 통째로 탈락했다 -
# 일본어·중국어·러시아어·아랍어로 물으면 토큰이 **0개**가 되어 오류도 없이 영영
# "맞는 능력이 없습니다" 였다. 못 하는 언어가 있는 것과 **조용히** 못 하는 것은
# 다르다. `\w+` 는 유니코드를 다 본다.
_WORD = re.compile(r"\w+", re.UNICODE)
# 띄어쓰기 없이 붙여 쓰는 글자(한자·가나). 한국어에 쓰는 것과 같은 이유로
# bigram 을 낸다 - 낱말 경계를 모르면 통째로 한 토큰이 되어 아무것도 안 걸린다.
_SCRIPTIO_CONTINUA = re.compile(
    "[" + "\u3040-\u30ff"      # 가나
        + "\u3400-\u4dbf"      # 한자 확장A
        + "\u4e00-\u9fff"      # 한자
        + "\uf900-\ufaff" + "]")
_HANGUL = re.compile(r"^[가-힣]+$")
# 한 음절짜리 조사·어미. 이것들로 첫 음절을 내면 뜻이 아니라 문법이 걸린다.
# 한 음절짜리 조사·어미. `_KO_POSTPOSITIONS` 로 판정을 옮기면서 한동안
# 아무도 안 읽는 채로 남아 있었다("있는데 안 쓴다"). 지금은 **한 글자 낱말이
# 통째로 조사인지** 보는 데 쓴다 - 그런 낱말은 뜻이 아니라 문법이라 뺀다.
_KO_PARTICLES = frozenset("를을이가은는에의로와과도만요다고서게지한해하되며나야")
# 두 음절 이상인 조사. 이것들만 어간 머리를 내지 않는다 - `으로` 의 `으` 는
# 어간이 아니다. 한 글자 조사는 위 집합이 맡는다.
_KO_POSTPOSITIONS = frozenset({
    "으로", "에서", "부터", "까지", "에게", "한테", "께서", "보다", "처럼",
    "만큼", "라도", "이나", "이란", "이라", "에는", "에도", "으로서", "으로써",
})
# 식별자를 조각으로. `extract_video_frames` 가 통째로 한 토큰이면 "video" 로 못 걸린다
# (실측: 실제 MCP 도구 이름이 전부 snake_case 라 이름이 검색에 거의 기여하지 못했다).
_PARTS = re.compile(r"[A-Za-z][a-z0-9]*|[A-Z]+(?![a-z])|[0-9]+")

# 위험도별 기본 취급. 정책이 덮어쓸 수 있다.
DEFAULT_ALLOWED_RISK = ("safe", "caution")

# BM25 포화 계수. 같은 낱말이 예시에 여러 번 나와도 이만큼에서 포화한다.
_BM25_K1 = 1.2


def tokenize(text: str) -> list[str]:
    """한국어를 위해 **음절 bigram 도 함께** 낸다.

    한국어는 조사가 붙어 `배포하기`/`배포를`/`배포는` 이 전부 다른 토큰이 된다.
    단어 단위로만 맞추면 사람이 쓴 과제 문장과 능력 설명이 거의 안 겹친다.
    음절 bigram 을 함께 내면 `배포` 부분이 살아남는다(형태소 분석기 없이).
    """
    tokens: list[str] = []
    for match in _WORD.findall((text or "").lower()):
        if match in STOPWORDS:
            continue
        # 한 글자짜리 조사·어미는 뜻이 아니라 문법이다. 예전에는 이 목록이
        # 아무 데서도 안 읽히는 채로 남아 있었다("있는데 안 쓴다").
        if len(match) == 1 and match in _KO_PARTICLES:
            continue
        tokens.append(match)
        if _SCRIPTIO_CONTINUA.search(match) and not _HANGUL.match(match):
            # 한자·가나: 띄어쓰기가 없어 낱말 경계를 모른다. 통째로 두면 문장
            # 하나가 토큰 하나가 되어 아무것도 안 걸리므로 bigram 으로 쪼갠다.
            # 중국어 "이 파일을 지워라" 다섯 자에서 '지우다'와 '파일'에 해당하는
            # 두 글자 짝이 살아남는다. 한국어에 쓰는 방식과 같다.
            if len(match) > 1:
                tokens.extend(match[i:i + 2] for i in range(len(match) - 1))
            continue
        if _HANGUL.match(match):
            # 한국어는 조사가 붙어 `배포하기`/`배포를` 이 다른 토큰이 된다.
            if len(match) > 1:
                tokens.extend(match[i:i + 2] for i in range(len(match) - 1))
            # 어미가 바뀌면 bigram 이 통째로 어긋난다: `더한다`(더한·한다) 와
            # `더해줘`(더해·해줘) 는 겹치는 bigram 이 하나도 없다. 실측: "40 이랑 2
            # 더해줘" 가 "a 와 b 를 더한다" 에 대해 0.000 이었다 - 사람이 쓸 법한
            # 말인데 못 찾는다.
            #
            # 그래서 음절 하나도 낸다. 다만 **첫 음절만**이다. 한국어는 어간이
            # 앞에 오고 변하는 것은 뒤라, 첫 음절이 어간의 머리다. 모든 음절을
            # 내면 조사·어미 음절까지 물어서 `를` 하나로 1등이 되는 일이 생긴다
            # (실측: 그렇게 만들었더니 무관한 도구가 "겹침 를" 로 올라왔다).
            #
            # 조사인지는 **낱말 전체**로 본다. 예전에는 첫 음절이 조사 목록에
            # 있는지를 봤는데, 조사는 접미사라 낱말의 머리로 오지 않는다. 그
            # 조건 때문에 첫 음절이 우연히 조사와 같은 글자인 동사가 전부 어간을
            # 잃었다 - 나누기·나눠줘·도와줘·가져와·만들어줘·지워줘·로그인해줘.
            # 실측: "6 을 3 으로 나눠줘" 가 "두 수를 나눈다" 를 못 찾았다. 이
            # 토크나이저가 고치려던 바로 그 결함이 절반의 동사에 남아 있었다.
            #
            # 한 글자는 머리를 따로 내지 않는다. 머리가 곧 낱말이라 같은 것을 두
            # 번 넣게 되고, 한 글자짜리만 이유 없이 두 배 무거워진다.
            if len(match) > 1 and match not in _KO_POSTPOSITIONS:
                tokens.append(match[0])
        elif "_" in match or len(match) > 3:
            # 식별자는 조각으로도 낸다. 통째로만 두면 `extract_video_frames` 가
            # "video" 와 안 겹친다 - 실제 MCP 도구 이름은 거의 다 이 모양이다.
            parts = [p for p in _PARTS.findall(match) if len(p) > 1 and p not in STOPWORDS]
            tokens.extend(p for p in parts if p != match)
    return tokens


@dataclass
class Choice:
    capability: Capability
    score: float
    reasons: list[str] = field(default_factory=list)
    # **과제의 몇 할이 설명됐는가**(0~1). 점수만으로는 근거가 두꺼운지 알 수 없다 -
    # IDF 합을 길이로 눅인 값이라 절대 크기에 뜻이 없기 때문이다. 실측에서 "날짜"
    # 하나만 겹친 영업일 툴이 0.98 로 나와 꽉 찬 막대를 받았다.
    #
    # 원말 개수로 세려다 되돌렸다: 한국어는 조사 때문에 `정규화` 와 `정규화한다` 가
    # 다른 토큰이라, 사실상 같은 말을 해도 원말이 거의 안 겹친다. 덮은 비율은
    # 음절 조각까지 세므로 언어에 덜 휘둘린다.
    coverage: float = 0.0

    # 화면에 경고를 띄우는 문턱. **판정이 아니라 표시**다 - 이 값을 조금 잘못 잡아도
    # 경고가 뜨거나 안 뜰 뿐, 무엇이 골라지는지는 안 바뀐다. 그래서 딱 떨어지는
    # 근거 없이 "과제의 3분의 1도 못 덮으면 얇다"로 둔다.
    THIN_COVERAGE = 0.35

    @property
    def thin(self) -> bool:
        """근거가 얇은가. 얇은 것을 자신 있게 내놓으면 사람이 속는다."""
        return self.coverage < self.THIN_COVERAGE

    def line(self) -> str:
        label = self.capability.label()
        tail = f" [{label}]" if label else ""
        thin = f" · 근거 얇음({self.coverage:.0%})" if self.thin else ""
        return (f"{self.capability.name}{tail} · {self.score:.2f}"
                f" ({', '.join(self.reasons)}{thin})")


@dataclass
class RouteResult:
    chosen: list[Choice] = field(default_factory=list)
    considered: int = 0
    blocked: list[str] = field(default_factory=list)

    def names(self) -> list[str]:
        return [c.capability.name for c in self.chosen]

    def summary(self) -> str:
        line = f"후보 {self.considered} → 선택 {len(self.chosen)}"
        if self.blocked:
            line += f" · 정책상 제외 {len(self.blocked)}"
        return line

    def render(self) -> str:
        """프롬프트에 넣을 조각. 블록 모양은 `Capability.render` 한 자리에서만
        정해진다 - 여기서 또 만들면 이스케이프 규율이 두 곳으로 갈린다."""
        if not self.chosen:
            return ""
        body = render_all([c.capability for c in self.chosen], Capability.CARD)
        return "<available_capabilities>\n" + body + "\n</available_capabilities>"


def relevance(task: str, text: str) -> float:
    """과제와 글이 얼마나 겹치는가(0~1). **관련도를 재는 자리는 여기 하나다.**

    스킬·툴만 과제를 보고 기억은 안 보면, 기억은 무슨 일을 하든 늘 같은 것이 들어간다.
    같은 개념을 두 곳에서 재면 한쪽이 조용히 뒤처진다.

    IDF 를 못 쓰는 자리(코퍼스가 없는 호출)에서도 돌아야 하므로 단순 겹침 비율이다.
    순위를 매기는 데는 충분하고, 카탈로그가 있으면 `Router` 가 더 정확히 잰다.
    """
    wanted = set(tokenize(task))
    if not wanted:
        return 0.0
    return len(wanted & set(tokenize(text))) / len(wanted)


def searchable(capability: Capability) -> str:
    """검색 대상 글. 이름·설명에 **과거에 실제로 처리한 것들**을 더한다.

    E9 실측: 이름과 노드 구성만으로는 도메인 어휘가 없어 라우팅이 안 됐다(질문 190건
    중 63건이 아무것도 못 고름). 과거 실적을 넣자 같은 코퍼스에서 top-5 가 8.2% ->
    94.2% 가 됐다. 능력이 무엇인지보다 **무엇을 해왔는지**가 잘 맞춘다.
    """
    return " ".join([capability.name, capability.description, *capability.examples])


def _idf_from(token_sets: list[set[str]]) -> dict[str, float]:
    """흔한 말은 값이 싸다. 이걸 안 하면 설명이 긴 능력이 늘 이긴다."""
    total = len(token_sets) or 1
    seen: dict[str, int] = {}
    for tokens in token_sets:
        for token in tokens:
            seen[token] = seen.get(token, 0) + 1
    return {token: math.log(1 + total / count) for token, count in seen.items()}


class Router:
    """과제 → 쓸 능력 몇 개.

    `limit` 기본값이 5인 이유: 실측에서 도구가 10~15개를 넘으면 모델 정확도가 떨어진다.
    넉넉히 주는 것이 친절해 보이지만 실제로는 성능을 깎는다.
    """

    def __init__(self, capabilities: list[Capability],
                 allowed_risk: tuple[str, ...] = DEFAULT_ALLOWED_RISK,
                 verified_bonus: float = 0.6,
                 unresolved_penalty: float = 0.5) -> None:
        self.capabilities = list(capabilities)
        self.allowed_risk = tuple(allowed_risk)
        self.verified_bonus = verified_bonus
        self.unresolved_penalty = unresolved_penalty
        # 능력별 토큰을 **색인 때 한 번만** 만든다. 예전에는 질의마다 전체 코퍼스를
        # 다시 토크나이즈해서, 능력 1000개일 때 질의 1회가 38ms 였다(실측 E13).
        # 색인은 어차피 한 번이고 질의는 매번이므로, 비용은 질의 쪽에서 빼야 한다.
        self._tokens: list[set[str]] = []
        self._counts: list[dict[str, int]] = []
        # 이름·설명에서만 나온 토큰. **한 글자 조각을 가릴 때 쓴다.**
        # 이력(`examples`)은 도메인 어휘를 주지만 말버릇도 같이 준다 - `뭐야` 의
        # `뭐`. 여러 글자 낱말이면 말버릇이라도 잘 안 겹치는데, 한 글자 조각은
        # 무엇에나 겹친다.
        self._core: list[set[str]] = []
        for capability in self.capabilities:
            self._core.append(set(tokenize(
                " ".join([capability.name, capability.description]))))
            listed = tokenize(searchable(capability))
            counts: dict[str, int] = {}
            for token in listed:
                counts[token] = counts.get(token, 0) + 1
            self._tokens.append(set(counts))
            self._counts.append(counts)
        self._idf = _idf_from(self._tokens)
        self._max_idf = max(self._idf.values(), default=1.0)

    def _weight(self, token: str) -> float:
        """모르는 말은 **가장 드문 말**로 친다. 0 으로 두면 카탈로그에 없는 말이
        분모에서 사라져 근거가 두꺼워 보인다."""
        return self._idf.get(token, self._max_idf)

    def score(self, task_tokens: set[str], capability: Capability, index: int = -1
              ) -> tuple[float, list[str], float]:
        if index < 0:                      # 색인 밖에서 부르면 그때만 토크나이즈
            counts: dict[str, int] = {}
            for token in tokenize(searchable(capability)):
                counts[token] = counts.get(token, 0) + 1
        else:
            counts = self._counts[index]
        tokens = self._tokens[index] if index >= 0 else set(counts)
        core = (self._core[index] if index >= 0
                else set(tokenize(" ".join([capability.name, capability.description]))))
        # **한 글자 조각은 설명에서 온 것만 센다.**
        #
        # 토크나이저는 다음절 한글 낱말의 첫 음절을 따로 낸다 - 어미가 바뀌어도
        # 어간이 걸리게 하려는 장치다(`더해줘` 가 `두 수를 더한다` 를 `더` 로
        # 찾는다). 그런데 이력 문장에도 같은 규칙이 걸려서 `뭐야` 가 `뭐` 를 낸다.
        # 글자 수로는 이 둘을 못 가른다 - 둘 다 한 글자다.
        #
        # 가르는 것은 **어디서 왔느냐**다. 능력이 무엇인지는 이름과 설명이 말하고,
        # 이력은 그 능력이 무엇을 해왔는지를 말한다. 도메인 낱말이 이력에서 오는
        # 것은 값지지만(E9: 8.2% -> 94.2%), 말버릇 한 음절까지 근거로 세면
        # 아무 질문에나 걸린다.
        #
        # 실측(후보 26개 · 실무 질문 18건): "점심 뭐 먹을까" 가 `뭐` 하나로 결함ID
        # 도구에 12% 로 걸렸다. 문턱(10%)을 올려 막을 수는 없다 - 맞는 것들이
        # 17~20% 에 몰려 있어 같이 죽는다.
        overlap = {token for token in task_tokens & tokens
                   if len(token) > 1 or token in core}
        # 겹친 말의 IDF 합을 **글 길이로 눅인다**. 과거 실적을 붙이면 능력마다 글
        # 길이가 크게 벌어지는데(어떤 건 MR 300건, 어떤 건 3건), 나누지 않으면
        # "많이 쓰인 능력"이 무조건 이겨서 순위가 인기투표가 된다.
        # BM25 식 포화. 같은 낱말이 예시에 10번 나와도 10배가 되지 않는다 -
        # 이력을 붙이면 반복이 심해져서, 포화가 없으면 한 낱말이 점수를 지배한다.
        # 실측(E12): 실사용 질문 top-1 66.7%→72.9%, MR top-1 55.3%→57.0%·top-5 93.7%→95.6%.
        raw = sum(self._idf.get(token, 0.0)
                  * (counts[token] * (_BM25_K1 + 1)) / (counts[token] + _BM25_K1)
                  for token in overlap)
        base = raw / (1.0 + math.log(1 + len(tokens))) if tokens else 0.0
        wanted = sum(self._weight(token) for token in task_tokens)
        coverage = (sum(self._weight(token) for token in overlap) / wanted) if wanted else 0.0
        reasons: list[str] = []
        words = [t for t in overlap if len(t) > 2]
        if overlap:
            # 사람이 알아볼 수 있게 원말 위주로 보여준다(bigram 조각은 뒤로).
            shown = sorted(words, key=len, reverse=True)
            reasons.append("겹침 " + ", ".join(shown[:3] or sorted(overlap)[:3]))

        # [주의] 보정은 **곱셈**이어야 한다. 덧셈으로 두면 겹치는 말이 하나도 없는 능력이
        # 검증 보너스만으로 1등이 된다(실측: "브라우저를 열어줘"에 날짜 계산 툴이 1등).
        # 검증은 **비슷한 것들 사이에서 고르는 신호**이지 관련성을 만들어내지 않는다.
        score = base
        if capability.verified:
            score *= 1.0 + self.verified_bonus
            if base > 0:
                reasons.append(verified_mark(True))
        if not capability.resolved:
            score *= max(0.0, 1.0 - self.unresolved_penalty)
            if base > 0:
                reasons.append("미해결(내용 모름)")
        return score, reasons, coverage

    # 고를 값어치가 있으려면 **과제의 최소 이만큼은 설명해야** 한다. 점수만으로는
    # 못 거른다 - 점수는 IDF 합을 길이로 눅인 값이라 절대 크기에 뜻이 없어서,
    # 한 음절만 겹친 것이 0.99 로 나오기도 한다.
    #
    # 값은 실측으로 놓았다(실무 질문 15건, 능력 4개). 맞은 것과 헛짚은 것의 덮음이
    # 이렇게 갈렸다:
    #
    #     맞음   11% 15% 20% 37% 45% 50% 76%
    #     헛짚음  5%  6%  7%  9%          12%
    #
    #     문턱 없음  맞음 7/7 · 헛짚음 5      <- "오늘 서울 날씨" 에 날짜 도구를 내놓았다
    #     8%       맞음 7/7 · 헛짚음 2
    #     **10%**  맞음 7/7 · 헛짚음 1
    #     12%      맞음 6/7 · 헛짚음 1       <- 맞은 것을 잃기 시작한다
    #
    # 잡음 뭉치(5~9%)와 진짜 매치(11%~) 사이의 **틈**에 놓았지, 둥근 수라서 고른
    # 것이 아니다. 남는 헛짚음 하나는 잡음이 아니라 진짜 헷갈림이다 - "오늘"이
    # 날짜 낱말이라 12% 를 덮는다. 그건 문턱으로 풀 문제가 아니다.
    MIN_COVERAGE = 0.10

    def route(self, task: str, limit: int = 5,
              min_score: float = 0.01) -> RouteResult:
        task_tokens = set(tokenize(task))
        result = RouteResult(considered=len(self.capabilities))
        scored: list[Choice] = []
        for index, capability in enumerate(self.capabilities):
            if capability.risk() not in self.allowed_risk:
                result.blocked.append(f"{capability.name}({capability.risk()})")
                continue
            score, reasons, coverage = self.score(task_tokens, capability, index)
            if score < min_score or coverage < self.MIN_COVERAGE:
                # 열의 아홉을 설명 못 하는 것을 "쓸 것"으로 내놓으면 사람이 속는다.
                # 없으면 없다고 말하는 편이 낫다.
                continue
            scored.append(Choice(capability=capability, score=score, reasons=reasons,
                                 coverage=coverage))
        # 점수 → 검증 → 이름. 동점에서 순서가 흔들리면 어제와 오늘이 달라진다.
        scored.sort(key=lambda c: (-c.score, not c.capability.verified,
                                   c.capability.name))
        result.chosen = scored[:limit]
        return result
