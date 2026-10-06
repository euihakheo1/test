# 제때받기 실행과 포트폴리오 준비

Windows PowerShell 기준으로 API·worker·웹을 실행하고, vLLM만 WSL2 Ubuntu에서 실행한다.
실제 사용자 자료 없이도 저장소의 공개 의결서 전사 자료로 데모를 만들 수 있다. 전사 자료는
검증 전이며, 이 데모의 숫자를 실제 LLM 정확도로 소개하지 않는다.

## 1. 코드와 실행 환경

기존 작업 폴더는 `C:\test\jettae`다. 명령을 그 바깥 `C:\test`에서 실행하면 다른 Git 저장소를
사용하게 된다. 아래는 기존 폴더 기준이다. 다른 경로에 clone하면 이후의 `C:\test\jettae`와
WSL의 `/mnt/c/test/jettae`를 실제 경로로 바꾼다.

```powershell
git clone https://github.com/euihakheo1/test.git C:\test\jettae
cd C:\test\jettae
```

이미 코드가 있다면 다음부터 진행한다. uv와 Node.js 22.18 이상이 필요하다.

```powershell
cd C:\test\jettae
uv --version
node --version
uv sync --frozen --all-extras
cd frontend
npm ci
cd ..
```

uv가 PATH에 없지만 `C:\test\.tools\uv.exe`가 있다면 이 PowerShell 창에서 임시로 등록한다.
새 API·worker 창에도 같은 설정이 필요하다.

```powershell
$env:Path = "C:\test\.tools;" + $env:Path
```

## 2. 먼저 CPU 데모를 확인한다

`.env`가 이미 있으면 유지한다. 처음 실행하는 경우에만 예시를 복사한다.

```powershell
if (!(Test-Path .env)) { Copy-Item .env.example .env }
Remove-Item Env:JETTAE_ENV_FILE -ErrorAction SilentlyContinue
uv run jettae api migrate
uv run jettae demo run --out-dir var/demo
```

데모 명령이 표시하는 로컬 데모 계정으로 로그인한다. 이 단계의 목적은 DB·파서·계산 엔진·화면이
모델 서버 없이도 동작하는지 확인하는 것이다. LLM 호출은 아직 없다.

API, worker, 웹은 각각 새 터미널에서 실행한다. 세 프로세스 중 worker가 없으면 업로드·분석·Agent
작업이 대기 상태에 머문다.

**API 터미널**

```powershell
cd C:\test\jettae
uv run jettae api serve --host 127.0.0.1 --port 8000
```

**worker 터미널**

```powershell
cd C:\test\jettae
uv run jettae worker run
```

**웹 터미널**

```powershell
cd C:\test\jettae\frontend
$env:JETTAE_API_ORIGIN = "http://127.0.0.1:8000"
npm run dev -- --hostname 127.0.0.1
```

브라우저에서 `http://127.0.0.1:3000/login`을 연다. 웹은 `/api/v1`을 같은 출처로 호출하고 Next가
API로 연결한다. 웹의 `.env.local`은 Python의 `.env`와 별개다.

## 3. 실제 vLLM을 준비한다

기본 경로는 **NVIDIA GPU가 있는 Windows + WSL2 Ubuntu**다. WSL에서 `nvidia-smi`가 동작해야 하며,
선택한 vLLM 빌드와 호환되는 드라이버가 필요하다. 이 문서의 Qwen3 4B FP16 설정은 16GB급 GPU를
출발점으로 삼되, 실제 메모리 여유와 응답시간은 해당 장비에서 측정해야 한다. 이 검토 환경에는 GPU가
없어 모델 구동·처리량을 측정하지 않았다. NVIDIA GPU가 없는 노트북에서는 아래 GPU 경로가 동작하지 않는다.

모델은 `Qwen/Qwen3-4B-Instruct-2507`, revision은 `cdbee75f17c01a7cc42f958dc650907174af0554`로
고정했다. Apache-2.0 모델이며 프로젝트 소스의 이용 조건과는 별개다. 모델 다운로드에는 수 GB의
디스크와 인터넷이 필요하고, vLLM의 CUDA/PyTorch 의존성도 별도 공간을 사용한다.

**PowerShell에서 비밀 설정 생성(한 번)**

```powershell
cd C:\test\jettae
uv run python deploy/init_env.py local
```

`.env.vllm`을 만들고 키를 화면에 출력하지 않는다. 이미 파일이 있으면 덮어쓰지 않으며, 기존 파일을
사용하면 된다. API와 vLLM은 이 파일의 같은 키를 사용한다. 유료 API 키는 필요 없다.

**WSL Ubuntu 터미널에서 설치**

WSL에도 uv가 있어야 한다([공식 설치 안내](https://docs.astral.sh/uv/getting-started/installation/)).
Windows와 Linux의 Python 가상환경을 공유하지 않도록 아래 경로를 분리한다.

```bash
cd /mnt/c/test/jettae
nvidia-smi
export UV_PROJECT_ENVIRONMENT="$HOME/.venvs/jettae-wsl"
uv sync --frozen --all-extras
uv venv --python 3.12 "$HOME/.venvs/jettae-vllm"
uv pip install --python "$HOME/.venvs/jettae-vllm/bin/python" "vllm==0.30.0"
```

**WSL 모델 서버 터미널**

```bash
cd /mnt/c/test/jettae
export UV_PROJECT_ENVIRONMENT="$HOME/.venvs/jettae-wsl"
export JETTAE_ENV_FILE=.env.vllm
export JETTAE_VLLM_BIN="$HOME/.venvs/jettae-vllm/bin/vllm"
uv run python deploy/serve_vllm.py
```

모델 로딩이 끝날 때까지 기다린다. 서버는 `127.0.0.1:8001`에서만 듣는다. GPU 메모리를 줄이려면
`JETTAE_VLLM_CONTEXT`를 낮출 수 있지만, Agent 입력과 출력의 합계가 그 길이를 넘으면 요청이 실패한다.
가중치·revision을 바꿀 때는 `.env.vllm`의 `JETTAE_VLLM_MODEL`도 새 버전을 구분하는 이름으로 바꿔
이전 모델의 replay와 섞이지 않게 한다. 현재 어댑터는 텍스트 모델용이며 스캔 PDF용 VLM OCR은 포함하지 않는다.

## 4. API와 worker를 로컬 LLM 모드로 다시 실행한다

기존 API·worker를 각각 Ctrl+C로 종료한다. 새 프로세스가 설정 파일을 읽어야 변경이 반영된다.
기본 dev 설정은 API 재시작 시 로그인 키가 바뀌므로, 화면에서 다시 로그인한다.

**API 터미널**

```powershell
cd C:\test\jettae
$env:JETTAE_ENV_FILE = ".env.vllm"
uv run jettae api serve --host 127.0.0.1 --port 8000
```

**worker 터미널**

```powershell
cd C:\test\jettae
$env:JETTAE_ENV_FILE = ".env.vllm"
uv run jettae worker run
```

**확인 터미널**

```powershell
cd C:\test\jettae
$env:JETTAE_ENV_FILE = ".env.vllm"
uv run python deploy/check_vllm.py
```

`from_cache=false`, 모델 id, 입력·출력 토큰, 응답시간, `api_charge_krw=0`을 확인한다. 이것은 실제 추론
연결과 JSON 응답 확인이며 문서 정확도 시험은 아니다. 모델 서버가 없으면 오류로 끝나며 오프라인으로
몰래 바꾸거나 상용 API를 부르지 않는다. Windows에서 WSL의 서버에 접속하지 못하면 WSL2의 localhost
forwarding 설정을 확인하거나 API·worker·웹을 모두 WSL 안에서 실행한다. 모델 서버를 공용 IP로 열지 않는다.

## 5. 화면에서 단일·역할 분담 Agent를 실행한다

1. 결과 목록에서 한 거래의 상세 화면을 연다. 기준일이 없는 거래는 필요 서류 안내를 보여 주기 좋다.
2. 실행 모드를 **로컬 LLM**, 방식을 **단일 Agent**로 고른 뒤 조사 실행을 누른다.
3. 완료 후 필요 서류·원문 근거·입력과 출력 토큰을 확인한다.
4. 같은 거래에서 **역할 분담 Agent**로 실행한다.
5. **조사 과정과 사용 모델**을 펼쳐 어떤 역할이 어느 도구를 실행했는지 비교한다.

기본 `JETTAE_LLM_LOCAL_CACHE=0`이므로 local 실행은 실제 추론을 요청한다. `replay`는 별도로 기록을
재생한다. 모델이 바로 종료하거나 제한에 도달할 수도 있으므로 `agent_status`, 도구 호출 수와 실패를
그대로 기록한다. 도구 호출 0건을 성공적인 도구 사용으로 소개하지 않는다.

현재 Agent는 **스키마로 제한한 행동 JSON → 서버 도구 실행 → 관측 → 다음 행동** 구조다. OpenAI
`tools`/`tool_calls` 네이티브 형식이나 Hermes Agent를 사용한 것으로 표현하지 않는다. MCP 서버는
별도로 구현되어 있으며 웹의 도구 호출이 MCP 네트워크 통신을 경유하는 것은 아니다.

## 6. 포트폴리오에 넣을 장면

| 순서 | 장면 | 보여 주는 능력 | 함께 기록할 것 |
|---|---|---|---|
| 1 | 정산서·입금 업로드와 항목 확인 | 서로 다른 문서의 구조화·열 매핑 | 입력이 공개 자료 전사인지 실제 사용자 자료인지 |
| 2 | 거래 연결·남은 차액·지연 계산 | 결정적 계산과 문서 연결 | 적용 규칙·기준일·계산 가정 |
| 3 | 기준일 누락 → 계산 보류·필요 서류 | 근거 부족을 처리하는 안전한 실패 | 추정 계산을 하지 않았다는 화면 |
| 4 | local Agent 완료와 펼친 조사 과정 | 실제 LLM·역할 분담·서버 도구 호출 | 모델 버전, 실제 호출/재생 여부, 토큰·시간·실패 |
| 5 | 추가 입금·정정 업로드 후 재검토 | 의존성 추적·버전 관리·승인 무효화 | 변경 전후 같은 거래의 결과와 승인 상태 |
| 6 | 검증 보고서·CI·테스트 링크 | 재현성·보안 통제·검증 범위 설명 | 통과 수와 미검증 영역을 함께 표기 |

결과를 먼저 보여 주고, 문제와 사용자 흐름 → 계산/LLM 역할 → 변경 처리 → 검증 순서로 5~7쪽을
구성한다. 구성 요소 이름만 나열하기보다 실제 화면에서 각 선택의 이유를 설명한다. 원문·입금자명·계좌·
토큰은 캡처에서 가린다. 실제 사용자 자료가 없다는 이유로 합성 평가 성능을 만들어 넣지 않는다.

## 7. 검사 명령

저장소 루트:

```powershell
uv run python scripts/secret_scan.py
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv run pytest -q
```

프런트엔드:

```powershell
cd frontend
npm ci
npm run lint
npm run typecheck
npm test
npm run build
npm run e2e:install
npm run e2e
npm audit --omit=dev
npm audit
cd ..
```

typecheck는 `next typegen`을 먼저 수행하므로 생성된 `.next`가 없어도 실행할 수 있다. E2E는 개발
DB와 별개인 폐기용 DB·API·worker·웹을 시작하고 유료 키와 local/live 설정을 물려받지 않는다.
PostgreSQL 검사는 Linux/WSL의 폐기 가능한 전용 환경에서만 실행한다:

```bash
uv run --with pgserver python deploy/run_pg_tests.py var/pgtest
```

Python 취약점 검사:

```powershell
uv export --quiet --frozen --all-extras --no-emit-project --format requirements-txt -o var/audit-requirements.txt
uvx pip-audit -r var/audit-requirements.txt --no-deps --disable-pip
```

결과 해석과 이번 실행의 제한은 [FINAL_VERIFICATION.md](FINAL_VERIFICATION.md)를 참고한다.

## 8. Linux 서버에 계속 운영하기

NVIDIA GPU, Docker Engine·Compose v2, NVIDIA Container Toolkit, 접속할 DNS 도메인이 필요하다.
DNS가 서버를 가리키고 80·443 포트가 열려 있어야 Caddy가 HTTPS 인증서를 발급한다. 공개하는 것은
웹·API이며 vLLM·DB는 내부망에 둔다. 무료인 것은 외부 LLM API 호출료다. GPU·전기·서버·도메인
운영비와 상시 가용성이 무료로 보장되는 것은 아니다.

아래 `app.example.com`은 보유한 실제 도메인으로 바꾼다. 설정 생성은 한 번만 수행한다.

```bash
uv sync --frozen --all-extras
uv run python deploy/init_env.py prod --domain app.example.com
cd deploy
docker compose -f docker-compose.yml -f compose.web.yml -f compose.vllm.yml config --quiet
docker compose -f docker-compose.yml -f compose.web.yml -f compose.vllm.yml up -d --build
docker compose -f docker-compose.yml -f compose.web.yml -f compose.vllm.yml ps
docker compose -f docker-compose.yml -f compose.web.yml -f compose.vllm.yml logs -f api worker vllm
```

`https://본인도메인/login`에서 새 운영 계정을 만든다. 운영 DB에 데모 계정을 설치하지 않는다.
이 Compose 경로는 PostgreSQL → migrate → API·worker, 웹, HTTPS 프록시, 내부 vLLM으로 구성된다.
GPU 없는 서버에서 규칙 기반 서비스만 배포하려면 `-f compose.vllm.yml`을 제외한다.

데이터 보존을 위해 업데이트는 같은 프로젝트·볼륨에서 `up -d --build`로 수행하며,
`down -v`는 DB·업로드·모델 캐시를 삭제하므로 운영 업데이트 명령으로 쓰지 않는다.
백업·복구·장애 처리·worker 증설은 [runbook.md](runbook.md)를 따른다. Docker·실제 HTTPS·GPU
실행을 이 검토 환경에서 확인했다는 뜻은 아니며, 첫 배포 때 위 순서를 서버에서 검증해야 한다.

## 9. GitHub에 반영한다

```powershell
cd C:\test\jettae
uv run python scripts/secret_scan.py
git status --short
git add .
git diff --cached --stat
git commit -m "Finish private vLLM runtime and release setup"
git push origin HEAD:main
```

현재 파일뿐 아니라 과거 커밋도 공개되므로 전체 이력 검사가 필요하다. `.gitignore`는 이미 추적된
파일을 자동 제거하지 않는다. 기존 라이선스 조건과 보안 제보 경로는 `docs/PUBLIC_USE.md`,
`SECURITY.md`를 유지했다.
