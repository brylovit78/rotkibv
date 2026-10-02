# Rotki BV

Публічний community fork [rotki/rotki](https://github.com/rotki/rotki) для власного
сервера та розвитку додаткових інтеграцій. Початкова база — **v1.44.0**,
commit `ef14aadd1f387a199731fcc03254463b45b6e0cb`. Перший образ зберігає
функціональність upstream. **TRON ще не реалізовано.**

- [Завдання](https://github.com/brylovit78/rotkibv/issues)
- [Дошка Rotki BV](https://github.com/users/brylovit78/projects/3)
- [CI](https://github.com/brylovit78/rotkibv/actions/workflows/fork-ci.yml)
- [Релізи образів](https://github.com/brylovit78/rotkibv/actions/workflows/fork-release.yml)

## Що досліджено в upstream

Джерела нижче прив'язані до релізу, з якого починається fork.

| Частина | Як працює |
| --- | --- |
| Архітектура | Python 3.14: Flask REST, uvicorn/WebSocket, SQLCipher, облік і blockchain integrations. Vue/TypeScript: web та Electron. Rust: Colibri і supervisor/proxy Starling. [Код](https://github.com/rotki/rotki/tree/v1.44.0). |
| Гілки | Нові можливості йдуть до `develop`, виправлення для patch-релізів — до `bugfixes`. Окремі Issue, feature branch і PR з тестами. [CONTRIBUTING](https://github.com/rotki/rotki/blob/v1.44.0/CONTRIBUTING.md). |
| CI | Lint/typecheck, Python pytest у групах api/decoders/others, Vitest, Playwright у 4 shards, Rust clippy/tests, Windows lifecycle, документація. Upstream вибирає jobs за diff та labels. [Workflow](https://github.com/rotki/rotki/blob/v1.44.0/.github/workflows/rotki_ci.yml). |
| Дані тестів | VCR cassettes живуть окремо у `rotki/test-caching`; assets — у `rotki/assets`, додаткові дані — у `rotki/data`. Назва власної гілки не є назвою їхніх гілок. [Backend tests](https://github.com/rotki/rotki/blob/v1.44.0/.github/workflows/task_backend_tests.yml). |
| Релізи | Upstream збирає desktop-пакети й Docker; публікація релізу просуває DockerHub tag у `latest`. Є власні signing/publishing secrets і nightly jobs. У цьому fork ці upstream-публікатори вимкнено в GitHub. [Release](https://github.com/rotki/rotki/blob/v1.44.0/.github/workflows/rotki_release.yaml). |
| Контейнер | Багатостадійний Dockerfile збирає frontend, Python binary і Rust із цього checkout. Distroless runtime, Starling як PID 1, HTTP 80, volumes `/data`, `/logs`, `/config`, власний healthcheck. [Dockerfile](https://github.com/rotki/rotki/blob/v1.44.0/Dockerfile). |
| Оновлення | Контейнер оновлюють заміною образу; desktop має окремий Electron updater. User/global DB мають послідовні міграції, assets можуть оновлюватися з upstream незалежно від образу. [AssetsUpdater](https://github.com/rotki/rotki/blob/v1.44.0/rotkehlchen/globaldb/asset_updates/manager.py). |

В upstream-довідці трапляються старі вимоги Node 22/pnpm 10. Для цієї бази
джерела правди — `frontend/.nvmrc`, `frontend/package.json`, `.github/.env.ci`
і lockfiles: Node 24, pnpm 11, Python 3.14, Rust 1.91 у CI.

## Гілки та сумісність

- `main` — власна перевірена лінія на базі stable upstream; default branch.
- `agent/<issue>-<slug>` — окрема задача; PR до `main`.
- `origin/develop` та інші успадковані гілки — вихідні upstream-гілки. Не
  використовувати GitHub **Sync fork** для `main`: ця кнопка орієнтується на
  default branch upstream (`develop`), а тут потрібен обраний stable tag.
- Локально `origin` → `brylovit78/rotkibv`, `upstream` → `rotki/rotki`.
- Feature PR можна squash-merge. **Upstream sync PR зливати merge commit**,
  щоб Git пам'ятав історію upstream і наступний merge не повторював зміни.
- Не перейменовувати Python modules, не переформатовувати весь код, не
  змішувати інфраструктуру fork із функціональними змінами для upstream.

Оновлення upstream виконують окремим Issue та PR:

```bash
git fetch origin
git fetch upstream --tags
git switch -c agent/ISSUE-upstream-vX.Y.Z origin/main
git merge --no-ff vX.Y.Z
```

Після розв'язання конфліктів оновити `.github/fork.env`: `UPSTREAM_TAG`,
`CASSETTES_REF` (commit test-caching, сумісний із релізом), `ASSETS_BRANCH`.
Початкові cassettes зафіксовано на master станом на публікацію v1.44.0;
assets читаються з master. Це не повністю offline-тести: деякі upstream
fixtures ще залежать від мережі та `rotki/data/develop`.
Перевірити міграції на **копії** реальних даних, пройти повний CI та рев'ю,
merge PR і випустити новий fork tag. Не перепризначати upstream tags.

Для внеску назад створити чисту гілку від `upstream/develop` або
`upstream/bugfixes` і cherry-pick лише потрібні функціональні commits.
Дотримуватися upstream Issue/CLA процесу; згоду з CLA приймає сам автор.

## Процес роботи людей і агентів

Джерело вимог — GitHub Issue з acceptance criteria, тестами та залежностями.
Стани дошки: **Backlog → Ready → In progress → Review → Done**.
TRON-задачі лишаються Backlog до окремого старту розробки.

1. Відновити checkpoint активного Issue або взяти найпріоритетніший Ready.
2. Перевести Issue в In progress, створити feature branch від `origin/main`.
3. Виконати критерії мінімальною зміною; додати релевантний regression test.
4. Запустити локальні перевірки змінених частин; відкрити PR з `Closes #N`.
5. Записати implementer model, перевести задачу в Review, пройти Fork CI.
6. Окремий reviewer **Claude Code Fable 5 (`claude-fable-5`)**, який не
   реалізовував PR, читає diff та потрібний контекст. Перевіряє коректність,
   безпеку, тести й зайву складність. Reviewer не змінює код.
7. Опублікувати фактичний висновок Claude в PR з моделлю, head SHA,
   findings і `APPROVED` / `CHANGES_REQUESTED`. Maintainer виставляє commit
   status **Independent review** на **цей head SHA**. Новий commit потребує
   нового рев'ю. Заборонено ставити success без фактичного незалежного рев'ю.
8. Після зеленого CI та review status merge PR, закрити Issue й поставити Done.
9. Перевірити CI на merge commit у main. За помилки повернути Issue в роботу.
10. Для нового серверного образу після успішного main CI створити immutable
    tag `v<upstream-version>-bv.<number>`, наприклад `v1.44.0-bv.1`.

GitHub status є засвідченням maintainer про локальне рев'ю. GitHub не вміє
сам перевірити модель; GitHub-account implementer і reviewer може бути один,
але model/session і висновок мають бути незалежними. API ключ Claude у
GitHub не потрібний. Якщо реалізує Claude, залучити іншу модель reviewer і
явно зафіксувати цей виняток у PR; ніколи не підмінювати рев'ю самооцінкою.

Приклад локального запуску reviewer (diff додати у prompt, вказати SHA):

```bash
claude -p --safe-mode --model claude-fable-5 \
  --tools Read,Glob,Grep --output-format json < /tmp/review-prompt.txt
```

Після прочитання результату та підтвердження, що remote PR head не змінився:

```bash
gh api --method POST repos/brylovit78/rotkibv/statuses/HEAD_SHA \
  -f context='Independent review' -f state=success \
  -f description='Claude Fable 5: approved this PR head' \
  -f target_url=REVIEW_COMMENT_URL
```

Checkpoint у коментарі Issue: model, branch/pushed SHA, PR, стан робочого
дерева, Done, Next (одна дія), Checks, Blockers. Спочатку зберегти корисні
зміни commit/push; наступний агент відновлюється з checkpoint.

## Перевірки та реліз

Fork CI повторно використовує upstream jobs; у fork вони запускаються повністю
для кожного PR/main push, без upstream labels для пропуску тестів. Додатково
виконується тест release gate, перевірка Compose та build/smoke образу.
Smoke перевіряє Docker healthcheck усього стека, HTML, API ping, restart і volume.
Це не замінює тест міграції реальної бази та зовнішніх фінансових інтеграцій.

```bash
uv sync --locked --group dev --group lint --group ci
uv run pytest rotkehlchen/tests/RELEVANT_PATH
uv run make lint
# frontend commands — із frontend/:
pnpm install --frozen-lockfile
pnpm run lint
pnpm run typecheck
pnpm run test:unit
pnpm run test:e2e
# із кореня:
cargo test --workspace --locked
python3 -m unittest discover -s tools/fork -p 'test_*.py'
docker build --build-arg ROTKI_VERSION=1.44.0 -t rotkibv:check .
python3 tools/fork/smoke.py rotkibv:check
```

Release gate перевіряє належність commit до main, успіх останнього main CI
саме на цьому commit і merged PR з актуальним незалежним review status.
Після цього образ **будується з власного checkout**, проходить smoke і той
самий локальний image ID публікується в GHCR. Публікація не має SSH-доступу
до сервера. Архітектура першого релізу — `linux/amd64`.

Версії: image tag `v1.44.0-bv.1`, Python version `1.44.0+rotkibv.1`,
додатковий tag `sha-<full-commit>`, OCI labels із source/revision/license.
UI може показувати базову frontend-версію upstream; джерело точного fork
релізу — image tag/digest та OCI revision. `latest` не публікується.
Dockerfile upstream містить змінні зовнішні base tags/tool downloads, тому
повторний build не обіцяє бітової ідентичності: розгортати збережений digest.

## Розгортання на сервері

```bash
cd deploy
cp .env.example .env
# Встановити ROTKIBV_IMAGE у .env на опублікований tag або image@sha256:digest.
docker compose pull
docker compose up -d --wait
docker compose ps
```

За замовчуванням доступ через `127.0.0.1:8080`. Для віддаленої роботи —
наявний HTTPS reverse proxy або SSH tunnel; LAN bind можна задати у `.env`.
Backend і Colibri зовні окремо не публікуються. Volumes зберігають дані.
Не запускати `docker compose down -v` для робочої інсталяції.

Перед оновленням: зупинити контейнер, зробити snapshot/backup **усіх** data,
config і logs volumes, записати попередній image digest. Потім змінити image,
pull/up --wait, перевірити вхід, баланси та історію. Для rollback після
міграції відновити попередній образ **і відповідний backup даних**. Зниження
версії поверх уже мігрованої БД не вважається безпечним rollback.
Перший запуск тестувати з окремими volumes; перенесення наявної серверної
інсталяції залежить від її версії та шляхів і потребує окремої перевірки.

## Перший напрям: TRON

Upstream запит: [rotki/rotki#10465](https://github.com/rotki/rotki/issues/10465).
У цій базі немає окремої TRON chain integration. Наявність активу TRX у
каталозі активів не означає можливість додати TRON-адресу та імпортувати історію.

Порядок робіт: контракт і fixtures → адреси/Base58Check, TRX і TRC20 баланси
→ paginated історія та дедуплікація → history events, fees та accounting
→ UI/E2E → upgrade/rollback і можливий внесок upstream.
TRON не слід просто додавати як EVM chain: відрізняються формат адрес,
індексація, TRC20 metadata, energy/bandwidth і модель комісій. RPC/indexer,
ліміти, confirmed/finalized дані й scope staking/TRC10 визначаються в окремій
специфікації. Новий код має використовувати чинні managers/events і точні
decimal amounts, з тестами помилок API, pagination, повторного імпорту,
failed transactions і міграцій.

Збережено [AGPL-3.0](LICENSE.md), авторство та upstream notices. Це незалежний
fork, а не офіційний реліз rotki. Початковий порожній репозиторій збережено як
[rotkibv-bootstrap](https://github.com/brylovit78/rotkibv-bootstrap).
