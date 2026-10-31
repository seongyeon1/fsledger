# fsledger

[![ci](https://github.com/seongyeon1/fsledger/actions/workflows/ci.yml/badge.svg)](https://github.com/seongyeon1/fsledger/actions/workflows/ci.yml)

> A local file ledger for your home folder. It tracks every add / modify / delete / move with
> git-style stat caching and DB-style temporal history, finds duplicates and junk, and organizes
> loose files — every move is journaled and reversible. Pure Python standard library, no dependencies.

홈 폴더의 파일 변화를 git·DB 방식으로 기록하고, 중복·불필요 파일을 찾아 정리를 제안하는 로컬 도구.

- **변화 추적**: 스캔할 때마다 스냅샷을 남기고 추가·수정·삭제·이동을 기록한다. 파일 하나의 버전 이력,
  특정 시점의 폴더 상태도 조회된다.
- **한눈에 보기**: 대시보드 HTML 하나에 변경 타임라인, 큰 폴더, 중복, 정리 후보, 정리 계획이 모인다.
- **중복·불필요 탐지**: 내용 해시로 중복을 찾고, OS 찌꺼기·캐시·미완료 다운로드·이미 푼 압축·안 쓰는
  `node_modules`/`.venv` 등을 safe / review 등급으로 제안한다.
- **자동 정리**: Downloads 같은 폴더의 낱개 파일을 카테고리·월별 폴더로 옮긴다.
- **안전장치**: 아무것도 바로 지우지 않는다. 모든 이동은 선기록 저널에 남고 `undo`로 되돌린다.
  git 저장소 안은 건드리지 않는다.

설계 근거는 [research/README.md](research/README.md)에 있다 (git stat 캐시·racy git, MVCC/SCD2, CDC, WAL,
fdupes식 단계 필터 — 일부는 [REA](https://github.com/morluto/rea)로 로컬 git·sqlite3 바이너리를
정적 검사해 확인).

## 설치

Python 3.11 이상, 외부 의존성 없음. macOS 기준으로 만들었고 스캔·정리는 Linux에서도 동작한다
(일일 스케줄만 macOS launchd 전용).

```bash
uv tool install git+https://github.com/seongyeon1/fsledger
# 또는
pipx install git+https://github.com/seongyeon1/fsledger
```

> **macOS 권한**: Desktop·Documents·Downloads는 개인정보 보호(TCC) 대상이다. 처음 실행할 때 접근 허용
> 창이 뜨면 허용하고, 스케줄 실행까지 쓰려면 *시스템 설정 > 개인정보 보호 및 보안 > 전체 디스크 접근 권한*에
> 터미널을 추가한다. 루트 폴더를 못 읽으면 fsledger는 스냅샷을 남기지 않고 중단한다
> (읽지 못한 걸 삭제로 기록하지 않기 위해서다).

## 하루 쓰는 흐름

```bash
fsledger scan --open        # 스캔 → 스냅샷 기록 → 대시보드(~/.fsledger/dashboard.html) 열기
fsledger status             # 스냅샷 목록과 변경 수
fsledger changes            # 마지막 스캔의 변경 (+추가 ~수정 -삭제 >이동)
fsledger history <파일>      # 파일 하나의 버전 이력
fsledger asof 3 ~/Downloads # 스냅샷 #3 시점의 파일 목록
```

## 정리

아무 명령도 파일을 바로 지우지 않는다. `--apply` 없이는 미리보기만 한다.

```bash
fsledger dupes                               # 중복 묶음 (맨 위가 남길 원본)
fsledger clean                               # safe 후보 미리보기
fsledger clean --apply                       # safe 후보를 ~/.fsledger/quarantine/<batch>/ 로 이동
fsledger clean --apply --level review --rule duplicate --rule extracted-archive
fsledger organize                            # 최상위 낱개 파일 → 카테고리 폴더 계획
fsledger organize --apply --root ~/Downloads # 실행
fsledger undo [batch]                        # 배치 되돌리기 (기본: 마지막)
fsledger recover                             # 중단된 작업 확정
fsledger purge --older-than 30 --yes         # 30일 지난 격리분만 영구 삭제
```

| 등급 | 규칙 |
|------|------|
| safe | `.DS_Store` 등 OS 메타데이터, AppleDouble 조각, Office 잠금 파일, 미완료 다운로드, `__pycache__`·`.next` 등 캐시 |
| review | 중복 사본(저장소 밖), 이미 푼 압축, 60일 안 쓴 프로젝트의 `node_modules`·`.venv`, 오래된 설치 파일·대용량 다운로드, 빈 파일 |

git 저장소 안의 파일은 중복 후보와 폴더 정리에서 빠진다.

## 자동 실행

```bash
fsledger schedule show            # launchd plist 미리보기
fsledger schedule install --hour 9
fsledger schedule remove
```

매일 지정 시각에 `fsledger auto`가 돈다: 중단 작업 복구 → 스캔 → `auto = true` 루트(기본: Downloads)
정리 → 대시보드 갱신. 삭제 후보는 자동으로 옮기지 않는다. 로그는 `~/.fsledger/auto.log`.

## 설정 (`~/.fsledger/config.toml`, 선택)

```toml
roots = ["~/Desktop", "~/Documents", "~/Downloads"]
dupe_min_size = 1024
organize_min_age_days = 2

[[organize]]
path = "~/Downloads"
group_by_month = true
auto = true

[[organize]]
path = "~/Desktop"
auto = false
```

## 데이터 모델

| 테이블 | 개념 | 내용 |
|--------|------|------|
| `snapshots` | git commit | 스캔 1회, `parent_id` 체인 |
| `file_versions` | SCD2 / MVCC + git index | `valid_from`·`valid_to`, stat 캐시 `(size, mtime_ns, ino, dev)`, 해시, racy |
| `changes` | CDC | added / modified / deleted / moved |
| `ops` | WAL | 이동 전 pending 기록 → done, undo·recover 근거 |
| `junk_dirs`, `repos` | .gitignore | 들어가지 않은 디렉터리 크기, git 저장소 루트 |

## 개발

```bash
uv sync
uv run pytest -q
uv run ruff check src tests && uv run ruff format --check src tests
uv run mypy
```

## 라이선스

MIT
