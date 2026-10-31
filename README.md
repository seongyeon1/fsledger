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
- **문서·데이터 허브**: 곳곳에 흩어진 문서·발표·스프레드시트·노트·데이터·노트북을 옮기지 않고
  한 페이지에 모은다. 프로젝트(git 저장소)별로 묶고 이름·경로로 검색한다.
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

## 문서·데이터 허브

```bash
fsledger serve               # 로컬 서버로 열기: 미리보기·앱에서 열기·삭제까지 (권장)
fsledger hub --open          # 파일로만 열기: ~/.fsledger/hub.html (scan·report 때도 갱신된다)
```

파일은 원래 자리에 그대로 두고, 마지막 스캔 기록에서 문서·데이터만 골라 색인한다.

- **범위**: 기본은 "내 문서"로 git 저장소 밖의 파일만 보여 준다. 코드 저장소 안의 README·노트는 "저장소 문서",
  둘 다 보려면 "전체"를 고른다.
- **본문 검색**: 검색창에 낱말을 넣으면 이름·경로와 함께 본문도 찾는다(회의 → "회의록을"처럼 앞부분 일치).
  마크다운·텍스트·워드·파워포인트·엑셀 글자·HWPX·노트북은 `~/.fsledger/search.db` 색인으로 찾고, 일치한 문장을
  강조해 보여 준다. PDF·HWP 등은 macOS Spotlight 결과를 보탠다. 색인은 스캔 뒤 바뀐 파일만 다시 읽고,
  지워도 다음 스캔 때 다시 만들어진다. 저장소 안 문서는 앞 32KB, 나머지는 64KB까지 넣는다.
- **판 묶기**: 같은 폴더의 `기획서_v2`, `기획서_최종`, `기획서 (1)`, `복사본`, 날짜만 다른 이름을 한 줄로 묶고
  가장 최근 판을 보여 준다. "판 N개"를 누르면 펼쳐진다.
- **중복**: 다른 폴더에 내용이 똑같은 파일이 있으면 "같은 파일 N곳" 배지가 붙고, 위쪽 "중복 있음"으로 그런
  파일만 볼 수 있다. 뷰어에서 다른 위치를 확인하고 "이 파일만 남기고 나머지 격리"로 한 번에 정리한다.
- **동기화**: `serve`가 켜져 있는 동안 15분마다(`rescan_minutes`) 다시 스캔하고, 오른쪽 위 "동기화"
  버튼으로 바로 맞출 수도 있다. 허브에서 지우거나 저장하면 그때도 스캔한다. 내용이 바뀌면 보던 폴더·필터를
  그대로 둔 채 목록을 새로 불러온다. 뷰어나 편집기가 열려 있으면 닫을 때까지 기다린다.
- **폴더 트리**: 왼쪽에서 루트부터 펼쳐 들어가면 본문이 Finder처럼 하위 폴더와 그 폴더의 파일을 보여 준다.
  위쪽 경로 표시와 상위 폴더 버튼으로 이동한다.
- **뒤로가기**: 폴더·필터·열어 본 파일이 주소(`#…`)에 남아서 브라우저 뒤로·앞으로·새로고침이 그대로 된다.
- **뷰어**: 파일 이름을 누르면 오른쪽에 열린다. PDF, 마크다운, 텍스트·JSON, CSV, 엑셀, 워드
  (macOS textutil), 파워포인트(슬라이드 글자), 노트북, 한글(HWP·HWPX), mermaid(`.mmd`·`.mermaid`, 마크다운 안의
  ```` ```mermaid ```` 블록 포함), Excalidraw를 미리 본다. `j`/`k`로 다음·이전 파일, `Esc`로 닫는다.
  iCloud에만 있는 파일은 읽지 않는다.
- **한글 편집**: `config.toml`에 `hwp_editor = true`를 넣으면 한글 파일 뷰어에 "편집" 버튼이 생긴다.
  저장하면 원본은 격리 폴더에 백업되고 알림의 되돌리기로 돌아간다.
- **삭제**: 파일은 뷰어의 삭제 버튼, 폴더는 폴더 줄의 휴지통이나 경로 표시 오른쪽 "이 폴더 삭제"로 지운다.
  바로 지우지 않고 격리 폴더로 옮기며, 알림의 되돌리기나 `fsledger undo`로 복구한다. 폴더는 확인 창에
  원장 기준 전체 파일 수·크기를 보여 주고, git 저장소가 걸려 있으면 폴더 이름을 입력해야 지워진다.
  추적 루트 자체, fsledger 데이터 폴더, 심볼릭 링크, 원장에 기록이 없는 폴더는 지울 수 없다.
- **격리함**: 오른쪽 위 "격리함"에서 지운 묶음(지운 시점 단위)을 크기·원래 경로와 함께 본다. 묶음마다
  "복원"으로 원래 자리에 돌려놓거나 "영구 삭제"로 디스크에서 지운다. "전체 비우기"는 `비우기`를 입력해야
  실행된다. 영구 삭제는 되돌릴 수 없다.

`fsledger serve`는 127.0.0.1 에서만 열리고, 실행할 때마다 새 토큰이 들어간 주소를 출력한다. 토큰과 Host
헤더가 맞지 않는 요청, 원장에 없는 경로나 문서·데이터가 아닌 파일 요청은 거부한다. `file://`로 연
페이지는 브라우저가 다른 파일 읽기를 막기 때문에 미리보기가 꺼진다.

한글·mermaid·Excalidraw는 브라우저에서 오픈소스 라이브러리로 그린다. 처음 열 때 jsDelivr에서
버전을 고정한 파일을 받고 무결성 해시(SRI)로 확인하므로 인터넷이 필요하다. 그리는 화면은
`sandbox="allow-scripts"` iframe이라 출처가 없어서, 허브 토큰이나 서버 API에 닿지 못한다.

| 형식 | 라이브러리 |
|---|---|
| HWP·HWPX | [rhwp](https://github.com/edwardkim/rhwp) `@rhwp/core@0.8.7` (MIT) |
| mermaid | `mermaid@11.4.1` (MIT), `securityLevel: "strict"` |
| Excalidraw | `@excalidraw/utils@0.1.2` (MIT) |

한글 편집기(`@rhwp/editor@0.8.7`)는 편집 화면을 rhwp 개발자 사이트(edwardkim.github.io/rhwp)에서 불러온다.
문서는 그 iframe으로 postMessage만 하고 서버로 올라가지 않지만, 그 화면 코드는 버전을 고정할 수 없다.
그래서 기본으로 꺼 두었고, 켜도 처음 열 때 한 번 더 확인한다. 고객 문서를 다룬다면 rhwp-studio를 직접 빌드해
쓰는 편이 안전하다.

- 종류: 문서(pdf·docx·hwp…), 발표, 스프레드시트, 노트(md·txt), 데이터(csv·parquet·jsonl…),
  노트북, 다이어그램. 저장소 안의 `.txt`·`.json`은 설정 파일이 대부분이라 뺀다.
- 묶음: 가장 가까운 git 저장소 → 저장소를 바로 아래에 둔 프로젝트 폴더 → 루트 바로 아래 폴더.
- `dist`·`build`·`site-packages`·venv·`.worktrees` 안은 빼고, `.claude` 같은 숨김 폴더 안은
  기본으로 숨긴다(화면에서 켤 수 있다).
- 위치 경로의 폴더 이름을 누르면 그 폴더 아래로 좁혀진다.

범위를 넓히려면 설정의 `roots`에 폴더를 더한다. iCloud Drive도 넣을 수 있다. 내려받지 않은
iCloud 파일은 해시를 위해 읽지 않으므로(읽으면 다운로드가 시작된다) 스캔해도 받아지지 않는다.

```toml
roots = [
  "~/Desktop", "~/Documents", "~/Downloads",
  "~/Library/Mobile Documents/com~apple~CloudDocs",  # iCloud Drive
  "~/workspace",
]
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
hwp_editor = false          # true 면 허브에서 한글 편집 (위 설명 참고)
rescan_minutes = 15         # serve 중 자동 스캔 간격(분), 0 이면 끔
purge_after_days = 0        # 매일 자동 실행 때 격리한 지 N일 지난 묶음을 영구 삭제, 0 이면 끔
fulltext = true             # 허브 본문 검색 색인(search.db), false 면 이름·경로로만 찾기

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

## 화면

대시보드와 허브는 외부 라이브러리 없는 단일 HTML 파일이다. 둘이 같은 디자인 토큰(`theme.css`)을 쓴다.
간격·반경·글자 크기는 [ai-design-skills](https://github.com/elayadesign/ai-design-skills)의
`landing-page-design` Part B 규칙을 따랐다. 글꼴 Geist는 Google Fonts에서 불러오고, 오프라인이면
시스템 글꼴로 대신 표시한다(파일 정보는 밖으로 나가지 않는다).

## 라이선스

MIT. 아이콘은 [Phosphor Icons](https://phosphoricons.com) (MIT).
