# 2026-10-03

- Bug
  - До миграции на реакитвный UI секция Git Access показывала понятный текст о выбранном ключе. Для generated она также показывала публичный ключ, инструкцию добавить его в GitHub Deploy Keys с правом записи и кнопку Regenerate Deploy Key. После миграции в секции остались сырое значение generated и кнопка Generate Deploy Key; публичный ключ и пояснения исчезли. Это регрессия интерфейса, а не задуманный вид секции.
  - Сделай ровно так, как было до реактивного интерефейса.

- Bug
  - HA без свежего бекапа.
  - Жму [Preview Git to HA]
  - Процесс останавливается с [ERROR] и предложением продолжить через [Acknowledge & Proceed]
  - Жму в браузере перезагрузить страницу
  - В интерфейсе больше нет строки с ошибкой, но состояние всё ещё ERROR
  - Ожидаемое поведение как и до обновления вкладки:
    - Бедж [WARNING]
    - Сообщение No fresh system backup found within 24 hour(s)
  - Отдельное пожелание: Замени состояние ERROR на WARNING и всё соответствующее: беджики, тексты, итд. Идея в том, что это по дизайну фичи более не является ошибкой, а лишь предупреждением.

- Bug
  - Preview Git to HA сначала долго проверяет diff, а лишь потом наличие бекапа — это избыточная UX задержка
  - Ожидаемое состояние: сервер сразу отдаёт признак наличия некапа и если его нет, не теряет время на diff рутину
  - Риски: Между [Preview Git to HA], [Apply Git to HA] и [Acknowledge & Proceed] может быть большая разница во времени. Поэтому надо проверять наличие свежего бекапа на нескольких этапах.

# 2026-10-01

- Proceed with Git to HA without backup
  - Implemented in HA Ops 2.2.0
  - Persona should be able to proceed with additional step of confirmation
    - When there is no Backup
      - The following messange on the same line as where the button [Apply Git to HA] is located will be displayed:
        - `[ERROR] No fresh system backup found within 24 hour(s)`
      - Following buttons: [Acknowledge & Proceed] [Retry Git to HA]
        - The button [Retry Git to HA] replaces disabled [Apply Git to HA]
        - While [Retry Git to HA] means re-check the presence of fresh backup and proceed immediately if the backup is fresh now
  - [Acknowledge & Proceed] will bypass the requirement to have a fresh backup
    - In this case there is no need to double check the freshness of backup as persona action already acknowledges the impact
  - No [Cancel] button is needed, persona can simply ignore furhter steps.

- Revisit tests
  - It takes around a minute to perform all 500+ tests
    ```
    % g pf
    Running HA Ops tests before push...
    Running parallel test group...
    =============================================================== test session starts ===============================================================
    platform darwin -- Python 3.9.6, pytest-8.4.2, pluggy-1.6.0
    rootdir: /Users/purportex/Work/HA/ha-ops-addon
    plugins: anyio-4.12.1, xdist-3.8.0
    10 workers [534 items]
    ........................................................................................................................................... [ 26%]
    ........................................................................................................................................... [ 52%]
    ........................................................................................................................................... [ 78%]
    .....................................................................................................................                       [100%]
    ============================================================== 534 passed in 54.48s ===============================================================
    Everything up-to-date
    ```
  - Revisit all tests, make groups and figre out whether there are obsolete, redundant or repetitive tests
    - Potentially get rid of duplicated or useless ones.
  - Consider to run more of them in parallel if possible.

- Bug: Collapsing and refreshing the page when clicking the checkbox
  - Steps to repro:
    - Click on [Git to HA]
    - Expand any of the file in Change List
    - Click on checkbox next to expanded file
  - Observed:
    - Expanded file collapsed
    - Page refreshes
  - Expected:
    - Expanded file remains expanded
    - No visible refresh
    - UI persists as-is with no visible difference, except
      - Checkbox status
      - Confirmation button
      - LOg status potentially

# 2026-09-30

- Managed Targets
  - Persona should observe the explanation of the section and what's the impact between abstract choices
  - The table is
    - inside the section and is collapsible the same way as DIFFs inside the Change List
    - Is collapsed by default
    - Persona can expand and collapse the section
    - Reload of the page would serve collapsed section
      - No need to remember the state between
        - sessions / reloads
        - multiple tabs or windows
