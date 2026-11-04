# Paperbot

논문을 검색하고, 관련 논문을 GPT-6 Luna로 요약한 뒤 논문별 폴더에 Markdown과 추출한 그림·표 파일을 저장합니다. 선택적으로 요약마다 Gmail로 Markdown 본문과 그림을 보낼 수 있으며 PDF는 메일에 첨부하지 않습니다. 실행은 명령을 직접 입력했을 때만 시작됩니다.

## 동작 방식

1. [config.example.yaml](config.example.yaml)의 주제 설정을 바탕으로 Semantic Scholar와 arXiv에서 논문을 검색합니다.
2. 제목·초록을 바탕으로 임베딩 유사도와 GPT-6 Luna 판정을 적용해 관련 논문을 추립니다. 초록이 없으면 제목만으로 관련성을 판단할 수 있어 이 단계는 PDF 원문 확보 여부를 보장하지 않습니다.
3. PDF 응답이 실제로 열리는 PDF인지 확인하고 본문 텍스트의 품질을 검사합니다. PDF를 받지 못하거나 본문 텍스트를 뽑을 수 없으면 초록으로 요약합니다. 초록과 판독 가능한 PDF 본문이 모두 없으면 본문 정보 부족으로 필터링해 요약·Markdown 저장·메일 발송을 건너뜁니다. 본문 텍스트를 추출한 PDF에서는 DocLayout-YOLO와 PyMuPDF로 앞 10페이지의 그림·표를 찾아 PNG로 자릅니다.
4. 논문 제목과 ID를 바탕으로 `data/markdown/` 아래에 폴더를 만들고, 그 안에 UTF-8 Markdown 파일과 추출된 PNG를 저장합니다.
5. `gmail.enabled: true`이면 본문 또는 요약할 수 있는 초록을 확보한 처리 논문마다 Gmail을 한 통씩 보냅니다. PDF 원문을 받지 못해도 초록이 있으면 발송합니다. Markdown을 HTML 본문으로 렌더링하고 PNG는 본문 안에 표시하며, PDF 파일은 첨부하지 않습니다.

Markdown에는 제목, 저자, 학회/연도, 인용 수, 검색 주제와 출처, 논문 링크, 요약이 포함되며, 저장된 그림·표 PNG를 상대경로로 표시합니다. 기본값은 논문당 그림과 표 각각 최대 3개입니다. PDF 원문을 받지 못해도 사용할 수 있는 초록이 있으면 초록 기반 요약과 메일을 보냅니다.

기본 설정은 논문 검색과 Markdown 저장만 사용하며 Gmail은 꺼져 있습니다. Slack, Zulip, Zotero, Google Drive 인증은 필요하지 않습니다. 별도 GPU 서버 없이 OpenAI API를 사용하며, 자동 실행이나 예약 작업은 설정하지 않습니다.

## 설치

Python 3.10 이상이 필요합니다. 저장소 폴더에서 실행합니다.

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements-paperbot.txt
[ -f config.yaml ] || cp config.example.yaml config.yaml
```

그림·표 검출 모델 가중치(약 41MB)는 PDF를 처음 처리할 때 Hugging Face에서 내려받습니다.

OpenAI API 키는 `~/.paperbot_env`의 `OPENAI_API_KEY` 환경 변수로 읽습니다. 이미 저장한 키를 사용하려면 실행 전에 환경 파일을 불러옵니다. Semantic Scholar API 키는 선택 사항이며, `S2_API_KEY`가 없으면 검색 요청 간격이 길어집니다.

```bash
source ~/.paperbot_env
```

`config.yaml`에서 주제별 검색어, 학회, 연도, 인용 수 하한과 처리량을 조정합니다. 기본 LLM은 `https://api.openai.com/v1`의 `gpt-6-luna`입니다.

## Gmail 설정

Gmail 발송은 OAuth 2.0을 사용합니다. Google Cloud Console에서 Gmail API를 활성화하고 OAuth 클라이언트를 **Desktop app** 유형으로 만든 뒤, 내려받은 JSON 파일을 기본 경로 `~/.config/paperbot/gmail_credentials.json`에 저장합니다. Google OAuth 동의 화면이 테스트 상태라면 사용할 Gmail 계정을 테스트 사용자로 등록합니다. [Google의 Gmail Python 빠른 시작](https://developers.google.com/workspace/gmail/api/quickstart/python)에서 프로젝트·동의 화면·데스크톱 클라이언트 설정을 확인할 수 있습니다. 테스트 상태에서는 Gmail 권한이 포함된 refresh token이 7일 뒤 만료될 수 있으므로, 재인증이 필요하면 `--authorize-gmail`을 다시 실행합니다 ([Google OAuth 문서](https://developers.google.com/identity/protocols/oauth2)).

가상환경에 Gmail API와 Markdown 렌더링 의존성을 설치하고 브라우저 승인을 한 번 진행합니다.

```bash
source ~/.paperbot_env
./venv/bin/pip install -r requirements-paperbot.txt
./venv/bin/python -m paperbot --authorize-gmail
```

승인 토큰은 기본 경로 `~/.config/paperbot/gmail_token.json`에 저장됩니다. 메일 발송에 필요한 Gmail 권한과 계정 주소 확인 권한만 요청합니다. 실제 수신 주소를 `config.yaml`의 `gmail.to`에 입력하고 `gmail.enabled: true`로 바꾸면, 이후 paperbot 실행마다 새로 요약한 논문에 대해 한 통씩 보냅니다. Gmail이 발송을 명시적으로 거부하면 다음 수동 실행에서 저장된 Markdown으로 메일만 재시도합니다. 연결 끊김이나 서버 오류처럼 Gmail이 메일을 받았는지 확인할 수 없는 경우에는 중복 발송을 막기 위해 자동 재전송하지 않고, 요약 파일 저장은 완료 처리합니다. OAuth 클라이언트와 토큰 경로는 `gmail.credentials_file` 및 `gmail.token_file`에서 바꿀 수 있습니다. `--dry-run`은 이메일을 보내지 않습니다.

| 설정 | 설명 |
|---|---|
| `gmail.enabled` | `true`면 발송 활성화. 모든 topic은 `platform: markdown`이어야 합니다. |
| `gmail.to` | 받을 이메일 주소. 여러 주소는 쉼표로 구분합니다. |
| `gmail.credentials_file` | Google Cloud에서 받은 Desktop OAuth JSON 경로 |
| `gmail.token_file` | 사용자 승인 후 저장되는 로컬 OAuth 토큰 경로 |
| `gmail.subject_prefix` | 제목 앞에 붙일 문자열 |

## 수동 실행

저장소 루트에서 아래 명령을 실행하면 검색부터 저장까지 한 번 처리합니다.

```bash
source ~/.paperbot_env
./venv/bin/python -m paperbot
```

선택한 주제만 실행하거나 처리할 논문 수를 제한할 수 있습니다.

```bash
./venv/bin/python -m paperbot --topic ship-performance-prediction --limit 1
```

예약 실행은 만들지 않았습니다. 명령을 다시 실행할 때 이미 처리한 논문은 `data/papers.db`의 기록에 따라 건너뜁니다. `--dry-run`은 Markdown 대신 터미널에 미리보기를 출력하지만, 논문을 처리 완료로 기록하므로 일반 실행에는 사용하지 마세요.

## 파일 위치

| 경로 | 내용 |
|---|---|
| `data/markdown/<논문 제목>-<ID>/` | 논문별 폴더. 제목 기반 `.md` 요약과 `figure_*.png`, `table_*.png` 파일 |
| `data/papers.db` | 중복 처리를 막는 검색·처리 이력 |
| `data/pdfs/` | 논문 본문 추출에 사용한 PDF. `storage.keep_pdfs: true`면 로컬에 보관하며 메일에는 첨부하지 않음 |
| `logs/paperbot.log` | 실행 로그 |
| `~/.config/paperbot/gmail_credentials.json` | Google Cloud에서 받은 OAuth Desktop 클라이언트 파일 |
| `~/.config/paperbot/gmail_token.json` | Gmail OAuth 승인 토큰 |

`markdown.max_figures`와 `markdown.max_tables`로 논문별 추출 개수를 조정할 수 있습니다. 0으로 설정하면 해당 종류를 추출하지 않습니다. 출력 폴더는 `storage.markdown_dir`로 변경할 수 있습니다. 저장 경로는 설정 파일 위치를 기준으로 해석합니다.
