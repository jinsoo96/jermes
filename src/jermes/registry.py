"""Tiny plugin registry with optional entry_points loading.

Groups are Jermes' own (`jermes.*`). Jermes is a standalone engine, so its
extension contract carries its own name; a host that later embeds it inherits
these groups rather than the other way round.
"""

from __future__ import annotations

from importlib import metadata
from typing import Any, Callable

GROUP_SIGNAL_EXTRACTORS = "jermes.signal_extractors"
GROUP_SYNTHESIZERS = "jermes.synthesizers"
GROUP_GATES = "jermes.gates"
GROUP_LEDGERS = "jermes.ledgers"
# 호스트에 이미 있는 능력(다른 플러그인이 등록한 도구 등)을 jermes 자신의
# discover() 목록에 끼워 넣는 자리. entry_point 는 **0 인자 factory** -
# `CapabilitySource` 를 만들어 돌려주는 함수(호스트에 실제로 붙어 있을 때만
# 뭔가를 여는 부작용이 있을 수 있으니, import 시점이 아니라 쓰는 시점에 만든다).
GROUP_CAPABILITY_SOURCES = "jermes.capability_sources"
# 위에서 찾은 능력을 **부르는** 자리. `Capability.invoke["via"]` 값으로 고른다.
# entry_point 는 `(args, choice) -> int` 함수 그 자체(factory 아님 - 부르는 데
# 상태가 필요 없다).
GROUP_CAPABILITY_CALLERS = "jermes.capability_callers"


class Registry:
    def __init__(self, group: str) -> None:
        self.group = group
        self._items: dict[str, Any] = {}
        self._loaded = False
        # 플러그인 로드 실패는 본체를 멈추지 않지만, 조용히 없어지면 왜 안 붙는지
        # 아무도 모른다. 세어서 남기고 `load_errors` 로 꺼내 볼 수 있게 한다.
        self.load_errors: list[str] = []

    def register(self, name: str, item: Any) -> None:
        self._items[name] = item

    def get(self, name: str) -> Any:
        if name not in self._items:
            raise KeyError(f"{self.group}: unknown entry {name!r}")
        return self._items[name]

    # 읽는 자리에서 **자동으로 한 번** 확장을 붙인다. 호출측이 로더를 기억해야 하면
    # 언젠가 한 경로에서 빠뜨리고, 그 경로에서는 플러그인이 조용히 사라진다.
    def names(self) -> list[str]:
        self.load_entry_points()
        return sorted(self._items)

    def items(self) -> list[tuple[str, Any]]:
        self.load_entry_points()
        return sorted(self._items.items())

    def load_entry_points(self) -> int:
        """설치된 패키지가 선언한 확장을 붙인다. **한 번만** 돈다.

        예전에는 이 함수를 아무도 부르지 않았다. 확장 지점이 설계 기조인데 로더가
        안 돌면 남이 선언한 추출기·합성기는 영영 안 붙는다 - 있는 척하는 확장성이다.
        지금은 레지스트리를 처음 쓸 때 자동으로 돈다.

        실패는 삼키되 **세어서 남긴다**. 플러그인 하나가 깨졌다고 본체가 멈추면 안
        되지만, 조용히 없어지면 왜 안 붙는지 아무도 모른다.
        """
        if self._loaded:
            return 0
        self._loaded = True
        try:
            eps = metadata.entry_points(group=self.group)
        except Exception as exc:
            self.load_errors.append(f"{self.group}: {type(exc).__name__}")
            return 0
        loaded = 0
        for ep in eps:
            try:
                self.register(ep.name, ep.load())
                loaded += 1
            except Exception as exc:
                self.load_errors.append(f"{ep.name}: {type(exc).__name__}: {exc}"[:160])
        return loaded


def registry_decorator(reg: Registry) -> Callable[[str], Callable[[Any], Any]]:
    def outer(name: str) -> Callable[[Any], Any]:
        def inner(obj: Any) -> Any:
            reg.register(name, obj)
            return obj

        return inner

    return outer
