# 배포

Jermes 는 의존성이 0 이라 배포가 단순하다. 그래도 **올리기 전에 도는지 본다**.
빌드만 되고 실행이 안 되는 패키지는 흔하다.

## 레포가 둘인 이유

- **`jinsoo96/jermes-lab`** (비공개) = 정본. 전체 이력, 실험, 붙일 자리까지 다 있다.
  여기서 일하고 여기에 태그를 붙인다. PyPI 토큰도 여기 있다.
- **`jinsoo96/jermes`** (공개) = 엔진만 골라 담은 사본. `publish_public.py` 가
  매번 새로 만든다. 여기서는 `test.yml` 만 돌고 **발행하지 않는다**.

정본을 그대로 공개할 수 없어서 이렇게 나눴다. 정본에는 남의 코드와 사내 데이터가
들어 있고, 파일을 지우는 커밋을 얹어도 이력에 남으면 공개된 것이다.

## 한 번만 하는 준비

1. PyPI 에 이름 `jermes` 가 비어 있는지 확인한다(2026-08 기준 비어 있음).
   남이 쓰고 있으면 `pyproject.toml` 의 `name` 만 바꾸면 된다
   (import 이름 `jermes` 는 그대로 두어도 된다).
2. 발행 자격. **이미 되어 있다.** `jermes-lab` 의 GitHub Secret `PYPI_API_TOKEN`
   에 개인 PyPI 토큰이 들어 있고 `release.yml` 이 그것을 쓴다. 레포 파일에는
   토큰이 없다. 갈아끼울 일이 생기면 Settings > Secrets > Actions 에서 값만 바꾼다.

## 낼 때마다 하는 것

```bash
# 1. 손으로 먼저 확인 (CI 가 같은 것을 다시 한다)
python -m pytest -q
python smoke.py --llm

# 2. 판 올리기 - pyproject.toml 의 version 한 곳만 고친다
#    (0.1.0 -> 0.2.0)

# 3. 빌드하고 검사
python -m pip install build twine
python -m build
python -m twine check dist/*

# 4. 깨끗한 곳에 설치해서 진짜 도는지
python -m venv /tmp/probe && /tmp/probe/bin/pip install dist/*.whl
/tmp/probe/bin/jermes demo

# 5. 태그를 밀면 CI 가 같은 절차를 돌고 발행한다
git tag v0.2.0 && git push origin v0.2.0    # origin = jermes-lab
```

태그 없이 밀면 CI 는 빌드·검사까지만 하고 **발행하지 않는다.**

## 손으로 올리고 싶을 때

```bash
python -m twine upload dist/*          # 계정/토큰을 그 자리에서 묻는다
python -m twine upload --repository testpypi dist/*   # 먼저 시험해 보려면
```

## 판 번호

`pyproject.toml` 의 `version` 이 **유일한 정본**이다. 다른 곳에 적어 두면 언젠가
한쪽만 고쳐지고, 그때부터 어느 쪽이 맞는지 아무도 모른다.
