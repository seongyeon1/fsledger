# 버전관리·변화추적 방식 조사 — git과 전통 DB

로컬 파일 추적 도구(`fsledger`)를 설계하기 전에 git과 관계형 DB가 "무엇이 바뀌었는지"를
어떻게 알아내고 기록하는지 정리했다. 각 절 끝의 **→ fsledger** 는 실제로 가져온 설계 결정이다.

조사 근거는 세 가지다.

1. 공식 소스·문서 — git `read-cache.c`, `Documentation/technical/racy-git.txt`,
   `gitformat-index`, SQLite `wal.html`·`sessionintro.html`, PostgreSQL MVCC·logical decoding 문서
2. REA(`rea-agents` 6.8.0) 정적 검사 — 이 Mac에 설치된 바이너리를 직접 확인 (`rea-evidence/`)
3. 기존 중복 탐지 도구(fdupes·jdupes·rmlint)의 공개된 알고리즘

## 1. git — 콘텐츠 주소 + stat 캐시

### 1-1. 객체 모델: 내용이 곧 주소다

git은 파일 내용을 해시해서 `blob`으로 저장하고, 디렉터리는 `(이름, 모드, blob 해시)` 목록인
`tree`, 스냅샷은 `tree` 하나와 부모 커밋을 가리키는 `commit`으로 표현한다.
같은 내용이면 해시가 같으니 **중복은 저장 단계에서 자동으로 하나가 된다.**
변경 이력은 커밋 체인을 따라가며 두 tree를 비교(diff)해서 얻는다. 이력을 따로 저장하지 않는다.

→ **fsledger**: 파일 내용을 BLAKE2b-256으로 해시해 `content_hash`로 쓴다.
같은 해시를 가진 경로들이 곧 중복 그룹이다. 스캔 한 번이 커밋 하나(`snapshots`)가 되고,
`parent_id`로 이전 스냅샷을 가리킨다.

### 1-2. 인덱스의 stat 캐시: 해시를 매번 계산하지 않는 이유

`git status`가 수십만 파일에서도 빠른 이유는 내용을 다시 읽지 않기 때문이다.
인덱스(`.git/index`)에 각 파일의 `ctime, mtime, dev, ino, uid, gid, size`를 저장해 두고
(`fill_stat_cache_info`), 다음 번엔 `lstat()` 결과와 비교(`ie_match_stat`)만 한다.
값이 같으면 "안 바뀜"으로 보고 해시를 건너뛴다.

**racy git 문제**: mtime 해상도 안에서 인덱스 기록과 파일 수정이 동시에 일어나면,
stat은 같은데 내용은 다른 상황이 생긴다. git은 인덱스 파일 자체의 mtime보다
같거나 늦은 mtime을 가진 항목을 "racy"로 표시(`is_racy_timestamp`)하고, 다음 비교 때
내용을 실제로 다시 확인한다.

**fsmonitor**: 최근 git은 OS 파일 이벤트(macOS FSEvents)를 받는 데몬(`fsmonitor--daemon`)으로
"마지막 토큰 이후 바뀐 경로"만 받아 `lstat` 자체도 줄인다.

REA 정적 검사로 이 Mac의 Homebrew git 2.51.0 바이너리(arm64, sha256 `c6ea49f5…`)에서
아래 심볼을 실제로 확인했다 (`rea-evidence/git-macho.json`).

| 심볼 | 역할 |
|------|------|
| `_fill_stat_cache_info` | 인덱스에 stat 값 기록 |
| `_ie_match_stat` | 저장된 stat과 현재 `lstat` 비교 |
| `_is_racy_timestamp` | racy 항목 판정 |
| `_refresh_index` | stat 기반 일괄 갱신 |
| `_hash_object_file`, `_write_object_file` | 내용 해시 → 객체 저장 |
| `_fsmonitor_ipc__send_query`, `_fsmonitor_batch__add_path` 외 20여 개 | FSEvents 기반 변경 경로 수집 |
| `_hold_lock_file_for_update_timeout_mode`, `_commit_lock_file` | `index.lock`에 쓰고 rename으로 원자적 교체 |
| `_xdl_diff` | 내용 diff (xdiff) |

→ **fsledger**: 파일마다 `(size, mtime_ns, ino, dev)`를 저장한다. 다음 스캔에서 네 값이
같으면 해시를 건너뛴다. 스캔 시작 시각과 2초 이내로 가까운 mtime은 `racy=1`로 표시하고,
다음 스캔에서 stat이 같더라도 해시를 다시 확인한다. FSEvents 데몬은 범위 밖이다.
대신 하루 한 번 도는 launchd 스캔으로 충분하도록 stat 캐시를 쓴다.

### 1-3. rename 감지: 저장하지 않고 추론한다

git은 이름 변경을 기록하지 않는다. diff 시점에 "삭제된 파일"과 "추가된 파일"의 내용 유사도를
비교해 50% 이상이면 rename으로 **추론**한다(`-M`).

→ **fsledger**: 파일시스템은 git보다 단서가 하나 더 있다. 같은 볼륨 안에서 `mv`하면
inode가 유지된다. 그래서 1순위로 `(dev, ino)` 일치, 2순위로 `(size, content_hash)` 일치를
보고 삭제+추가 쌍을 `moved`로 묶는다.

### 1-4. ignore 규칙

`.gitignore`로 추적 제외 대상을 선언한다. `node_modules`, 빌드 산출물처럼 재생성 가능한 것은
버전관리 대상이 아니다.

→ **fsledger**: `node_modules`, `.venv`, `__pycache__`, `.git` 내부 등은 들어가서 추적하지 않는다.
다만 디렉터리 단위 크기는 재서 "불필요 후보"로 보고한다. git 저장소 루트를 만나면
`is_repo`로 표시하고, 그 안의 파일은 **폴더 정리 대상에서 제외**한다.
저장소 구조를 바꾸면 git 입장에선 대량 삭제·추가가 되기 때문이다.

## 2. 전통 DB — 로그를 먼저 쓰고, 행을 덮어쓰지 않는다

### 2-1. WAL (Write-Ahead Logging)

SQLite WAL 모드와 PostgreSQL WAL의 공통 원칙: **바꾸기 전에 무엇을 바꿀지 로그에 먼저 쓴다.**
중간에 죽어도 로그를 보고 재실행(redo)하거나 버릴 수 있다. 커밋은 로그에 커밋 레코드가
기록된 순간 성립한다.

REA 검사 결과 macOS 기본 `/usr/bin/sqlite3`는 SQLite를 정적으로 포함하고 있고
(`libSystem`, `libz`, `libedit`, `libncurses`만 링크), 문자열 테이블에 `-wal`, `journal_mode`,
`wal_checkpoint`, `ENABLE_PREUPDATE_HOOK` 컴파일 옵션이 남아 있다
(`rea-evidence/sqlite3-macho.json`). export 심볼은 strip돼서 함수 수준 확인은 불가능했다.
이 한계는 REA 결과의 `limitations`에도 적혀 있다.

→ **fsledger**: 파일 이동·격리 같은 **되돌리기 어려운 작업은 실행 전에 `ops` 테이블에
`pending`으로 먼저 기록**한다. 실제 이동이 끝나면 `done`으로 바꾼다. 중간에 죽으면
`pending` 상태가 남으므로 `fsledger recover`가 실제 파일 위치를 보고 정리한다.
`undo`는 이 기록을 거꾸로 재생한다. DB 자체도 `PRAGMA journal_mode=WAL`로 연다.

### 2-2. MVCC와 시간 테이블 (SCD Type 2, SQL:2011 temporal)

PostgreSQL은 UPDATE를 하면 기존 행을 덮어쓰지 않는다. 새 버전 행을 만들고 옛 행에
`xmax`(종료 트랜잭션)를 찍는다. 데이터 웨어하우스의 SCD Type 2와 SQL:2011
`SYSTEM_TIME` 기간 테이블은 이 생각을 사용자 수준으로 끌어올린 것이다.
행마다 `valid_from`, `valid_to`를 두고, 현재 상태는 `valid_to IS NULL`이다.
이렇게 하면 "지난주 화요일 기준 상태"도 질의할 수 있다.

→ **fsledger**: `file_versions(path, valid_from, valid_to, size, mtime_ns, content_hash, …)`.
파일이 바뀌면 현재 행의 `valid_to`를 이번 스냅샷 id로 닫고 새 행을 연다.
`fsledger history <path>`, `fsledger asof <snapshot>`이 이 테이블 하나로 동작한다.

### 2-3. CDC (Change Data Capture)

변경을 "상태"가 아니라 "이벤트 스트림"으로 내보내는 방식이다.
PostgreSQL logical decoding, SQLite session 확장(changeset), Debezium이 대표적이다.
소비자는 전체를 다시 읽지 않고 변경분만 받는다.

→ **fsledger**: 스캔마다 `changes(snapshot_id, kind, path, old_path, size_delta)`에
`added / modified / deleted / moved`를 남긴다. 대시보드의 타임라인과 "최근 변경"은
이 테이블만 읽는다.

### 2-4. 원자성

스캔 하나는 트랜잭션 하나다. 중간에 실패하면 반쯤 반영된 스냅샷이 남지 않는다.

## 3. 중복 탐지 — fdupes/jdupes/rmlint의 단계적 필터

전체 파일을 해시하면 수백 GB를 읽어야 한다. 기존 도구는 싼 비교부터 거른다.

1. **크기**가 같은 파일끼리만 후보 (대부분 여기서 탈락)
2. 앞부분 **부분 해시**(fsledger는 64 KiB)가 같은 것만 남김
3. 남은 것만 **전체 해시**
4. 하드링크(같은 inode)는 중복이 아니라 같은 파일이므로 제외

→ **fsledger**: 위 순서 그대로 구현하고, 계산한 전체 해시는 stat 캐시와 함께 저장해
다음 스캔에서 재사용한다. 보존할 원본은 "Downloads 밖 > 경로가 짧은 쪽 > 수정 시각이 오래된 쪽"
순서로 고른다.

## 4. 종합 — 설계 매핑표

| 개념 | 출처 | fsledger 구현 |
|------|------|---------------|
| 콘텐츠 주소 | git blob | `content_hash` (BLAKE2b-256), 중복 그룹 키 |
| 커밋 체인 | git commit | `snapshots.parent_id` |
| stat 캐시 | git index | `(size, mtime_ns, ino, dev)` 일치 시 해시 생략 |
| racy 처리 | git racy-git | 스캔 시작 2초 이내 mtime → `racy=1`, 다음 스캔에서 재해시 |
| rename 추론 | git diff -M | inode 일치 → 해시 일치 순으로 `moved` 판정 |
| ignore | .gitignore | 기본 제외 목록 + 설정, 제외 디렉터리는 크기만 집계 |
| 시간 테이블 | MVCC / SCD2 | `file_versions.valid_from/valid_to` |
| 변경 이벤트 | CDC | `changes` 테이블 |
| 선기록 | WAL | `ops` 테이블에 pending 먼저 기록 → 실행 → done, `undo`·`recover` |
| 원자성 | 트랜잭션 | 스캔 1회 = 트랜잭션 1개 |
| 단계적 중복 필터 | fdupes·rmlint | 크기 → 부분 해시 → 전체 해시 |

## 5. 의도적으로 가져오지 않은 것

- **내용 자체의 버전 보관** (git object store): 홈 폴더 300 GB의 이전 버전을 보관하면 디스크가
  두 배로 든다. 메타데이터와 해시만 기록한다. 내용 복원이 필요하면 Time Machine이 맞는 도구다.
- **FSEvents 실시간 감시**: 데몬 운영 부담에 비해 이득이 작다. stat 캐시로 43만 파일 재스캔이
  1분 안쪽이면 하루 1회 스캔으로 충분하다.
- **자동 삭제**: 어떤 DB도 사용자 데이터를 추측으로 지우지 않는다. fsledger는 삭제를
  **제안**만 하고, 실행해도 격리 폴더로 옮길 뿐이다. 영구 삭제는 `purge`로 따로 한다.
