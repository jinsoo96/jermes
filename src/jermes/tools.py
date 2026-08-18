"""SF9 - 툴 단조(forging): 반복되는 절차를 **실행 가능한 스크립트**로 만든다.

여태 Jermes 가 만든 것은 문서(guide)와 설정(config)뿐이었다. `tool` 종류는 플랫폼의
컴파일 경로를 가리키는 매니페스트일 뿐 실제로 도는 물건이 아니었다. 그래서
"에이전트가 능력을 얻는다"는 말은 성립하지 않았다.

**왜 이게 나은가.**
- 스킬을 자동 생성하는 쪽은 있지만 **효능 검증이 없다**. 문서가 맞는지 아무도 안 본다.
  실행 코드를 번들할 수 있어도 그 코드가 맞는지는 사람이 읽어야 안다.
- 여기서는 절차를 **스크립트로 만들고 그걸 실제로 실행해서** 통과한 것만 남긴다.
  게다가 툴 검증은 **LLM 이 전혀 필요 없다** - 입력을 넣고 출력을 비교하면 끝이다.
  스킬 검증(재현벤치)은 LLM 재생이 필요하지만 툴은 결정적이다. 더 싸고 더 확실하다.

**계약**: 스크립트는 stdin 으로 JSON 을 받아 stdout 으로 JSON 을 낸다. 그게 전부다.
언어 런타임 외의 의존성을 두지 않는다(가져다 쓰는 쪽이 설치할 게 없어야 한다).

**안전은 금지가 아니라 권한이다** (`ToolPolicy`). 파일 쓰기·네트워크를 무조건 막으면
쓸모 있는 툴의 절반을 못 만든다. 대신 툴이 무엇을 하는지 선언하고 사람이 허락하며,
허락한 내용은 MCP 주석 어휘로 패키지에 실려 나가 받는 쪽도 미리 안다.

[주의] **정직한 경계**: 이 검사는 *사고*를 줄이는 것이지 **샌드박스가 아니다**. 정규식은
우회할 수 있고, 네트워크를 허락한 순간 그 툴은 무엇이든 밖으로 보낼 수 있다.
신뢰할 수 없는 출처의 스크립트는 사람이 읽고 승인해야 한다.
"""

from __future__ import annotations

import ast
import json
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .gate import (BenchCase, GateConfig, HOLDOUT_CONFIRMED,
                   HOLDOUT_REGRESSED, HOLDOUT_UNPROVEN, decide, split_holdout)
from .model import Provenance, SkillDef

# ── 정적 검사: 정규식이 아니라 **구문 트리**를 본다 ──────────────────────────
#
# 왜 바꿨나. 정규식은 글자만 본다. 실측으로 뚫렸다: 권한이 **하나도 없는** 기본
# 정책에서 아래가 전부 통과하고 실제로 실행됐고, 그중 하나는 임시 디렉터리 밖
# 절대경로에 파일을 만들었다.
#
#     open('x', mode='w')      쉼표 뒤 따옴표를 찾는 패턴이 키워드 인자를 못 봄
#     Path('x').write_text()   규칙에 unlink 만 있고 write_* 가 없었음
#     shutil.copy(a, '/etc/b') 규칙에 rmtree 만 있었음
#     os.makedirs('deep/dir')  규칙에 없었음
#
# 이게 왜 치명적인가: 통과한 정책은 `annotations()` 로 `readOnlyHint: true` 가
# 되고, 그 주석은 `Capability.risk()` 에서 `safe` 가 되어 **`ask` 가 동의 없이
# 자동 실행**하고 **`serve` 가 다른 에이전트에게 읽기전용이라고 내준다**.
# 검사가 틀리면 안전 등급 전체가 거짓말이 된다.
#
# 구문 트리는 이 부류를 통째로 막는다. 인자를 어떻게 쓰든 `open(...)` 호출은
# `Call(func=Name('open'))` 이고, 모드는 위치 인자든 키워드든 같은 자리에서 읽는다.
# 트리로도 못 막는 것(변수에 담아 우회, getattr)은 남지만, 그건 정규식으로도 못
# 막던 것이고 **통과 = 안전 아님**이라는 계약은 그대로다.

# 모듈 이름 -> (설명, 필요한 권한)
_BANNED_IMPORTS = {
    "subprocess": ("프로세스 실행", "allow_process"),
    "socket": ("네트워크 소켓", "allow_network"),
    "urllib": ("네트워크 요청", "allow_network"),
    "http": ("네트워크 요청", "allow_network"),
    "requests": ("네트워크 요청", "allow_network"),
    "httpx": ("네트워크 요청", "allow_network"),
    "ftplib": ("네트워크 요청", "allow_network"),
    "smtplib": ("네트워크 요청", "allow_network"),
    "telnetlib": ("네트워크 요청", "allow_network"),
    "asyncio": ("네트워크 요청", "allow_network"),
    "ctypes": ("동적 실행", "allow_dynamic"),
    "importlib": ("동적 실행", "allow_dynamic"),
}

# 붙여 쓰는 이름(`os.system`, `shutil.copy`, `Path(...).write_text`) -> (설명, 권한)
_BANNED_ATTRS = {
    # 프로세스
    "system": ("셸 실행", "allow_process"), "popen": ("셸 실행", "allow_process"),
    "execv": ("셸 실행", "allow_process"), "execl": ("셸 실행", "allow_process"),
    "spawnv": ("셸 실행", "allow_process"), "fork": ("셸 실행", "allow_process"),
    # 삭제
    "remove": ("파일 삭제", "allow_delete"), "unlink": ("파일 삭제", "allow_delete"),
    "rmtree": ("파일 삭제", "allow_delete"), "rmdir": ("파일 삭제", "allow_delete"),
    # 쓰기·이동·생성
    "write_text": ("파일 쓰기", "allow_write"),
    "write_bytes": ("파일 쓰기", "allow_write"),
    "writelines": ("파일 쓰기", "allow_write"),
    "mkdir": ("파일 쓰기", "allow_write"),
    "makedirs": ("파일 쓰기", "allow_write"),
    "rename": ("파일 쓰기", "allow_write"),
    "replace": ("파일 쓰기", "allow_write"),
    "touch": ("파일 쓰기", "allow_write"),
    "copy": ("파일 쓰기", "allow_write"),
    "copy2": ("파일 쓰기", "allow_write"),
    "copyfile": ("파일 쓰기", "allow_write"),
    "copytree": ("파일 쓰기", "allow_write"),
    "move": ("파일 쓰기", "allow_write"),
    "chmod": ("파일 쓰기", "allow_write"),
    "symlink_to": ("파일 쓰기", "allow_write"),
    "urlopen": ("네트워크 요청", "allow_network"),
    "urlretrieve": ("네트워크 요청", "allow_network"),
}

# `replace` 와 `copy` 는 문자열·사전에도 있다. 파일 쪽일 때만 위험하다.
#
# 예전에는 뿌리 이름에 힌트가 **부분문자열**로 들어 있는지 봤고, 그 힌트에 한
# 글자 "p"·"f" 가 있었다. 그런데 이 엔진의 계약 파라미터가 정확히 `payload`
# 다(`def run(payload: dict)`). 그래서 `payload.replace(...)` 가 파일 쓰기로
# 잡혔고, 문자열을 다루는 흔한 툴이 통째로 막혔다. 실측: LLM 자동생성이 6/6
# 전멸했고, 되먹임이 "파일 쓰기 코드가 있다"를 주는데 코드에 파일 쓰기가
# 없으니 모델이 고칠 수가 없어 토큰만 태우고 수렴하지 않았다.
#
# 추측하지 말고 **정확한 뿌리 이름**만 본다. `os.replace`·`shutil.copy`·
# `Path(...).replace` 는 위험하고, `payload.replace`·`text.replace` 는 아니다.
_ONLY_ON_PATHS = {"replace", "copy"}
_PATH_ROOTS = {"os", "shutil", "path", "pathlib"}

_BANNED_CALLS = {
    "eval": ("동적 실행", "allow_dynamic"),
    "exec": ("동적 실행", "allow_dynamic"),
    "compile": ("동적 실행", "allow_dynamic"),
    "__import__": ("동적 실행", "allow_dynamic"),
    "getattr": ("동적 실행", "allow_dynamic"),
}
_WRITE_MODES = set("wax+")


def _root_name(node) -> str:
    """`shutil.copy` 의 `shutil`, `Path('x').write_text` 의 `Path` 처럼 뿌리 이름."""
    while True:
        if isinstance(node, ast.Attribute):
            node = node.value
        elif isinstance(node, ast.Call):
            node = node.func
        elif isinstance(node, ast.Subscript):
            node = node.value
        else:
            break
    return node.id if isinstance(node, ast.Name) else ""


def _open_is_write(call) -> bool:
    """`open(...)` 이 쓰기 모드인가. 위치 인자든 키워드든 같은 자리에서 읽는다.

    모드가 **상수가 아니면 쓰기로 본다.** 변수나 식으로 넘기면 우리는 모르는
    것이고, 모르는 것을 읽기로 치는 순간 이 검사를 우회하는 법이 생긴다
    (`open(p, "mode" and "w")` 로 실제로 뚫렸다). 이 레포는 MCP 주석에서도
    같은 판단을 한다: 모르는 것은 안전이 아니라 모르는 것이다.
    """
    node = None
    if len(call.args) > 1:
        node = call.args[1]
    for keyword in call.keywords:
        if keyword.arg == "mode":
            node = keyword.value
    if node is None:
        return False          # 모드를 안 주면 읽기다
    if not isinstance(node, ast.Constant):
        return True           # 모르면 쓰기로 본다
    return bool(_WRITE_MODES & set(str(node.value)))


def _module_aliases(tree) -> tuple[dict, dict]:
    """이 파일 안에서 이름이 **어느 모듈에서 왔는지** 표로 만든다.

    별칭 때문에 필요하다. `import shutil as sh` 뒤의 `sh.copy(...)` 는 뿌리
    이름이 "sh" 라 글자로만 보면 안 걸린다. 실측으로 그렇게 뚫렸다.

    반환은 (모듈별칭 -> 모듈, 직수입이름 -> 모듈).
    """
    modules: dict[str, str] = {}
    direct: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                head = alias.name.split(".")[0]
                modules[(alias.asname or alias.name).split(".")[0]] = head
        elif isinstance(node, ast.ImportFrom) and node.module:
            head = node.module.split(".")[0]
            for alias in node.names:
                # 별칭을 붙이면 **원래 이름**을 잃는다. `copy as cp` 를 로컬 이름
                # `cp` 로만 기억하면 금지 목록(원래 이름 기준)과 안 맞는다.
                # 실측: `from shutil import copy as cp` 가 그대로 통과했다.
                direct[alias.asname or alias.name] = (head, alias.name)
    return modules, direct


# 순수 계산에만 쓰이는 표준 라이브러리. 여기 없는 것을 들여오면 그 툴이
# 무엇을 하는지 우리는 **모른다**(위험하다는 뜻이 아니다).
#
# 목록이 짧은 이유: 이건 금지 목록이 아니라 **허용 목록**이라 빠뜨려도 안전한
# 쪽으로 틀린다. 금지 목록은 빠뜨리면 위험한 쪽으로 틀렸다 - 그게 `os.open` 이
# 통과한 이유다.
_PURE_MODULES = frozenset({
    "math", "cmath", "json", "re", "datetime", "decimal", "fractions",
    "statistics", "itertools", "functools", "collections", "string",
    "textwrap", "unicodedata", "hashlib", "hmac", "base64", "binascii",
    "uuid", "random", "calendar", "enum", "dataclasses", "typing",
    "operator", "bisect", "heapq", "array", "copy", "numbers", "zoneinfo",
    "difflib", "pprint", "reprlib", "types", "abc", "warnings",
})

# 바깥과 닿거나 코드를 만들어 내는 이름. 하나라도 있으면 순수하다고 말할 수 없다.
_IMPURE_NAMES = frozenset({
    "open", "eval", "exec", "compile", "input", "breakpoint",
    "globals", "locals", "vars", "getattr", "setattr", "delattr",
    "memoryview", "help",
})


def purity_scan(script: str) -> str:
    """**순수 계산뿐임을 증명할 수 있는가.** 증명하면 빈 문자열, 못 하면 그 이유.

    금지 목록(`ast_scan`) 과 방향이 반대다. 저쪽은 "아는 나쁜 것"을 찾고, 여기는
    "아는 좋은 것만 있는가"를 본다. 목록을 빠뜨렸을 때 저쪽은 위험한 쪽으로,
    여기는 안전한 쪽으로 틀린다.

    실측: `os.open`+`os.write` 로 파일을 쓰는 툴이 금지 목록을 통과해 `safe ·
    검증됨` 을 받고 임시 디렉터리 밖에 파일을 썼다. 그 스크립트는 여기서
    "os 를 들여옵니다" 로 걸린다 - `os` 안에 무엇이 있는지 일일이 알 필요가 없다.

    주의: 이건 여전히 **적대적 코드를 막는 장치가 아니다.** 파싱을 못 하면 모른다고
    답하고, 파싱한 것만 본다. 하는 일은 "안전하다고 **말하지 않을** 때를 아는 것"
    이다. 막는 것과 아는 것은 다르고, 여기서 고치는 쪽은 후자다.
    """
    try:
        tree = ast.parse(script or "")
    except SyntaxError as exc:
        return f"파싱을 못 했습니다({exc.msg})"

    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = ([alias.name for alias in node.names]
                     if isinstance(node, ast.Import) else [node.module or ""])
            for name in names:
                head = (name or "").split(".")[0]
                if head and head not in _PURE_MODULES:
                    return f"{head} 를 들여옵니다(순수 계산 목록에 없습니다)"
        elif isinstance(node, ast.Name) and node.id in _IMPURE_NAMES:
            return f"{node.id} 을(를) 씁니다"
        elif isinstance(node, ast.Attribute) and node.attr.startswith("__"):
            return f"{node.attr} 에 손댑니다(내부 구조 우회)"

    return ""


def ast_scan(script: str, policy) -> str:
    """구문 트리로 본 위반. 못 파싱하면 빈 문자열(호출측이 정규식으로 한 번 더 본다)."""
    try:
        tree = ast.parse(script or "")
    except SyntaxError:
        return ""

    def blocked(permission: str) -> bool:
        return not getattr(policy, permission, False)

    modules, direct = _module_aliases(tree)

    # `from shutil import copy` 처럼 위험한 이름을 **직접** 들여온 경우.
    # 호출 지점은 `copy(...)` 라 속성 접근이 아니어서 아래 분기에 안 걸린다.
    for local, (module, original) in direct.items():
        found = _BANNED_ATTRS.get(original)
        if found and (module in _PATH_ROOTS or original not in _ONLY_ON_PATHS):
            if blocked(found[1]):
                return (f"{found[0]} 코드가 들어 있는데 허락되지 않았습니다 "
                        f"({found[1]}=True 로 허용하거나 코드를 고치세요)")

    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = ([alias.name for alias in node.names]
                     if isinstance(node, ast.Import) else [node.module or ""])
            for name in names:
                head = (name or "").split(".")[0]
                found = _BANNED_IMPORTS.get(head)
                if found and blocked(found[1]):
                    return (f"{found[0]} 코드가 들어 있는데 허락되지 않았습니다 "
                            f"({found[1]}=True 로 허용하거나 코드를 고치세요)")
        elif isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name):
                if func.id == "open" and _open_is_write(node) and blocked("allow_write"):
                    return ("파일 쓰기 코드가 들어 있는데 허락되지 않았습니다 "
                            "(allow_write=True 로 허용하거나 코드를 고치세요)")
                found = _BANNED_CALLS.get(func.id)
                if found and blocked(found[1]):
                    return (f"{found[0]} 코드가 들어 있는데 허락되지 않았습니다 "
                            f"({found[1]}=True 로 허용하거나 코드를 고치세요)")
            elif isinstance(func, ast.Attribute):
                found = _BANNED_ATTRS.get(func.attr)
                if not found:
                    continue
                if func.attr in _ONLY_ON_PATHS:
                    # 별칭을 따라간다. `import shutil as sh` 뒤의 `sh.copy` 는
                    # 뿌리 이름이 "sh" 지만 실제로는 shutil 이다.
                    root = _root_name(func)
                    origin = modules.get(root, root).lower()
                    if origin not in _PATH_ROOTS and root.lower() not in _PATH_ROOTS:
                        continue      # 문자열 `.replace` 같은 것은 위험이 아니다
                if blocked(found[1]):
                    return (f"{found[0]} 코드가 들어 있는데 허락되지 않았습니다 "
                            f"({found[1]}=True 로 허용하거나 코드를 고치세요)")
    return ""


# 각 규칙은 (패턴, 이름, **어떤 권한이 있어야 허용되는가**) 다.
# 무조건 금지가 아니라 권한 대응이라는 점이 중요하다 - 전부 막으면 쓸모 있는 툴의
# 절반을 못 만든다(파일을 쓰는 툴, API 를 부르는 툴은 정상적인 요구다).
_RULES = [
    (r"\bimport\s+(?:os\s*,\s*)?subprocess\b|\bfrom\s+subprocess\b",
     "프로세스 실행", "allow_process"),
    (r"\bos\s*\.\s*system\b|\bos\s*\.\s*popen\b|\bos\s*\.\s*exec[lv]",
     "셸 실행", "allow_process"),
    (r"\bimport\s+socket\b|\bfrom\s+socket\b", "네트워크 소켓", "allow_network"),
    (r"\bimport\s+(?:urllib|http\.client|requests|httpx)\b"
     r"|\bfrom\s+(?:urllib|requests|httpx)\b", "네트워크 요청", "allow_network"),
    (r"\bshutil\s*\.\s*rmtree\b|\bos\s*\.\s*remove\b|\bos\s*\.\s*unlink\b"
     r"|\bPath\([^)]*\)\.unlink\b", "파일 삭제", "allow_delete"),
    (r"\b__import__\s*\(|\beval\s*\(|\bexec\s*\(", "동적 실행", "allow_dynamic"),
    # 두 번째 인자(모드)를 봐야 한다. 첫 인자만 훑으면 `open('a.txt')` 의 파일명
    # 'a' 를 append 모드로 오인한다 - 읽기까지 막혀 툴을 못 만든다.
    (r"\bopen\s*\([^)]*,\s*[\"'][^\"']*[wax+]", "파일 쓰기", "allow_write"),
]

DEFAULT_TIMEOUT = 10.0


@dataclass
class ToolPolicy:
    """이 툴이 **무엇을 해도 되는지**. 금지 목록이 아니라 권한 목록이다.

    예전 구조는 파일 쓰기·네트워크를 무조건 막았다. 그러면 "정산서를 파일로 떨군다",
    "환율 API 를 부른다" 같은 정상적인 툴을 아예 만들 수 없다. 그래서 막는 대신
    **선언하게 하고 사람이 허락**한다. 허락한 것은 스킬에 기록되고 패키지에 실려
    나가므로, 받는 쪽도 이 툴이 무엇을 하는지 미리 안다.

    [주의] **선언은 검사를 대신하지 않는다.** 정규식은 우회할 수 있고, 네트워크를 허락한
    순간 그 툴은 무엇이든 밖으로 보낼 수 있다. 여기서 하는 일은 *사고*를 줄이는 것이지
    적대적 코드를 막는 것이 아니다. 신뢰할 수 없는 출처의 스크립트는 사람이 읽어야 한다.
    """

    allow_write: bool = False
    allow_delete: bool = False
    allow_network: bool = False
    allow_process: bool = False
    allow_dynamic: bool = False
    timeout: float = DEFAULT_TIMEOUT
    # **커널이** 거는 한계. 파이썬 안의 검사로는 못 막는 부류다 - 메모리
    # 할당은 문법을 어기지 않는다. 실측: 1GB 를 0.4초에 잡고, 200MB 를
    # 0.2초에 썼다. 사람이 자리를 비운 사이 도는 물건에서 이건 기계를 멈춘다.
    max_memory_mb: int = 512
    max_output_mb: int = 64
    # 넘겨줄 환경변수 이름들. 기본은 아무것도 안 준다 - 비밀값이 실수로 새는 걸 막는다.
    # API 키가 필요하면 **그 이름만** 명시적으로 적는다.
    env_allowlist: tuple[str, ...] = ()

    PRESETS = ("strict", "files", "network", "trusted")

    @classmethod
    def preset(cls, name: str, **overrides) -> "ToolPolicy":
        """이름으로 고르는 흔한 조합. 세밀하게 필요하면 필드를 직접 준다."""
        name = (name or "strict").lower()
        if name not in cls.PRESETS:
            raise ValueError(f"모르는 정책: {name} (있는 것: {', '.join(cls.PRESETS)})")
        base = {
            "strict": {},
            "files": {"allow_write": True},
            "network": {"allow_network": True},
            # trusted 는 "사람이 읽고 책임진다"는 뜻이다. 자동으로 붙지 않는다.
            "trusted": {"allow_write": True, "allow_delete": True, "allow_network": True,
                        "allow_process": True, "allow_dynamic": True},
        }[name]
        base.update(overrides)
        return cls(**base)

    def granted(self) -> list[str]:
        return [field_name for field_name in
                ("allow_write", "allow_delete", "allow_network",
                 "allow_process", "allow_dynamic")
                if getattr(self, field_name)]

    def annotations(self) -> dict:
        """MCP 도구 주석 어휘로 옮긴다 - 새 등급 이름을 발명하지 않는다.
        (`readOnlyHint`/`destructiveHint`/`idempotentHint`/`openWorldHint`)"""
        writes = self.allow_write or self.allow_delete or self.allow_process
        return {
            "read_only": not writes and not self.allow_network,
            "destructive": self.allow_delete or self.allow_process,
            "idempotent": not self.allow_delete and not self.allow_process,
            "open_world": self.allow_network,
        }

    def describe(self) -> str:
        granted = self.granted()
        return "권한 없음(순수 계산)" if not granted else "허용: " + ", ".join(granted)

    def to_dict(self) -> dict:
        return {"allow_write": self.allow_write, "allow_delete": self.allow_delete,
                "allow_network": self.allow_network, "allow_process": self.allow_process,
                "allow_dynamic": self.allow_dynamic,
                # 자원 한계도 **같이 실린다.** 안 실으면 받는 쪽이 기본값으로
                # 돌려서, 512MB 로 검증한 툴이 남의 기계에서는 다른 한계로 돈다.
                # 권한을 실어 보내는 이유와 정확히 같다 - 받는 쪽이 미리 알아야 한다.
                "max_memory_mb": self.max_memory_mb,
                "max_output_mb": self.max_output_mb,
                "env_allowlist": list(self.env_allowlist)}

    @classmethod
    def from_dict(cls, data: dict | None, **overrides) -> "ToolPolicy":
        """저장된 권한 그대로 되살린다. 모르는 열쇠는 버린다 - 나중에 필드가 늘어도
        옛 기록이 터지지 않아야 한다."""
        data = data or {}
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        if "env_allowlist" in known:
            known["env_allowlist"] = tuple(known["env_allowlist"] or ())
        known.update(overrides)
        return cls(**known)


@dataclass
class ToolCase:
    """툴 검증 케이스. LLM 이 필요 없다.

    정답이 하나로 안 떨어지는 절차도 있다 - 요약, 정렬, 추출, 포맷팅. 그런 걸
    "정답 일치"로만 재면 툴로 만들 수 있는 일이 좁아진다(그게 예전의 한계였다).
    그래서 **검사 방식을 셋** 둔다. 어느 쪽이든 채점은 여전히 결정적이고 LLM 이 없다.

      expect  : 값이 정확히 같아야 한다              (가장 강함)
      match   : 출력 문자열이 이 정규식에 걸려야 한다  (모양만 정하고 싶을 때)
      check   : `output -> bool` 술어를 만족해야 한다  (성질만 정하고 싶을 때)

    셋 다 없으면 케이스가 아무것도 주장하지 않으므로 **실패로 친다** - 통과를
    공짜로 주면 검증 전체가 무의미해진다.
    """

    case_id: str
    payload: dict = field(default_factory=dict)
    expect: Any = None
    match: str = ""
    check: Any = None                 # Callable[[Any], bool]
    _has_expect: bool = True          # expect=None 을 "기대값 없음"과 구분한다

    def __post_init__(self) -> None:
        if self.expect is None and (self.match or self.check is not None):
            self._has_expect = False

    def judge(self, output: Any) -> tuple[bool, str]:
        """(통과했는가, 왜 아닌가). 판정 자리를 한 곳에 모은다."""
        if self._has_expect:
            if output != self.expect:
                return False, f"기대 {self.expect!r}, 실제 {output!r}"
        if self.match:
            text = output if isinstance(output, str) else json.dumps(
                output, ensure_ascii=False, sort_keys=True, default=str)
            if not re.search(self.match, text):
                return False, f"정규식 {self.match!r} 에 안 걸림: {text[:120]!r}"
        if self.check is not None:
            try:
                if not self.check(output):
                    return False, f"성질 검사 실패: {output!r}"
            except Exception as exc:
                return False, f"성질 검사가 터짐: {type(exc).__name__}: {exc}"
        if not self._has_expect and not self.match and self.check is None:
            return False, "케이스가 아무것도 주장하지 않습니다(expect/match/check 중 하나 필요)"
        return True, ""

    @classmethod
    def from_dict(cls, data: dict, index: int = 0) -> "ToolCase":
        """dict -> 케이스. **케이스를 만드는 자리는 여기 하나다** - 파일에서 읽든
        툴 매니페스트에서 꺼내든 같은 규칙으로 해석돼야 한다.

        `check`(파이썬 술어)는 직렬화할 수 없어 복원되지 않는다. 그래서 `expect` 도
        `match` 도 없는 항목은 **아무것도 주장하지 않는 케이스**가 되고 통과하지
        못한다 - 저장 못 한 조건을 통과한 척하면 검증이 거짓말이 된다.
        """
        case = cls(case_id=str(data.get("case_id") or f"case-{index}"),
                   payload=data.get("payload") or {},
                   expect=data.get("expect"),
                   match=str(data.get("match") or ""))
        if "expect" not in data:
            case._has_expect = False
        return case

    def to_dict(self) -> dict:
        """저장용. `check` 는 파이썬 함수라 담을 수 없다 - 담은 척하지 않는다."""
        record = {"case_id": self.case_id, "payload": self.payload}
        if self._has_expect:
            record["expect"] = self.expect
        if self.match:
            record["match"] = self.match
        return record

    def as_bench_case(self) -> BenchCase:
        return BenchCase(case_id=self.case_id, payload=self.payload)


@dataclass
class ToolRun:
    ok: bool
    output: Any = None
    error: str = ""
    seconds: float = 0.0


@dataclass
class ToolReport:
    passed: int = 0
    failed: int = 0
    dev_pass: int = 0
    holdout_pass: int = 0
    dev_total: int = 0
    holdout_total: int = 0
    failures: list[str] = field(default_factory=list)
    rejected: str = ""

    @property
    def verdict(self) -> str:
        """툴은 **전부 통과해야** 승격한다. 스킬은 확률적 개선이지만 툴은 결정적이라
        "대체로 맞는다"는 말이 성립하지 않는다.

        재는 방식은 다르지만 판정은 `gate.decide` 한 자리에서 나온다 - 세 낱말이
        뜻하는 바가 두 경로에서 달라지면 원장 전체를 못 믿게 된다.
        """
        if self.rejected:
            return "rejected"
        measured = bool(self.dev_total and self.holdout_total)
        holdout_state = (HOLDOUT_CONFIRMED if self.holdout_pass == self.holdout_total
                         else HOLDOUT_REGRESSED)
        # 홀드아웃이 얇으면 `검증됨` 을 붙이지 않는다. 스킬 게이트와 같은 문턱을
        # 쓴다 - 재는 방식이 달라도 그 세 낱말의 뜻은 두 경로에서 같아야 한다.
        #
        # 실측: 케이스 4줄짜리 csv 로 `jermes tool` 을 돌리면 홀드아웃이 1건이고,
        # 그 하나가 `검증됨` 을 정했다. 원장에서 `검증됨` 을 읽는 사람은 그게
        # 1건에 걸린 것인지 20건에 걸린 것인지 알 수 없다.
        if measured and self.holdout_total < GateConfig().min_holdout_to_promote \
                and holdout_state == HOLDOUT_CONFIRMED:
            holdout_state = HOLDOUT_UNPROVEN
        return decide(measured=measured,
                      dev_ok=self.dev_pass == self.dev_total,
                      holdout=holdout_state)

    def summary(self) -> str:
        if self.rejected:
            return f"rejected: {self.rejected}"
        return (f"dev {self.dev_pass}/{self.dev_total} · "
                f"holdout {self.holdout_pass}/{self.holdout_total}"
                + (f" · 실패 {len(self.failures)}건" if self.failures else ""))


def safety_scan(script: str, policy: ToolPolicy | None = None) -> str:
    """**허락하지 않은** 동작이 들어 있는지 본다. 통과 = '안전 확인'이 아니라
    '선언 밖의 동작은 안 보임'이다. 이 차이를 흐리면 안 된다.

    구문 트리를 먼저 보고, 그다음 정규식을 본다. 트리는 인자를 어떻게 쓰든
    같은 호출로 보므로 글자 장난에 안 뚫리고, 정규식은 트리가 못 파싱하는
    조각(문법 오류가 있는 초안)에서도 최소한을 지킨다. **둘 다** 통과해야
    통과다.
    """
    policy = policy or ToolPolicy()
    reason = ast_scan(script, policy)
    if reason:
        return reason
    for pattern, label, permission in _RULES:
        if getattr(policy, permission, False):
            continue          # 허락한 것은 검사하지 않는다
        if re.search(pattern, script or ""):
            return (f"{label} 코드가 들어 있는데 허락되지 않았습니다 "
                    f"({permission}=True 로 허용하거나 코드를 고치세요)")
    if not script or "def run(" not in script:
        return "진입점 `def run(payload: dict)` 이 없음"
    return ""


_GUARD = """
import os as _os, sys as _sys

_ROOTS = [_os.path.realpath(p) for p in {roots!r}]


# 부르는 쪽이 **직접 지목한** 경로를 상자에 더한다.
#
# 허락(allow_write)은 "이 툴이 파일을 쓴다"는 뜻이지 "어디든 쓴다"는 뜻이 아니다.
# 어디는 **부르는 쪽**이 정한다 - 페이로드에 적힌 경로가 그것이다. 툴이 스스로
# 고른 절대경로는 여기 없으므로 상자 밖이다.
#
# 페이로드는 툴이 아니라 호출자가 만든다. 그래서 이 목록은 툴이 못 늘린다.
def _allow(path):
    try:
        real = _os.path.realpath(path)
    except (TypeError, ValueError):
        return
    if real not in _ROOTS:
        _ROOTS.append(real)
_ALLOW = {allow!r}
_WRITE_FLAGS = _os.O_WRONLY | _os.O_RDWR | _os.O_CREAT | _os.O_APPEND | _os.O_TRUNC


def _inside(path):
    try:
        real = _os.path.realpath(path)
    except (TypeError, ValueError):
        return True          # 경로가 아닌 것(파일서술자 등)은 우리 관심사가 아니다
    return any(real == r or real.startswith(r + _os.sep) for r in _ROOTS)


def _refuse(what, need, detail):
    raise PermissionError(
        "허락받은 곳 밖입니다 - %s: %s\\n"
        "  이 툴을 만들 때 %s 를 안 켰습니다. 필요하면 그 권한으로 다시 만드세요."
        % (what, detail, need))


def _writes(event, args):
    \"\"\"이 `open` 사건이 쓰기인가. 모드 문자열이든 O_ 플래그든 같은 자리에서 본다.\"\"\"
    mode, flags = (args + (None, None))[1:3]
    if isinstance(mode, str) and any(c in mode for c in "wxa+"):
        return True
    return bool(isinstance(flags, int) and flags & _WRITE_FLAGS)


# 사건 이름을 **가족 단위**로 적는다. 하나씩 적으면 결국 금지 목록과 같은 실수다 -
# 실측: `os.startfile` 을 안 적어서 권한 0 인 툴이 calc.exe 를 띄웠다.
_WRITE_EVENTS = ("os.mkdir", "os.rename", "os.replace", "os.link", "os.symlink",
                 "os.truncate", "os.chmod", "os.chown", "os.utime", "os.chdir")
_DELETE_EVENTS = ("os.remove", "os.unlink", "os.rmdir", "shutil.rmtree",
                  "shutil.move")
_NETWORK_EVENTS = ("socket.connect", "socket.bind", "socket.getaddrinfo",
                   "socket.sendto", "urllib.Request", "ftplib.connect",
                   "smtplib.connect", "http.client.connect")
_PROCESS_EVENTS = ("subprocess.Popen", "os.system", "os.exec", "os.spawn",
                   "os.posix_spawn", "os.startfile", "os.fork", "os.forkpty",
                   "os.kill", "os.killpg", "_winapi.CreateProcess",
                   "pty.spawn")
_DYNAMIC_EVENTS = ("ctypes.dlopen", "ctypes.dlsym", "ctypes.dlsym/handle",
                   "ctypes.call_function", "ctypes.set_exception",
                   "ctypes.cdata", "ctypes.create_string_buffer")


def _hook(event, args):
    # **허락은 "무엇을" 이고 상자는 "어디까지" 다. 둘 다 지킨다.**
    #
    # 예전에는 `not _ALLOW["write"] and not _inside(...)` 였다 - 허락하면 경계를
    # 통째로 건너뛴다는 뜻이다. 실측으로 `allow_write` 인 툴이 드라이브 루트에
    # 파일을 넷 만들었고, `allow_delete` 인 툴이 상자 밖 폴더를 rmtree 했다.
    # `--policy files` 라는 한 낱말이 그런 뜻일 수는 없다.
    #
    # 상자는 임시 폴더 + **부르는 쪽이 페이로드에 적어 준 경로**다. 어디에 쓸지는
    # 호출자가 정하고 툴은 따른다. 툴이 스스로 고른 절대경로는 상자 밖이다.
    if event == "open":
        if _writes(event, args):
            if not _inside(args[0]):
                _refuse("파일 쓰기", "부르는 쪽이 지목한 경로", args[0])
            if not _ALLOW["write"]:
                _refuse("파일 쓰기", "allow_write", args[0])
    elif event in _DELETE_EVENTS:
        if not all(_inside(a) for a in args if a):
            _refuse("파일 삭제·이동", "부르는 쪽이 지목한 경로", str(args[:1])[:80])
        if not _ALLOW["delete"]:
            _refuse("파일 삭제·이동", "allow_delete", str(args[:1])[:80])
    elif event in _WRITE_EVENTS:
        if not all(_inside(a) for a in args if a):
            _refuse("파일 쓰기", "부르는 쪽이 지목한 경로", str(args[:1])[:80])
        if not _ALLOW["write"]:
            _refuse("파일 쓰기", "allow_write", str(args[:1])[:80])
    elif event in _NETWORK_EVENTS:
        if not _ALLOW["network"]:
            _refuse("네트워크", "allow_network", str(args[:1])[:80])
    elif event in _PROCESS_EVENTS:
        if not _ALLOW["process"]:
            _refuse("프로세스 실행", "allow_process", str(args[:1])[:80])
    elif event in _DYNAMIC_EVENTS:
        if not _ALLOW["dynamic"]:
            _refuse("네이티브 코드 호출", "allow_dynamic", str(args[:1])[:80])


# **한 번 걸면 못 뗀다.** 원숭이 패칭은 `builtins.open = _real` 한 줄로 되돌릴 수
# 있었다. 감사 훅은 설계상 제거가 불가능하다.
_sys.addaudithook(_hook)
"""


_RUNNER = '''
import json, sys
{guard}
{body}

if __name__ == "__main__":
    payload = json.loads(sys.stdin.read() or "{{}}")
    # 호출자가 지목한 경로를 상자에 더한다. **툴 코드가 도는 앞**이라 툴은 못 늘린다.
    try:
        _stack = [payload]
        while _stack:
            _item = _stack.pop()
            if isinstance(_item, dict):
                _stack.extend(_item.values())
            elif isinstance(_item, (list, tuple)):
                _stack.extend(_item)
            elif isinstance(_item, str) and _item.strip():
                _allow(_item)
    except NameError:
        pass          # 관문을 못 건 판(권한 없음)에서는 늘릴 것도 없다
    sys.stdout.write(json.dumps(run(payload), ensure_ascii=False, default=str))
'''


def _posix_limits(memory_mb: int, file_mb: int, seconds: float):
    """자식 프로세스가 시작되기 직전에 **커널에** 한계를 건다(POSIX).

    파이썬 안에서 거는 것과 다르다. `setrlimit` 은 커널이 집행하므로 스크립트가
    무슨 수를 써도 넘길 수 없다. 감사 훅은 파이썬 문법을 지나가는 것만 보는데,
    메모리 할당은 문법을 어기지 않는다.
    """
    import resource

    def apply() -> None:
        import os as _os

        if memory_mb:
            cap = memory_mb * 1024 * 1024
            resource.setrlimit(resource.RLIMIT_AS, (cap, cap))
        if file_mb:
            cap = file_mb * 1024 * 1024
            resource.setrlimit(resource.RLIMIT_FSIZE, (cap, cap))
        if seconds:
            # 벽시계 제한(`subprocess timeout`)과 별개다. 저쪽은 우리가 기다리다
            # 죽이는 것이고 이쪽은 커널이 CPU 를 재서 죽인다 - 우리가 못 죽이는
            # 상황(우리가 먼저 죽는 등)에서도 선다.
            cpu = max(1, int(seconds) + 1)
            resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu))
        try:
            _os.setsid()      # 자식의 자식까지 한 무리로 묶는다
        except OSError:
            pass

    return apply


def _windows_job(memory_mb: int, allow_process: bool):
    """윈도우 Job Object. 커널이 메모리와 프로세스 수를 집행한다.

    `KILL_ON_JOB_CLOSE` 가 핵심이다 - 우리가 죽어도 자식이 안 남는다. 실측으로
    `os.startfile` 이 띄운 프로세스가 임시 폴더를 붙잡아 뒷정리를 막았는데,
    그 부류가 여기서 끝난다.

    못 만들면 `None` 을 준다. 한계를 못 걸어도 툴은 돌아야 한다 - 여기서 죽으면
    본말이 뒤집힌다(한계는 안전장치이지 기능이 아니다).
    """
    import ctypes
    from ctypes import wintypes

    JOB_OBJECT_LIMIT_JOB_MEMORY = 0x00000200
    JOB_OBJECT_LIMIT_ACTIVE_PROCESS = 0x00000008
    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
    JobObjectExtendedLimitInformation = 9

    class IO_COUNTERS(ctypes.Structure):
        _fields_ = [(n, ctypes.c_ulonglong) for n in
                    ("ReadOperationCount", "WriteOperationCount",
                     "OtherOperationCount", "ReadTransferCount",
                     "WriteTransferCount", "OtherTransferCount")]

    class BASIC(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64),
                    ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", wintypes.DWORD),
                    ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t),
                    ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.POINTER(ctypes.c_ulong)),
                    ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD)]

    class EXTENDED(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", BASIC),
                    ("IoInfo", IO_COUNTERS),
                    ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t),
                    ("PeakJobMemoryUsed", ctypes.c_size_t)]

    try:
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        job = kernel.CreateJobObjectW(None, None)
        if not job:
            return None
        info = EXTENDED()
        flags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if memory_mb:
            info.JobMemoryLimit = memory_mb * 1024 * 1024
            flags |= JOB_OBJECT_LIMIT_JOB_MEMORY
        if not allow_process:
            # 자식 프로세스를 **커널이** 막는다. 감사 훅은 파이썬을 지나가는 것만
            # 보는데, 여기서는 어떤 경로로 만들든 하나를 넘길 수 없다.
            info.BasicLimitInformation.ActiveProcessLimit = 1
            flags |= JOB_OBJECT_LIMIT_ACTIVE_PROCESS
        info.BasicLimitInformation.LimitFlags = flags
        ok = kernel.SetInformationJobObject(
            job, JobObjectExtendedLimitInformation,
            ctypes.byref(info), ctypes.sizeof(info))
        if not ok:
            kernel.CloseHandle(job)
            return None
        return kernel, job
    except Exception:
        return None      # 한계를 못 걸어도 툴은 돌아야 한다


def _disk_watchdog(process, workdir, cap_mb: int):
    """작업 폴더가 한계를 넘으면 죽인다. 반환값은 **멈추는 함수**.

    커널이 거는 것보다 약하다 - 재는 사이에 조금 더 쓸 수 있다. 그러나 "무제한"과
    "0.25초어치"는 다르고, 그 차이가 기계가 멈추느냐 마느냐를 가른다.

    별도 스레드로 도는 이유: 부모는 `communicate()` 에서 막혀 있다. 재는 일은
    싸다(작은 폴더의 `scandir` 한 바퀴).
    """
    import threading

    cap = cap_mb * 1024 * 1024
    stop = threading.Event()
    # 죽였으면 **왜** 죽였는지 남긴다. 프로세스를 죽이면 stderr 가 비어서
    # 종료코드만 남고, 사용자는 코드를 아무리 고쳐도 이유를 모른다.
    killed: list[str] = []

    def size_of(root) -> int:
        total = 0
        stack = [str(root)]
        while stack:
            try:
                with os.scandir(stack.pop()) as entries:
                    for entry in entries:
                        try:
                            if entry.is_dir(follow_symlinks=False):
                                stack.append(entry.path)
                            else:
                                # `entry.stat()` 이 아니라 **실제 stat** 이다.
                                # 윈도우는 파일이 닫히기 전까지 디렉터리 항목의
                                # 크기를 갱신하지 않아, 쓰는 중인 파일이 계속 0 으로
                                # 보인다. 실측: 같은 순간에 scandir 0MB · 실제
                                # 240MB 를 봤다. 그래서 감시자가 한 번도 안 물었다.
                                total += os.path.getsize(entry.path)
                        except OSError:
                            continue
            except OSError:
                continue
        return total

    def watch() -> None:
        while not stop.wait(0.25):
            if process.poll() is not None:
                return
            used = size_of(workdir)
            if used > cap:
                killed.append(
                    f"작업 폴더가 {used / 1048576:.0f}MB 가 되어 멈췄습니다"
                    f"(상한 {cap_mb}MB). 큰 파일이 필요하면 정책의 "
                    f"max_output_mb 를 올리세요.")
                process.kill()
                return

    thread = threading.Thread(target=watch, daemon=True)
    thread.start()

    def finish() -> str:
        """멈추고, 죽였다면 그 사유를 준다.

        **끝난 뒤에도 한 번 잰다.** 0.25초마다 재는 감시자는 그 사이에 끝나는
        폭주를 통째로 놓친다 - 실측으로 400MB 쓰기가 0.4초에 끝났고, 폴링이
        한 번도 안 물린 판에서는 도구가 그대로 성공했다. 물어야 할 것은 "쓰는
        중에 잡았는가"가 아니라 **"이 도구가 상한을 넘겼는가"** 이고, 그건
        끝난 뒤에도 알 수 있다 - 쓴 것이 그대로 남아 있으니까.
        """
        stop.set()
        thread.join(timeout=1.0)
        if killed:
            return killed[0]
        used = size_of(workdir)
        if used > cap:
            return (f"작업 폴더가 {used / 1048576:.0f}MB 가 되었습니다"
                    f"(상한 {cap_mb}MB). 큰 파일이 필요하면 정책의 "
                    f"max_output_mb 를 올리세요.")
        return ""

    return finish


class _Done:
    """`subprocess.run` 이 주는 것과 같은 모양. 부르는 쪽이 안 바뀌게."""

    def __init__(self, returncode, stdout, stderr):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _run_confined(argv, stdin_text: str, *, timeout, cwd, env, policy, **extra):
    """한계를 걸고 돌린다. `subprocess.run` 과 같은 결과를 준다.

    윈도우에서 `Popen` 을 쓰는 이유: Job Object 는 만들기만 해서는 효력이 없고
    **프로세스를 붙여야** 한다. `subprocess.run` 은 끝난 뒤에 돌아오므로 붙일
    대상이 없다.

    붙이기 전 틈은 파이썬 인터프리터 시작(수십 ms)뿐이고 툴 코드는 그 뒤에 돈다.
    POSIX 는 `preexec_fn` 이라 그 틈조차 없다 - 자식이 시작되기 전에 걸린다.
    """
    job = None
    if os.name != "posix":
        job = _windows_job(policy.max_memory_mb, bool(policy.allow_process))
    process = subprocess.Popen(
        argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True, encoding="utf-8",
        cwd=cwd, env=env, **extra)
    if job:
        kernel, handle = job
        try:
            kernel.AssignProcessToJobObject(handle, int(process._handle))
        except Exception:
            pass          # 한계를 못 걸어도 툴은 돌아야 한다
    # POSIX 는 `RLIMIT_FSIZE` 로 커널이 파일 크기를 막는다. 윈도우에는 그에
    # 대응하는 프로세스 단위 한계가 없어서(볼륨 쿼터는 관리자 권한·폴더 단위 아님)
    # **부모가 지켜본다.** 실측: 200MB 를 0.4초에 썼다 - 제한시간 10초면 5GB 다.
    watchdog = None
    if os.name != "posix" and policy.max_output_mb:
        watchdog = _disk_watchdog(process, cwd, policy.max_output_mb)
    out = err = ""
    try:
        out, err = process.communicate(stdin_text, timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        process.communicate()
        raise
    finally:
        # 감시자가 죽였으면 **왜** 죽였는지 받아 온다. 프로세스를 죽이면 stderr 가
        # 비어서 종료코드만 남고, 사용자는 코드를 아무리 고쳐도 이유를 모른다.
        # 실측: 상한을 넘겨 막혔는데 화면에 "(사유 없음)" 이 나왔다.
        why = watchdog() if watchdog else ""
        if not why and process.returncode not in (0, None) and err:
            # POSIX 는 커널이 `RLIMIT_FSIZE` 로 막고 "File too large" 만 남긴다.
            # 무엇을 올리면 되는지는 커널이 모른다 - 우리가 안다. 플랫폼마다
            # 안내가 다르면 사용자는 같은 실패에 대해 다른 것을 배우게 된다.
            if "File too large" in err or "Errno 27" in err:
                why = (f"작업 폴더 크기 상한({policy.max_output_mb}MB)을 넘겼습니다. "
                       "큰 파일이 필요하면 정책의 max_output_mb 를 올리세요.")
            elif "MemoryError" in err:
                why = (f"메모리 상한({policy.max_memory_mb}MB)을 넘겼습니다. "
                       "더 필요하면 정책의 max_memory_mb 를 올리세요.")
        if why:
            err = (err + chr(10) + why).strip()
            # **사유만 있고 실패가 없으면 안 된다.** 폭주가 두 폴링 사이에 끝나면
            # 프로세스는 0 으로 나가고, 우리는 상한을 넘긴 것을 알면서도 그대로
            # 성공으로 넘겼다(실측: 400MB 를 쓴 도구가 통과했다). 상한을 넘겼다는
            # 사실이 판정을 바꿔야 사유를 적는 뜻이 있다.
            if process.returncode in (0, None):
                process.returncode = 1
        if job:
            # 잡을 닫으면 `KILL_ON_JOB_CLOSE` 가 남은 자식까지 정리한다. 실측으로
            # 살아남은 자식이 임시 폴더를 붙잡아 뒷정리를 막았던 그 문제다.
            job[0].CloseHandle(job[1])
    return _Done(process.returncode, out, err)


def run_tool(script: str, payload: dict, timeout: float | None = None,
             policy: ToolPolicy | None = None) -> ToolRun:
    """툴 한 번 실행 - 임시 디렉터리, 시간 제한, 환경변수는 허락한 것만.

    환경변수를 비우는 이유: 스크립트가 실수로든 아니든 API 키를 읽어 밖으로 보낼 수
    없게 한다. 키가 정말 필요하면 `policy.env_allowlist` 에 **그 이름만** 적는다.

    **쓰기 경계는 실행 중에도 지킨다**(`_GUARD`). 정적 검사는 이름을 보지만 런타임은
    닿는 지점을 보므로, `os.open` 처럼 목록에 없는 길로 와도 같은 관문을 지난다 -
    실측으로 그 길이 뚫려 임시 디렉터리 밖에 파일이 생겼다.

    다만 `allow_write` 를 **허락하면 경계는 없다.** 허락은 허락이고, 정산서를 사용자가
    고른 경로에 떨구는 것이 그 툴의 목적일 수 있다. 여기서 지키는 것은 "허락 안 한
    툴이 몰래 쓰지 않는 것"이지 "허락한 툴을 가두는 것"이 아니다. (예전 주석은
    "허락해도 임시 디렉터리 안" 이라고 했는데 사실이 아니었다.)
    """
    import time

    policy = policy or ToolPolicy()
    timeout = policy.timeout if timeout is None else timeout
    reason = safety_scan(script, policy)
    if reason:
        return ToolRun(ok=False, error=f"안전 검사 실패: {reason}")

    # 뒷정리 실패에 죽지 않는다. 툴이 띄운 프로세스가 폴더를 붙잡고 있으면
    # 지울 수 없는데, 그 예외가 올라오면 사용자는 `ToolRun` 을 못 받는다.
    # 툴이 실패한 것과 뒷정리가 실패한 것은 다르다 - 실측: `os.startfile` 이
    # 띄운 프로세스 때문에 `run_tool` 자체가 크래시했다.
    with tempfile.TemporaryDirectory(prefix="jermes-tool-",
                                     ignore_cleanup_errors=True) as workdir:
        path = Path(workdir) / "tool.py"
        # 실행 중 경계 감시를 심는다. 정적 검사는 이름을 보지만 런타임은
        # **닿는 지점**을 본다 - `os.open` 이든 `open` 이든 같은 관문을 지난다.
        guard = _GUARD.format(roots=[workdir], allow={
            "write": bool(policy.allow_write),
            "delete": bool(policy.allow_delete),
            "network": bool(policy.allow_network),
            "process": bool(policy.allow_process),
            "dynamic": bool(policy.allow_dynamic)})
        path.write_text(_RUNNER.format(guard=guard, body=script),
                        encoding="utf-8")
        env = {"PATH": os.environ.get("PATH", ""), "PYTHONIOENCODING": "utf-8",
               "SYSTEMROOT": os.environ.get("SYSTEMROOT", "")}
        for key in policy.env_allowlist:
            if key in os.environ:
                env[key] = os.environ[key]
        started = time.time()
        try:
            # 파이썬 안의 검사(정적·감사훅)로는 자원 소모를 못 막는다.
            # 여기서는 **커널에** 건다 - 스크립트가 무슨 수를 써도 못 넘긴다.
            extra: dict = {}
            if os.name == "posix":
                extra["preexec_fn"] = _posix_limits(
                    policy.max_memory_mb, policy.max_output_mb, timeout)
            completed = _run_confined(
                [sys.executable, str(path)],
                json.dumps(payload, ensure_ascii=False),
                timeout=timeout, cwd=workdir, env=env, policy=policy, **extra)
        except subprocess.TimeoutExpired:
            return ToolRun(ok=False, error=f"시간 초과({timeout:g}s)",
                           seconds=time.time() - started)
        elapsed = time.time() - started
        if completed.returncode != 0:
            # traceback 은 **끝줄**이 본론이다(`KeyError: 'missing'`). 앞에서 자르면
            # 임시 디렉터리 경로만 남고 정작 원인이 날아가 고칠 수가 없다.
            stderr = (completed.stderr or "").strip()
            return ToolRun(ok=False, seconds=elapsed,
                           error=stderr if len(stderr) <= 400 else "…" + stderr[-400:])
        raw = (completed.stdout or "").strip()
        try:
            return ToolRun(ok=True, output=json.loads(raw) if raw else None,
                           seconds=elapsed)
        except ValueError:
            return ToolRun(ok=False, error=f"JSON 이 아님: {raw[:200]}", seconds=elapsed)


def verify_tool(script: str, cases: list[ToolCase],
                config: GateConfig | None = None,
                timeout: float | None = None,
                policy: ToolPolicy | None = None) -> ToolReport:
    """**실행해서** 검증한다. 스킬 게이트와 같은 dev/holdout 분할을 쓰되, 채점에
    LLM 이 개입하지 않는다 - 입력을 넣고 출력을 비교할 뿐이라 결정적이다."""
    config = config or GateConfig()
    policy = policy or ToolPolicy()
    report = ToolReport()
    reason = safety_scan(script, policy)
    if reason:
        report.rejected = reason
        return report
    if len(cases) < config.min_cases:
        report.failures.append(f"케이스 {len(cases)}개 < 최소 {config.min_cases}")
        return report

    # 초안 작성기가 본 것과 **같은 분할**을 써야 한다. 각자 갈라 보면 "감춘 것으로
    # 확인했다"는 말이 성립하지 않는다.
    _, held = split_cases(cases, config)
    held_ids = {c.case_id for c in held}
    for case in cases:
        holdout = case.case_id in held_ids
        if holdout:
            report.holdout_total += 1
        else:
            report.dev_total += 1
        result = run_tool(script, case.payload, timeout, policy)
        passed, why = case.judge(result.output) if result.ok else (False, "")
        if passed:
            report.passed += 1
            if holdout:
                report.holdout_pass += 1
            else:
                report.dev_pass += 1
        else:
            report.failed += 1
            # 입력을 같이 적는다 - 무엇이 틀렸는지 없이 "틀렸다"만 주면 고칠 수 없다.
            # (holdout 실패는 프롬프트로 되먹이지 않으므로 여기에 실려도 새지 않는다.)
            detail = result.error or (
                f"입력 {json.dumps(case.payload, ensure_ascii=False)} → {why}")
            report.failures.append(f"{case.case_id}: {detail}")
    return report


def synthesize_tool_skill(name: str, description: str, script: str,
                          report: ToolReport,
                          provenance: Provenance | None = None,
                          policy: ToolPolicy | None = None,
                          cases: list["ToolCase"] | None = None) -> SkillDef:
    """검증 결과를 달고 원장에 들어갈 모양으로 접는다.

    본문에 **스크립트 원문**을 담는다 - 나중에 사람이 읽고 판단할 수 있어야 한다.
    """
    policy = policy or ToolPolicy()
    body = json.dumps({
        "name": name,
        "description": description,
        "entrypoint": "scripts/tool.py",
        "contract": "stdin JSON -> stdout JSON, def run(payload: dict) -> Any",
        "verification": report.summary(),
        "permissions": policy.describe(),
        # 사람이 읽을 문장(`permissions`)과 **기계가 되살릴 구조**(`policy`)를 따로 둔다.
        # 문장에서 권한을 되짚으려 하면 문구가 바뀌는 순간 조용히 틀린 권한으로 돈다.
        "policy": policy.to_dict(),
        "annotations": policy.annotations(),
        # **증명했는가**를 같이 남긴다. 빈 문자열이면 순수 계산임을 증명했다는
        # 뜻이고, 내용이 있으면 그 이유로 우리가 **모른다**는 뜻이다.
        # 나중에 코드를 다시 파싱하지 않아도 되고, 패키지로 나갈 때 받는 쪽도
        # 같은 판정을 본다.
        "purity": purity_scan(script),
        # 검증 케이스를 **툴 안에** 넣는다 - 그래야 나중에 아무 때나 회귀검사를 할 수
        # 있고, 받는 쪽도 "이게 맞다"를 자기 손으로 확인할 수 있다. 증거가 따라다닌다.
        "cases": [c.to_dict() for c in (cases or [])],
        "script": script,
    }, ensure_ascii=False, indent=2)
    skill = SkillDef(name=name, kind="tool", scope="user", description=description[:200],
                     body=body, provenance=provenance or Provenance(origin="tool_forge"),
                     meta={"verdict": report.verdict, "passed": report.passed,
                           "failed": report.failed})
    skill.verified = report.verdict == "promoted"
    skill.status = "active" if skill.verified else "staged"
    return skill


def tool_package(skill: SkillDef, *, evidence: dict | None = None,
                 license_name: str = "") -> dict[str, str]:
    """agentskills.io 표준 패키지 - `scripts/` 가 표준이 정한 실행 코드 자리다.
    즉 **다른 에이전트가 그대로 가져다 실행**할 수 있다."""
    from .portable import to_skill_md

    manifest = json.loads(skill.body)
    script = manifest.get("script", "")
    if not script:
        raise ValueError("툴 매니페스트에 script 가 없습니다")
    marks = {"tool-verification": manifest.get("verification", ""),
             "entrypoint": manifest.get("entrypoint", "scripts/tool.py")}
    marks.update(evidence or {})
    return {
        f"{skill.name}/SKILL.md": to_skill_md(skill, evidence=marks,
                                              license_name=license_name),
        # 내보낼 때는 감시를 안 심는다. 경계는 **돌리는 쪽**이 정하는 것이고,
        # 우리 임시 디렉터리 경로를 남의 패키지에 박아 보낼 수는 없다.
        f"{skill.name}/scripts/tool.py": _RUNNER.format(guard="", body=script).lstrip(),
    }


_TOOL_PROMPT = """You write a small, dependency-free Python function for a reusable tool.

Contract (obey exactly):
- Define `def run(payload: dict)`. It returns a JSON-serialisable value.
- Standard library only. No imports of subprocess/socket/urllib/requests.
- No file writes, no deletes, no network, no shell. Pure computation on `payload`.
- Deterministic: same payload -> same result.

Task:
{task}

It must satisfy every example (payload -> expected output):
{examples}

{feedback}Respond with ONLY the Python code for `run` (no prose, no code fences)."""

_FEEDBACK = """Your previous attempt failed these checks:
{failures}

Fix the code so every example passes.

"""


def _strip_fences(text: str) -> str:
    text = re.sub(r"^```(?:python)?|```$", "", (text or "").strip(), flags=re.MULTILINE)
    return text.strip()


def split_cases(cases: list[ToolCase], config: GateConfig | None = None
                ) -> tuple[list[ToolCase], list[ToolCase]]:
    """dev / holdout. **가르는 규칙은 `gate.split_holdout` 한 곳에만 있다** -
    툴과 스킬이 서로 다르게 가르면 "감춘 것으로 확인했다"가 조용히 거짓이 된다."""
    return split_holdout(cases, (config or GateConfig()).holdout_ratio)


def draft_tool(complete, task: str, cases: list[ToolCase], *,
               max_attempts: int = 3, config: GateConfig | None = None,
               timeout: float | None = None,
               policy: ToolPolicy | None = None) -> tuple[str, ToolReport, list[str]]:
    """LLM 이 스크립트를 쓰고 → **실제로 돌려보고** → 실패하면 그 오류를 보여주고
    다시 쓰게 한다. 문서를 쓰는 것과 달리 여기서는 채점이 결정적이라, 약한 모델도
    시행착오만 몇 번 하면 맞는 답에 도달한다(그리고 못 하면 그냥 못 한 것이다).

    [주의] **holdout 은 프롬프트에 절대 넣지 않는다** - 예시로도, 실패 보고로도. 라이브에서
    확인한 결함: 예시를 `cases[:6]` 으로 잘라 주고 실패도 전부 되먹였더니 모델이
    holdout 을 보고 맞추게 되어, 게이트가 막으려던 과최적화를 초안 작성기가 스스로
    뚫고 있었다. 못 본 문제를 못 풀면 그건 **일반화 실패**이고 그대로 거절해야 맞다.

    반환: (스크립트, 마지막 검증 결과, 시도 기록)
    """
    dev, held = split_cases(cases, config)
    if not dev or not held:
        # **왜 갈리지 않는지까지 말한다.** 케이스 파일이 잘못돼 한 줄만 읽힌
        # 경우가 흔한데, 화면에는 갈림의 결과만 떴다. 사용자는 자기 CSV 가
        # 문제였다는 것을 알 길이 없었다.
        return "", ToolReport(
            rejected=f"케이스가 dev/holdout 으로 갈리지 않습니다 "
                     f"(읽은 케이스 {len(cases)}건 · 최소 2건이 필요하고, "
                     f"검증됨을 받으려면 "
                     f"{(config or GateConfig()).min_cases}건이 필요합니다)"), []
    examples = "\n".join(
        f"- {json.dumps(c.payload, ensure_ascii=False)} -> {json.dumps(c.expect, ensure_ascii=False)}"
        for c in dev)
    dev_ids = {c.case_id for c in dev}
    attempts: list[str] = []
    script, report = "", ToolReport()
    feedback = ""
    for attempt in range(1, max_attempts + 1):
        prompt = _TOOL_PROMPT.format(task=task, examples=examples, feedback=feedback)
        try:
            script = _strip_fences(complete(prompt))
        except Exception as exc:
            attempts.append(f"{attempt}회: 호출 실패 {type(exc).__name__}")
            # **일시적인 실패면 남은 시도를 버리지 않는다.** 재시도가 있어야 할
            # 자리가 정확히 여기다. 실측: 도구 6개를 단조하는 중에 타임아웃이
            # 한 번 나서 `--attempts 3` 을 준 도구가 1회로 끝나고 staged 로
            # 떨어졌다 - 모델이 못 쓴 것이 아니라 물어보지도 못한 것이다.
            # 죽은 엔드포인트에 매달리는 것은 예산이 막는다(실패도 센다).
            from .drafter import _worth_retrying
            if _worth_retrying(exc) and attempt < max_attempts:
                continue
            break
        report = verify_tool(script, cases, config=config, timeout=timeout,
                             policy=policy)
        attempts.append(f"{attempt}회: {report.summary()}")
        if report.verdict == "promoted":
            break
        if report.rejected:
            problems = [report.rejected]
        else:
            # dev 실패만 되먹인다. holdout 실패는 사람이 볼 보고서에만 남는다.
            problems = [f for f in report.failures
                        if f.split(":", 1)[0] in dev_ids][:4]
            if not problems:
                # 왜 멈추는지에 더해 **무엇을 하면 되는지**도 말한다. 이유만
                # 말하면 사용자는 같은 명령을 다시 칠 뿐이고, 결과는 같다.
                attempts.append("  (dev 는 다 맞았고 holdout 에서 틀렸습니다 - "
                                "일반화 실패이므로 답을 알려주지 않고 멈춥니다. "
                                "빠진 경우를 dev 케이스로 늘려 `--cases` 로 다시 "
                                "재우세요)")
                break
        feedback = _FEEDBACK.format(failures="\n".join(f"- {p}" for p in problems))
    return script, report, attempts


# ────────────────────────────────────────────── 회귀와 개선

def load_cases(skill: SkillDef) -> list[ToolCase]:
    """툴이 들고 다니는 자기 검증 케이스를 꺼낸다.

    `check`(파이썬 술어)는 직렬화할 수 없으므로 저장되지 않는다. 그래서 그 케이스는
    `match` 나 `expect` 로 내려앉거나, 아무 주장도 없으면 **탈락**한다 - 저장 못 한
    조건을 통과한 척하면 회귀검사가 거짓말이 된다.
    """
    try:
        raw = json.loads(skill.body).get("cases") or []
    except ValueError:
        return []
    return [ToolCase.from_dict(item, index) for index, item in enumerate(raw)
            if isinstance(item, dict)]


@dataclass
class ImproveResult:
    verdict: str = "unchanged"        # unchanged | repaired | still_broken | no_cases
    before: ToolReport = field(default_factory=ToolReport)
    after: ToolReport | None = None
    script: str = ""
    attempts: list[str] = field(default_factory=list)

    def summary(self) -> str:
        if self.verdict == "no_cases":
            return "케이스가 없어 회귀검사를 못 합니다"
        line = f"이전 {self.before.summary()}"
        if self.after is not None:
            line += f" → 이후 {self.after.summary()}"
        return f"{self.verdict} · {line}"


def improve_tool(skill: SkillDef, complete=None, *,
                 extra_cases: list[ToolCase] | None = None,
                 config: GateConfig | None = None,
                 policy: ToolPolicy | None = None,
                 max_attempts: int = 3) -> ImproveResult:
    """이미 있는 툴을 **다시 재고 필요하면 고친다**.

    두 가지를 한다.
      ① 회귀검사 - 지금도 통과하는가. 파이썬이 올라가거나 케이스가 늘면 깨질 수 있다.
      ② 고치기 - 깨졌으면 실패를 되먹여 다시 쓴다(`complete` 가 있을 때만).

    Hermes 의 GEPA 가 트레이스를 읽어 "왜 실패했는지"로 스킬을 진화시키는 것과 같은
    방향인데, 여기서는 **채점이 결정적**이라 사람 승인 없이도 안전하다. 고친 결과가
    이전보다 나쁘면 버린다 - 개선이라는 말은 숫자로 증명될 때만 쓴다.
    """
    manifest = json.loads(skill.body) if skill.body.strip().startswith("{") else {}
    script = manifest.get("script", "")
    cases = load_cases(skill) + list(extra_cases or [])
    result = ImproveResult(script=script)
    if not cases:
        result.verdict = "no_cases"
        return result

    result.before = verify_tool(script, cases, config=config, policy=policy)
    if result.before.verdict == "promoted":
        result.verdict = "unchanged"
        return result
    if complete is None:
        result.verdict = "still_broken"
        return result

    task = manifest.get("description") or skill.description
    fixed, after, attempts = draft_tool(complete, task, cases, max_attempts=max_attempts,
                                        config=config, policy=policy)
    result.attempts = attempts
    # **나빠졌으면 안 바꾼다.** 새로 쓴 게 더 못하면 그건 개선이 아니다.
    if fixed and after.passed > result.before.passed:
        result.script, result.after = fixed, after
        result.verdict = "repaired" if after.verdict == "promoted" else "still_broken"
    else:
        result.after = after
        result.verdict = "still_broken"
    return result


# ────────────────────────────────────────────── 케이스를 어디서 읽든 한 규칙으로

# 기대값 칸 이름의 관용들. 형식을 하나로 강제하면 사람이 파일을 만들다 지쳐서 안 쓴다.
EXPECT_KEYS = ("expect", "expected", "output", "result", "answer", "기대", "정답", "결과")


def _coerce(text: str):
    """CSV 칸은 전부 문자열로 온다. 숫자·불리언·JSON 은 원래 모양으로 되돌린다 -
    "7" 을 기대값으로 두면 7 을 내는 툴이 영원히 틀린 게 된다."""
    stripped = (text or "").strip()
    if stripped == "":
        return None
    try:
        return json.loads(stripped)
    except ValueError:
        return stripped


def _cases_from_rows(rows: list[dict]) -> list[ToolCase]:
    """{입력들…, 기대값} 평평한 행 목록 -> 케이스."""
    cases = []
    for index, row in enumerate(rows):
        key = next((k for k in row if str(k).strip().lower() in EXPECT_KEYS), None)
        if key is None:
            raise ValueError(
                f"기대값 칸을 못 찾았습니다(행 {index}). 칸 이름을 {EXPECT_KEYS[0]} 로 두거나 "
                f"{', '.join(EXPECT_KEYS[1:4])} 중 하나를 쓰세요.")
        cases.append(ToolCase.from_dict(
            {"payload": {k: v for k, v in row.items() if k != key},
             "expect": row[key]}, index))
    return cases


def read_cases(path: str | Path) -> list[ToolCase]:
    """케이스 파일 -> 케이스. **사람이 이미 가진 파일을 그대로 받는다.**

    ①  JSON  [{"payload": {...}, "expect": ...}, ...]     정식
    ②  JSON  [[{...}, 기대값], ...]                        짧게 쓰고 싶을 때
    ③  JSONL {"입력a": …, "expect": …} 한 줄에 하나
    ④  CSV   입력 칸들 + 기대값 칸(expect/정답/결과 …)

    새 형식을 더할 자리도 여기 한 곳이다. CLI 든 다른 호스트든 이걸 부른다 -
    파일 형식 처리가 갈라지면 어떤 경로로 넣었느냐에 따라 결과가 달라진다.
    """
    source = Path(path)
    if not source.exists():
        raise OSError(f"케이스 파일이 없습니다: {source}")
    suffix = source.suffix.lower()
    text = source.read_text(encoding="utf-8-sig")   # 엑셀이 붙이는 BOM 을 흘린다

    if suffix == ".csv":
        import csv

        return _cases_from_rows([{k: _coerce(v) for k, v in row.items() if k is not None}
                                 for row in csv.DictReader(text.splitlines())])
    if suffix in (".jsonl", ".ndjson"):
        return _cases_from_rows([json.loads(line) for line in text.splitlines()
                                 if line.strip()])

    raw = json.loads(text)
    if not isinstance(raw, list):
        raise ValueError("케이스 파일은 리스트여야 합니다.")
    cases: list[ToolCase] = []
    for index, item in enumerate(raw):
        if isinstance(item, dict) and "payload" in item:
            cases.append(ToolCase.from_dict(item, index))
        elif isinstance(item, dict):
            return _cases_from_rows(raw)          # 평평한 행 목록도 받아준다
        elif isinstance(item, (list, tuple)) and len(item) == 2:
            cases.append(ToolCase.from_dict(
                {"payload": item[0], "expect": item[1]}, index))
        else:
            raise ValueError(f"{index}번 케이스 모양을 알 수 없습니다: {item!r}")
    return cases
