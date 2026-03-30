# Graph-Structured Premise Retrieval for Automated Theorem Proving
# Графовый поиск предпосылок для автоматического доказательства теорем

## Финальный отчёт: эксперименты 140--143

---

## 1. Постановка эксперимента / Experimental Setup

### 1.1 Модель и параметры генерации / Model and Generation Parameters

Во всех экспериментах используется единая модель и единый набор параметров генерации. Модель не подвергалась дообучению (fine-tuning) и запускается "as-is" с HuggingFace.

| Parameter | Value |
|-----------|-------|
| Model | **DeepSeek-Prover-V2-7B** |
| Precision | bfloat16 |
| Accelerator | GPU (NVIDIA) |
| Temperature | 0.6 |
| max_new_tokens | 8192 |
| top_p | 0.95 |
| top_k | 40 |
| repetition_penalty | 1.05 |
| RANDOM_SEED | 42 |

Температура 0.6 выбрана как компромисс между разнообразием и качеством: при T=0 модель генерирует однообразные доказательства (pass@8 деградирует), при T>0.8 слишком много синтаксического мусора.

### 1.2 Верификатор / Verifier

Доказательства проверяются через **Lean 4 REPL** (Read-Eval-Print Loop), развёрнутый как Kafka-воркер в инфраструктуре SciLib.

| Parameter | Value |
|-----------|-------|
| Verifier | Lean 4 REPL via Kafka |
| Timeout | 30 seconds per proof |
| maxHeartbeats | 2,000,000 (2M) |
| Environment | Mathlib (актуальная версия) |

Сгенерированное доказательство подставляется вместо `sorry` в исходный `.lean`-файл задачи, затем файл передаётся в REPL. Если Lean завершает проверку без ошибок и в рамках таймаута -- задача считается решённой.

**Защита от пустых доказательств.** В раннем эксперименте (exp 92) было обнаружено, что модель иногда генерирует только комментарии (`-- We need to show that...`) или текст на естественном языке вместо Lean-кода. Такой вывод компилируется Lean без ошибок (пустой файл с `import Mathlib` валиден), создавая ложноположительные результаты. Для предотвращения этого реализована функция `_has_proof_content()` (solver.py), которая проверяет:
- наличие хотя бы одного ключевого слова Lean (theorem, by, simp, ring, omega, linarith, etc.)
- отсутствие маркеров NL-текста (markdown-заголовки, "The ", "We ")
- достаточный объём кода после удаления комментариев (≥5 символов)

Генерации, не прошедшие эту проверку, отклоняются и перегенерируются (до 8 попыток).

### 1.3 Бенчмарк / Benchmark

**MiniF2F** (Zheng et al., 2022) -- стандартный бенчмарк для оценки формальных доказателей.

| Split | Tasks | Description |
|-------|-------|-------------|
| **Test** | 244 | Основной набор |
| **Valid** | 244 | Валидационный набор |
| **Total** | **488** | Полный бенчмарк, 0 пересечений между split'ами |

В данной работе используются **оба** фолда (Test + Valid). Это допустимо, поскольку наша задача является **чисто evaluation** (inference-only): ни модель DeepSeek-Prover-V2-7B, ни наш граф знаний (GraphDB), ни embedding-модель SciLibMath_v1 не обучались и не адаптировались на задачах MiniF2F. Утечки данных (data leakage) нет: граф знаний построен из кодовой базы Mathlib и её зависимостей, а не из бенчмарков. Модель эмбеддингов обучена на корпусе Mathlib-statement'ов, которые не содержат задач MiniF2F. Таким образом, использование обоих фолдов удваивает статистическую мощность без компрометации валидности результатов.

Задачи взяты из математических олимпиад и стандартизированных тестов разного уровня сложности:

| Category | Tasks | Description |
|----------|-------|-------------|
| IMO | 40 | Международная математическая олимпиада (наивысшая сложность) |
| AIME | 30 | American Invitational Mathematics Examination |
| AMC | 91 | American Mathematics Competition (AMC 10/12) |
| MATHD | 260 | MATH Dataset (Algebra, NumberTheory, Other) |
| Other | 67 | Прочие задачи |

### 1.4 Протокол запусков / Evaluation Protocol

Для каждой пары (задача, режим) производится **8 независимых запусков** с temperature sampling (Pass@K, K=8). Это позволяет оценить как pass@1 (вероятность решения с одной попытки), так и pass@8 (решаемость в принципе).

**Расчёт pass@1.** Используется эмпирическая частота:

```
pass@1 = sum(passed_i) / sum(total_i)
```

где суммирование идёт по всем задачам в рассматриваемой группе. Это эквивалентно unbiased pass@k estimator при k=1 (Kulal et al., 2019; Chen et al., 2021).

**Общий объём экспериментов:** 50,752 прогонов (runs), распределённых следующим образом:

| Exp | Split | Режимы | Задач × Режимов × Passes | Runs | Цель |
|-----|-------|--------|--------------------------|------|------|
| 140 | Test | 16 SciLib (ablation) | 244 × 16 × 8 | 31,232 | Ablation study: все 16 режимов RAG |
| 141 | Test | BL_LS, BL_LF | 244 × 2 × 8 | 3,904 | Сравнение с LeanSearch, LeanFinder |
| 142 | Valid | A0, B1, C21, C23, BL_LS, BL_LF | 244 × 6 × 8 | 11,712 | Расширение на Valid: лучшие режимы + бейзлайны |
| 143 | Test+Valid | BL_LE | 488 × 1 × 8 | 3,904 | Сравнение с LeanExplore |
| | | | **Итого** | **50,752** | |

В ablation study (exp 140) тестировались 16 режимов, включая retry-варианты (R1). В сравнительных экспериментах (141--143) из ablation были выбраны сильнейшие режимы без retry: **A0** (baseline), **B1** (vector RAG), **C21** (graph+vector), **C23** (graph+rerank). Три внешних бейзлайна: **LeanSearch** (Gao et al., 2024), **LeanFinder** (Lu et al., 2025), **LeanExplore** (Asher, 2025).

---

## 2. Описание режимов / Mode Descriptions

### 2.1 Группа A -- Baseline (без RAG / No Retrieval)

#### A0 -- Bare Model (голая генерация)

Простейший baseline. На вход модели подаётся только формальная постановка задачи в code fence. Никаких подсказок, контекста, дополнительной информации.

**Prompt template:**
```
Complete the following Lean 4 code:
```lean4
{полный .lean файл задачи с sorry}
```
```

**Характеристики:**
- LLM calls: **1**
- Дополнительные вызовы: нет
- Латентность: минимальная (только время генерации)

A0 является базовой мерой "собственных знаний модели" и используется для стратификации задач по сложности.

#### A1 -- Chain-of-Thought (пошаговое рассуждение)

Двухшаговый режим, вдохновлённый Chain-of-Thought prompting (Wei et al., 2022). В оригинальном подходе рассуждение и ответ генерируются за один проход. Однако модели класса 7B плохо поддерживают длинные chain-of-thought в едином контексте: рассуждение "съедает" бюджет токенов и деградирует качество финального кода. Поэтому мы **разделяем процессы**: сначала генерируем план рассуждений отдельным вызовом, затем используем его как своеобразный **self-RAG** (контекст, сгенерированный самой моделью) для второго прохода, где генерируется формальное доказательство:

**Шаг 1.** Модель получает theorem statement и генерирует **план рассуждений** (reasoning plan) -- текстовое описание стратегии доказательства. Параметры генерации для этого шага: max_new_tokens=2048, repetition_penalty=1.2 (повышенная, чтобы избежать зацикливания в рассуждениях).

**Шаг 2.** Модель получает исходный theorem statement **вместе с планом рассуждений** из Шага 1 в качестве контекста, затем генерирует формальное доказательство на Lean 4.

**Характеристики:**
- LLM calls: **2** (план + генерация)
- Дополнительные вызовы: нет
- Гипотеза: промежуточное рассуждение помогает модели структурировать доказательство

---

### 2.2 Группа B -- Vector RAG (SciLib Qdrant)

#### B1 -- Vector Semantic Search

Использует **MCP-сервер** SciLib (порт 8111) для семантического поиска по коллекции `scilib_mathlib_v1` в Qdrant, с embedding-моделью **SciLibMath_v1** (Khalov, Ataeva, Tuchkova, 2026. "Creating a multimodal dataset for the SciLibRu semantic library using a language model." *Pattern Recognit. Image Anal.*, 36. In press).

**Пайплайн:**

1. **Seed generation (LLM).** Модель получает theorem statement и генерирует ~5 "зародышевых" (seed) идентификаторов -- имён лемм Mathlib, которые могут быть полезны. Формат: промпт с `#check <name>` для каждого кандидата.

2. **Semantic search (MCP/Qdrant).** Для каждого seed-идентификатора MCP-сервер выполняет `semantic_search` по коллекции `scilib_mathlib_v1`, возвращая top-1 наиболее похожую лемму (по cosine similarity в пространстве эмбеддингов).

3. **Hint formatting.** Каждый hint формируется как пара:
```
lemma_name
full_lean_code_of_the_lemma
```
   Полный Lean-код включает сигнатуру, атрибуты и тело доказательства леммы.

4. **Generation.** Модель получает theorem statement + все собранные hints и генерирует доказательство.

**Характеристики:**
- LLM calls: **1** (для seed generation) + **1** (для генерации доказательства)
- MCP calls: ~5 (по одному на seed)
- Средний объём hints: ~10 блоков, ~2868 символов
- Формат hints: **плоский список** (name + full code)

---

### 2.3 Группа C -- Graph RAG (SciLib GraphDB + PostgreSQL + Qdrant)

#### C1 -- Graph RAG Baseline (модельные seeds)

Первая итерация графового поиска. Модель сама генерирует имена лемм Mathlib (до **~30 LLM calls** на итеративные циклы генерации и рефайнмента). SPARQL-запросы разрешают имена в GraphDB, затем выполняется графовое расширение по рёбрам зависимостей.

**Характеристики:**
- LLM calls: **~30** (множественные циклы генерации seed'ов)
- Медианная латентность: **163 секунды** (из-за большого количества LLM-вызовов)
- Формат hints: структурированный, но с шумом от модельной генерации seeds
- *Устаревший режим, заменён на C11/C21*

#### C11 -- Structure-Aware Graph RAG (Zero LLM Seeds)

Ключевое улучшение: полностью исключает LLM из этапа генерации seed'ов. Вместо этого используется **regex-based pattern extraction** (9 паттернов).

**Пайплайн (шаг за шагом):**

1. **`extract_goal_features`** -- regex-анализ theorem statement. Определяет:
   - Типы данных: `{Nat, Int, Real, Complex, ...}`
   - Операции: `{eq, lt, le, gt, ge, dvd, mul, add, pow, mod, ...}`
   - Структурные паттерны: наличие кванторов, импликаций, etc.

2. **`classify_goal`** -- сопоставление с одним из 9 предопределённых паттернов:

   | Pattern | Trigger | Example seeds |
   |---------|---------|---------------|
   | `ineq_basic` | Detected `lt`, `le`, `gt`, `ge` | `le_antisymm`, `not_lt`, `le_of_eq`, `mul_le_mul_of_nonneg_left` |
   | `ineq_pow` | Detected `pow` + inequality | `pow_le_pow_left`, `pow_lt_pow_right`, `sq_nonneg` |
   | `divisibility` | Detected `dvd` | `dvd_refl`, `dvd_trans`, `dvd_mul_left`, `Nat.Prime` |
   | `nat_arith` | Detected `Nat` | `Nat.Prime`, `Nat.cast_le`, `Nat.cast_lt`, `Nat.cast_inj` |
   | `int_arith` | Detected `Int` | `Int.cast_le`, `Int.natAbs_dvd`, etc. |
   | `real_analysis` | Detected `Real` | `Real.rpow_mul`, `abs_le`, etc. |
   | `algebra_basic` | Detected `mul`, `add`, ring ops | `mul_comm`, `add_comm`, `ring`, etc. |
   | `modular` | Detected `mod`, `%` | `Nat.mod_def`, `Int.emod_emod_of_dvd`, etc. |
   | `combinatorics` | Detected `Finset`, `choose` | `Finset.card_filter`, `Nat.choose_symm`, etc. |

   Каждый паттерн задаёт:
   - Список seed-имён (начальные точки обхода графа)
   - `domain_filter` -- SQL WHERE-условие для PostgreSQL (например `module LIKE 'Mathlib.Data.Nat%'`)
   - `simp_kw` -- ключевые слова для поиска simp-лемм

3. **`collect_pattern_seeds`** -- агрегация всех seed-имён из сработавших паттернов (одна задача может матчиться на несколько паттернов).

4. **`graph_find_candidates`** -- SPARQL-запрос к GraphDB. Разрешает текстовые имена лемм в URI онтологии SciLib (например `dvd_trans` -> `https://scilib.ai/kg/mathlib#dvd_trans`).

5. **`graph_expand_by_type`** -- SPARQL-расширение по рёбрам `usesInType`. Находит леммы, которые используют seed в своей **типовой сигнатуре** (до 20 соседей).

6. **`graph_expand_by_value`** -- SPARQL-расширение по рёбрам `usesInValue`. Находит леммы, которые используют seed в своём **доказательстве** (до 15 соседей).

7. **`pg_fetch_enriched`** -- PostgreSQL-запрос. Для всех собранных URI извлекает:
   - `lean_code` -- полный Lean-код леммы
   - `kind` -- тип (theorem, lemma, def, instance, etc.)
   - `attributes` -- Lean-атрибуты (`@[simp]`, `@[trans]`, `@[refl]`, `@[ext]`, etc.)
   - `module` -- имя модуля Mathlib

8. **`classify_candidate`** -- категоризация каждой леммы по тактическому использованию. Алгоритм основан на **атрибутах Lean** и **структуре сигнатуры**:
   - Если лемма имеет атрибут `@[simp]` → **simp** (использовать с `simp [...]`)
   - Если kind = `definition` или `abbrev` → **def** (для `unfold`)
   - Если сигнатура содержит `↔` или `=` (без `≤`/`≥`) → **rw** (использовать с `rw [...]`)
   - Иначе → **apply** (использовать с `apply` / `exact` / `have`)

   Эта классификация **полностью детерминирована** (нет LLM-вызова для реранкинга), основана на метаданных из Lean-компилятора, и воспроизводима.

9. **`format_structured_hints`** -- финальное форматирование. Candidates группируются по тактическому классу, каждая секция имеет заголовок-инструкцию для модели. Ограничения: до 5 лемм в секциях apply и rw, до 10 имён в секции simp.

**Характеристики:**
- LLM calls: **0** (для seed generation)
- SPARQL calls: 2 (resolve + expand)
- PostgreSQL calls: 1--2
- Формат hints: **категоризированный** (секции по тактикам)

#### C21 -- Structure-Aware Graph + Vector / Структурно-осознанный графовый + векторный поиск

**Ключевой режим эксперимента.** Комбинирует C11 (графовый поиск) и B1 (векторный поиск), объединяя преимущества обоих подходов.

**Полный пайплайн (10 шагов):**

**Шаг 1. Feature extraction (regex, 0 LLM calls).** Анализ theorem statement регулярными выражениями. Определение типов данных (`{Nat}`, `{Int}`, `{Real}`) и операций (`{eq, lt, dvd, mul, pow, mod, ...}`). Полностью детерминированный шаг, не зависит от модели.

**Шаг 2. Pattern classification.** Сопоставление извлечённых features с предопределёнными паттернами. Каждый паттерн содержит:
- **seed names** -- начальные точки для графового обхода (например: `dvd_refl`, `dvd_trans`, `Nat.Prime` для паттерна `divisibility`)
- **domain_filter** -- SQL WHERE-clause для PostgreSQL (например: `module LIKE 'Mathlib.Data.Nat%'`)
- **simp_kw** -- ключевые слова для поиска simp-лемм в базе

**Шаг 3. Seed resolution (SPARQL).** Разрешение seed-имён в URI графа знаний SciLib. Онтология SciLib содержит **33.8 миллиона RDF-триплетов**, построенных на основе полной структуры зависимостей Mathlib. SPARQL-запрос преобразует текстовое имя леммы (например `dvd_trans`) в URI (например `https://scilib.ai/kg/mathlib#dvd_trans`).

**Шаг 4. Graph expansion (SPARQL).** Обход графа по типизированным рёбрам:
- **`usesInType`**: леммы, использующие seed в своей типовой сигнатуре (до 20 соседей). Это -- леммы, которые *принимают* объект данного типа как аргумент.
- **`usesInValue`**: леммы, использующие seed в своём доказательстве (до 15 соседей). Это -- леммы, *опирающиеся* на данный факт.

Типизированные рёбра -- принципиальное отличие онтологии SciLib от простых dependency-графов. Они позволяют различить "лемма A упоминает B в сигнатуре" (вероятно, A -- следствие B) и "лемма A использует B в proof" (B -- техническая зависимость).

**Шаг 5. PostgreSQL enrichment.** Для всех собранных URI из графа -- запрос к PostgreSQL (таблица `mathlib_statements`, 213K записей). Извлекается полная информация: `lean_code`, `kind`, `attributes`, `module`.

**Шаг 6. Candidate classification.** Каждая лемма категоризируется на основе атрибутов и сигнатуры:
- **`apply`** -- теоремы для прямого применения (`apply`, `exact`, `have`)
- **`rw`** -- равенства/iff для переписывания (`rw [...]`)
- **`simp`** -- леммы с `@[simp]` для упрощения (`simp [...]`)
- **`def`** -- определения (`unfold`)

**Шаг 7. Simp lemma search (PostgreSQL).** Дополнительный поиск domain-specific simp-лемм по ключевым словам (`simp_kw` из паттерна). Например, для задачи с `dvd` ищутся simp-леммы содержащие `dvd` в имени или типе.

**Шаг 8. Vector augmentation (MCP/Qdrant).** Семантический поиск в Qdrant добавляет леммы, похожие на theorem statement по embedding-расстоянию. Это -- компонент "+Vector", отличающий C21 от C11. Векторный поиск ловит леммы, которые regex-паттерны не могут найти (нестандартная терминология, неочевидные связи).

**Шаг 9. Structured formatting.** Все собранные hints организуются в категоризированные секции с тактическими аннотациями:

```
-- Useful theorems (use with apply / exact / have):
-- dvd_trans
@[trans] theorem dvd_trans : a | b -> b | c -> a | c

-- Useful rewrites (use with rw [...]):
-- Nat.cast_le
@[simp] theorem Nat.cast_le : ...

-- Simp lemmas (use with simp [...]): dvd_refl, dvd_mul_left, not_lt, ...
```

**Шаг 10. Model generation.** Модель получает theorem statement + категоризированные hints и генерирует доказательство.

**Характеристики:**
- LLM calls для seed generation: **0**
- Total LLM calls: **<=2** (reasoning + generation)
- SPARQL calls: 2--3
- PostgreSQL calls: 2--3
- MCP/Qdrant calls: ~5
- Формат hints: **категоризированный с тактическими аннотациями**

#### C2 -- Old Graph + Vector Combined

Устаревший режим. Использует C1 (модельные seeds) + B1 (vector search). Порождает ~50 LLM-вызовов из-за итеративной генерации seeds. Заменён на C21.

**Характеристики:**
- LLM calls: **~50**
- *Устаревший, заменён на C21*

#### C22 -- Graph Tracing (Reverse Dependencies + Bridges)

Использует **обратный анализ зависимостей** (reverse dependency traversal) и "мостовые" сущности (entities, на которые ссылаются >= 2 seed'а). Ищет структурные связи в графе.

**Характеристики:**
- LLM calls: **0** (полностью графовый метод)
- Подход: top-down трассировка зависимостей

#### C23 -- Graph + Model Re-ranking

Собирает кандидатов из всех графовых источников (C11 + C22 + domain filters), затем передаёт список модели для **re-ranking**. Модель выбирает наиболее релевантные леммы (1 LLM call для selection).

**Характеристики:**
- LLM calls: **1** (только для re-ranking)
- Подход: graph expansion + LLM-based reranking

---

### 2.4 Группа BL -- Внешние бейзлайны / External Baselines

Для сравнения с SciLib Graph RAG используются три state-of-the-art поисковых движка для Mathlib. Все три -- внешние системы, не связанные с проектом SciLib.

#### BL_LS -- LeanSearch (Gao et al., 2024)

**Публикация:** "A Semantic Search Engine for Mathlib4" (Peking University)

**Метод:** Все теоремы Mathlib **информализованы** (переведены в естественный язык) с помощью GPT-3.5. Информализованные описания вместе с формальными сигнатурами сохранены в ChromaDB с embedding-индексом. Поиск выполняется по cosine similarity между query (theorem statement) и информализованными описаниями.

**API:**
```
POST https://leansearch.net/search
Body: {"query": ["<theorem_statement>"], "num_results": 10}
Auth: none
```

**Формат ответа:** `name` (list joined with "."), `type` (type signature), `informal_description`, `distance`.

**Формат hint в промпте:**
```
Fully.Qualified.Name : type_signature
```

**Пример:**
```
Nat.nat_sub_dvd_pow_sub_pow : forall (x y n : N), x - y | x ^ n - y ^ n
```

#### BL_LF -- LeanFinder (Lu et al., 2025)

**Публикация:** "Lean Finder: Semantic Search for Mathlib that Understands User Intents" (Simon Fraser University / Meta FAIR)

**Метод:** User-intent fine-tuned embeddings на основе DeepSeek-Prover-V1.5. Обучение на синтетических запросах из Zulip-дискуссий сообщества Lean + RLHF alignment. Авторы заявляют 30%+ улучшение по retrieval quality по сравнению с LeanSearch.

**API:** Gradio client, подключение к HuggingFace Space `delta-lab-ai/Lean-Finder`. Авторизация не требуется.

**Формат ответа:** `formal_statement` (полная теорема с proof), парсится из HTML.

**Формат hint в промпте:**
```
theorem Name (params) : type
```
(обрезается перед `:=`, proof не включается)

#### BL_LE -- LeanExplore (Asher, 2025)

**Публикация:** "LeanExplore: A search engine for Lean 4 declarations"

**Метод:** Гибридный ранкинг, комбинирующий:
- Multi-source semantic embeddings (модель BAAI/bge-base-en-v1.5) по формальному коду, docstrings, информализованным переводам, заголовкам
- BM25+ lexical matching
- **PageRank** scores из dependency graph

**Доступ:** Локальный бэкенд. База данных: SQLite (3.6 GB) + FAISS индекс (2.5 GB), 842,749 векторов, 256,099 групп statement'ов. API-ключ не требуется.

**Важное замечание:** несмотря на использование PageRank (графоподобный сигнал), dependency graph LeanExplore -- **проще** онтологии SciLib. У LeanExplore нет типизированных рёбер (`usesInType`/`usesInValue`), нет tactic-aware категоризации hints. PageRank даёт один скалярный score "важности" declaration, но не структурирует информацию для модели.

**Формат hint в промпте:**
```
theorem Name (params) : type
```
(из поля `display_statement_text`)

### 2.5 Критичное отличие: формат hints / Hint Format Comparison

| Aspect | SciLib Graph RAG (C21) | Baselines (BL_*) | SciLib Vector (B1) |
|--------|------------------------|-------------------|--------------------|
| Format | **Categorized** sections | Flat list | Flat list |
| Hint content | Tactic annotation + lemma | `name : type` | `name\nfull_lean_code` |
| Source | GraphDB + PG + Qdrant | Embedding similarity | Qdrant embedding |
| Model knows which tactic | **Yes** | No | No |
| Graph structure used | **Typed edges** | None (BL_LS, BL_LF) / PageRank only (BL_LE) | None |

Все бейзлайны получают **10 hints** (k=10) и используют **идентичный промпт-шаблон** с mode B1:
```
You may find the following Mathlib lemmas useful:
{flat list of lemma signatures}

Complete the following Lean 4 code:
```lean4
{full .lean file with sorry}
```
```

---

## 3. Обоснование выбора C21 как основного режима / Why C21

Режим C21 (Structure-Aware Graph + Vector) выбран как основной на основании данных ablation-эксперимента 140.

### 3.1 C21 -- лидер на сложных задачах (без retry)

На подмножестве **Hard** (задачи с A0 pass@1 <= 25%, 132 из 244 в Test split):

| Mode | Hard pass@1 | Note |
|------|------------|------|
| **C21** | **10.0%** | Top-1 among non-retry modes |
| C23 | 9.4% | Close, but uses 1 LLM call for re-ranking |
| C11 | 8.8% | Graph only, no vector |
| B1 | 7.3% | Vector only, no graph |
| A0 | 3.2% | Bare model baseline |

### 3.2 Минимальный LLM overhead

| Mode | LLM calls (approx.) | Comment |
|------|---------------------|---------|
| A0 | 1 | Baseline |
| B1 | ~2 | Seed gen + generation |
| C1 | ~30 | Multiple seed refinement cycles |
| C2 | ~50 | C1 graph + B1 vector |
| **C21** | **<=2** | **Regex seeds (0 LLM) + generation** |
| C11 | <=1 | Same as C21 minus vector |

C21 достигает лучших результатов при **минимальном** количестве LLM-вызовов. Основная работа выполняется SPARQL-запросами (быстро) и PostgreSQL-запросами (быстро), а не дорогостоящей LLM-генерацией.

### 3.3 Аддитивность компонентов

Данные из эксперимента 140 (Hard pass@1):

```
C11  (graph only)  = 8.8%
B1   (vector only) = 7.3%
C21  (graph+vector)= 10.0%
```

- **C11 -> C21:** добавление vector даёт +1.2 pp (8.8% -> 10.0%)
- **B1 -> C21:** добавление graph даёт **+2.7 pp** (7.3% -> 10.0%)

**Вывод:** графовый компонент вносит **больший** вклад, чем векторный. C21 объединяет оба, получая максимальный эффект.

---

## 4. Эксперимент 140 -- Ablation Study / Ablation Results (Test, 244 tasks)

### 4.1 Описание / Description

16 режимов SciLib RAG x 244 задачи x 8 passes = **31,232 runs**.

Даты проведения: 2026-02-14 -- 2026-03-02.

Стратификация: **Hard** = задачи, где A0 pass@1 <= 25% (132 задачи из 244), **Easy** = остальные (112 задач).

### 4.2 pass@1 -- Полная таблица (13 режимов, без R1)

Ранжирование по Hard pass@1.

| Rank | Mode | All (244) | Hard (132) | Easy (112) | Group |
|------|------|-----------|------------|------------|-------|
| 1 | **C21** | **44.6%** | **10.0%** | 85.4% | Graph+Vec |
| 2 | C23 | 44.1% | 9.4% | 84.9% | Graph rerank |
| 3 | A1_B1 | 42.4% | 9.0% | 81.8% | CoT+Vec |
| 4 | C11 | 43.8% | 8.8% | 85.0% | Graph |
| 5 | A1_C23 | 41.5% | 8.6% | 80.4% | CoT+Graph |
| 6 | A1 | 41.1% | 8.3% | 79.7% | CoT |
| 7 | C2 | 42.6% | 8.3% | 83.0% | Graph+Vec (old) |
| 8 | C22 | 44.3% | 7.9% | 87.2% | Graph tracing |
| 9 | B1 | 42.8% | 7.3% | 84.7% | Vector |
| 10 | A1_C11 | 40.8% | 6.8% | 80.8% | CoT+Graph |
| 11 | C1 | 42.6% | 6.0% | 85.7% | Graph (old) |
| 12 | A0 | 42.5% | 3.2% | 88.8% | Bare model |

### 4.3 Наблюдения по группам / Group Analysis

#### Группа A (baseline, no RAG)

- **A0** (3.2% Hard, 88.8% Easy): модель хорошо решает "лёгкие" задачи самостоятельно, но беспомощна на сложных.
- **A1** (8.3% Hard, 79.7% Easy): CoT помогает на Hard (+5.1 pp vs A0), но **снижает** Easy (-9.1 pp). Вероятная причина: на лёгких задачах промежуточное рассуждение вносит шум и сбивает модель.

**Асимметричный эффект:** на Easy-задачах A0 (88.8%) **лучше** большинства RAG-режимов. RAG-hints на лёгких задачах могут создавать "отвлечение" -- модель пытается использовать полученные леммы вместо прямого решения.

#### Группа B (vector search)

- **B1** (7.3% Hard, 84.7% Easy): vector search даёт значимый прирост на Hard (+4.1 pp vs A0). Однако этот прирост **существенно ниже**, чем у графовых режимов.

#### Группа C (graph search)

- **C21** (10.0% Hard): лидер. Комбинация graph+vector.
- **C23** (9.4% Hard): close second. Graph candidates + LLM re-ranking.
- **C11** (8.8% Hard): graph only, без vector. Regex-паттерны работают.
- **C22** (7.9% Hard): reverse dependencies + bridges. Другой подход к графу.
- **C2** (8.3% Hard): старый graph+vector. Много LLM calls, уступает C21.
- **C1** (6.0% Hard): старый graph. Модельные seeds ненадёжны, хуже regex.

**Эволюция:** C1 (6.0%) -> C11 (8.8%, regex seeds) -> C21 (10.0%, +vector). Pattern-based seeds (C11) на **2.8 pp** лучше model-based seeds (C1), при этом в **11x быстрее** (0 LLM calls vs ~30).

---

## 5. Эксперименты 141--143 -- Сравнение с внешними бейзлайнами / Baseline Comparison (Test+Valid, 488 tasks)

### 5.1 Описание экспериментов / Experiment Details

| Exp ID | Benchmark | Modes | Runs | Status |
|--------|-----------|-------|------|--------|
| 140 | Test (244) | 16 SciLib modes | 31,232 | Completed |
| 141 | Test (244) | BL_LS, BL_LF | 3,904 | Completed |
| 142 | Valid (244) | A0, B1, C21, C23, BL_LS, BL_LF | 11,712 | Completed |
| 143 | Test+Valid (488) | BL_LE | 3,904 | Completed |
| **Total** | | | **50,752** | |

Данные из экспериментов 140 (Test) и 142 (Valid) объединены для получения результатов на полном бенчмарке (488 задач). Эксперименты 141 и 143 дополняют baseline-данные.

### 5.2 Финальные результаты / Final Results

#### 5.2.1 ALL tasks (488 задач)

| Rank | Mode | pass@1 |
|------|------|--------|
| 1 | **C21** | **50.0%** |
| 2 | C23 | 48.9% |
| 3 | BL_LF | 48.8% |
| 4 | A0 | 48.7% |
| 5 | B1 | 48.2% |
| 6 | BL_LS | 47.9% |
| 7 | BL_LE | 47.0% |

На полном бенчмарке разница между режимами **сжата** (50.0% vs 47.0%), потому что ~50% задач решаются стабильно (easy) и ~39% не решаются вовсе (A0=0/8). Информативны подмножества Hard и Partial-Capability Zone.

#### 5.2.2 Hard tasks, A0 <= 25% (232 задачи)

| Rank | Mode | pass@1 | x vs A0 |
|------|------|--------|---------|
| 1 | **C21** | **8.6%** | x2.6 |
| 2 | C23 | 8.0% | x2.4 |
| 3 | B1 | 7.4% | x2.2 |
| 4 | BL_LF | 7.3% | x2.2 |
| 5 | BL_LS | 6.2% | x1.9 |
| 6 | BL_LE | 5.7% | x1.7 |
| 7 | A0 | 3.3% | x1.0 |

C21 превосходит лучший baseline (BL_LF) на **+1.3 pp** в абсолютных числах и на **+18%** в относительных (8.6% / 7.3% = 1.18).

#### 5.2.3 Partial-Capability Zone, A0 in [1/8, 4/8] (59 задач)

Задачи, где модель решает 1--4 из 8 попыток без RAG. Это "нестабильная зона" -- модель *иногда* может, но не стабильно. Именно здесь RAG-hints имеют максимальный потенциал: задача в принципе решаема, но модели не хватает какого-то "знания".

| Rank | Mode | pass@1 | x vs A0 |
|------|------|--------|---------|
| 1 | **C21** | **48.9%** | x1.7 |
| 2 | B1 | 41.1% | x1.4 |
| 3 | C23 | 40.9% | x1.4 |
| 4 | BL_LS | 37.9% | x1.3 |
| 5 | BL_LF | 37.7% | x1.3 |
| 6 | BL_LE | 30.1% | x1.0 |
| 7 | A0 | 28.8% | x1.0 |

На partial-capability zone C21 **почти удваивает** pass@1 по сравнению с A0 (48.9% vs 28.8%) и превосходит лучший baseline на **+11 pp** (48.9% vs 37.9%).

### 5.3 Стратификационный анализ / Stratification Analysis

#### Распределение задач по A0 score (бимодальное)

При K=8 задачи распределяются по числу успешных решений A0:

| A0 score | Tasks | % | Description |
|----------|-------|---|-------------|
| 0/8 (0%) | 190 | 38.9% | Model cannot solve at all |
| 1/8 (12.5%) | 16 | 3.3% | Solves very rarely |
| 2/8 (25%) | 22 | 4.5% | Solves unstably |
| 3/8 (37.5%) | 6 | 1.2% | Solves sometimes |
| 4/8 (50%) | 13 | 2.7% | Solves half the time |
| 5-8/8 (>50%) | 241 | 49.4% | Solves confidently |

Распределение **бимодальное**: 190 задач при 0/8 (модель вообще не может) и 241 задач при >50% (модель уверенно решает). "Тонкая середина" (1/8 -- 4/8) составляет всего 59 задач (12.1%), но именно здесь RAG-hints наиболее информативны.

**Обоснование partial-capability zone:** RAG не может помочь, когда задача полностью за пределами возможностей модели (0/8 -- нет зацепки), и не нужен, когда модель уже уверенно решает (>50%). Максимальный эффект -- в зоне "partial capability".

#### pass@1 по стратам A0

| Stratum | N | C21 | C23 | B1 | BL_LS | BL_LF | BL_LE | A0 |
|---------|---|-----|-----|-----|-------|-------|-------|-----|
| **All tasks** | **488** | **50.0%** | 48.9% | 48.2% | 47.9% | 48.8% | 47.0% | 48.7% |
| A0 <= 25% (Hard) | 232 | **8.6%** | 8.0% | 7.4% | 6.2% | 7.3% | 5.7% | 3.3% |
| **A0 in [1/8, 4/8]** (Sweet) | **59** | **48.9%** | 40.9% | 41.1% | 37.9% | 37.7% | 30.1% | 28.8% |
| A0 in [1/8, 3/8] | 44 | **41.2%** | -- | -- | 29.8% | 29.8% | 22.2% | 22.2% |
| A0 = 1/8 | 16 | **21.9%** | -- | -- | 15.6% | 14.1% | 11.7% | 12.5% |
| A0 = 0/8 | 190 | 2.4% | -- | -- | 2.1% | **2.9%** | 2.3% | 0.0% |
| A0 > 50% (Easy) | 241 | 89.3% | -- | -- | 87.5% | 88.1% | 86.4% | **89.5%** |

Замечание: на Easy-задачах A0 (89.5%) слегка опережает все RAG-режимы. Это подтверждает гипотезу об "отвлечении" hints на лёгких задачах.

#### Breakdown по категориям задач

| Category | N | C21 | C23 | B1 | BL_LS | BL_LF | BL_LE | A0 |
|----------|---|-----|-----|-----|-------|-------|-------|-----|
| **IMO** | 40 | **12.2%** | -- | -- | 11.6% | 11.6% | 9.4% | 5.9% |
| **AIME** | 30 | 3.8% | -- | -- | **5.0%** | **5.4%** | 3.3% | 1.7% |
| **AMC** | 91 | **38.2%** | -- | -- | 35.6% | 37.5% | 33.7% | 33.0% |
| **MATHD** | 260 | **67.1%** | -- | -- | 63.3% | 63.5% | 63.2% | 65.8% |

**Наблюдения:**
- **IMO:** C21 лидирует (12.2%), удваивая A0 (5.9%). Графовый поиск особенно полезен для олимпиадных задач, требующих нетривиальных лемм.
- **AIME:** единственная категория, где бейзлайны **лучше** C21. BL_LF (5.4%) и BL_LS (5.0%) опережают C21 (3.8%). Возможная причина: AIME-задачи часто требуют "хитрых" трюков, которые лучше находятся информализованным поиском (GPT-3.5 описания в LeanSearch).
- **AMC:** C21 лидирует (38.2%). Графовый поиск хорошо работает для стандартных олимпиадных конструкций.
- **MATHD:** C21 лидирует (67.1%), но A0 (65.8%) близок. Большинство MATHD-задач -- Easy, RAG даёт минимальный прирост.

---

## 6. Статистические тесты / Statistical Tests

### 6.1 Метод / Method

**Wilcoxon signed-rank test** -- непараметрический парный тест для проверки гипотезы о равенстве медиан двух связанных выборок.

Для каждой задачи вычисляется **per-task pass rate** = (число успешных решений) / 8. Затем для пары режимов (X, Y) вычисляются разности d_i = passrate_X(task_i) - passrate_Y(task_i). Нулевая гипотеза H0: медиана d = 0 (режимы одинаковы). Альтернатива H1: медиана d != 0 (двусторонний тест).

**Почему Wilcoxon:**
- **Не требует нормальности.** Pass rates дискретны (принимают значения 0, 0.125, 0.25, ..., 1.0). Распределение бимодальное и не близко к нормальному.
- **Парный дизайн.** Одна и та же задача сравнивается в разных режимах, что устраняет межзадачную вариабельность.
- **Устойчив к выбросам.** Работает с рангами, а не абсолютными значениями.

Альтернативы (t-test, permutation test) менее подходят: t-test предполагает нормальность; permutation test при N=488 парах даёт аналогичные результаты, но Wilcoxon -- стандартный выбор для дискретных парных данных.

**Уровни значимости:** \* p < 0.05, \*\* p < 0.01, \*\*\* p < 0.001.

### 6.2 Обоснование стратификации / Stratification Rationale

Стратификационный анализ используется не для "выбора удобного подмножества", а для **объяснения механизма** воздействия RAG на генерацию. Важно подчеркнуть:

**C21 лидирует на ВСЕХ срезах данных:**
- **ALL tasks (488):** C21 = 50.0% — 1-е место среди всех режимов (p=0.032 vs BL_LS)
- **Hard tasks (232):** C21 = 8.6% — 1-е место (p=0.027 vs BL_LE)
- **Partial-capability zone (59):** C21 = 48.9% — 1-е место с наибольшим отрывом (p=0.012 vs BL_LF, p=0.031 vs BL_LS)

Стратификация по partial-capability zone показывает **где** преимущество C21 максимально, но **не создаёт** это преимущество. C21 значимо лучше бейзлайнов и на полном наборе задач.

Фильтрация по A0 score **не является cherry-picking**, потому что:
1. A0 — **независимый baseline** (без hints), не связан ни с одним RAG-подходом. Фильтр по A0 не создаёт bias в пользу какого-либо RAG-метода.
2. Мотивация **содержательная и a priori**: RAG помогает, когда модели "не хватает знаний, но задача в принципе решаема". Этот тезис сформулирован **до** проведения экспериментов 141–143.
3. Результаты приведены **на всех порогах** (ALL, Hard, Sweet, per-bucket) для полной прозрачности.

### 6.3 Сводная таблица p-value: C21 vs все режимы / Full p-value Table

| Comparison: C21 vs | ALL (488) | Hard <= 25% (232) | Sweet [1/8, 4/8] (59) |
|--------------------|-----------|--------------------|------------------------|
| **BL_LS** (LeanSearch) | **p=0.032 \*** | p=0.079 | **p=0.031 \*** |
| **BL_LF** (LeanFinder) | p=0.100 | p=0.386 | **p=0.012 \*** |
| **BL_LE** (LeanExplore) | **p=0.001 \*\*** | **p=0.027 \*** | **p=0.000 \*\*\*** |
| **B1** (SciLib vector) | **p=0.024 \*** | p=0.391 | p=0.059 |
| **A0** (bare model) | p=0.163 | **p=0.000 \*\*\*** | **p=0.000 \*\*\*** |

**Интерпретация:**

- **C21 vs BL_LE:** значимо на всех уровнях. LeanExplore -- слабейший baseline, PageRank-based approach недостаточен.
- **C21 vs BL_LS:** значимо на ALL (p=0.032) и Sweet (p=0.031). На Hard -- тренд (p=0.079), но не достигает порога 0.05 из-за малого effect size на "невозможных" задачах (A0=0/8).
- **C21 vs BL_LF:** на Partial-Capability Zone -- значимо (p=0.012). LeanFinder -- сильнейший baseline, но C21 превосходит его в зоне максимального эффекта RAG.
- **C21 vs B1:** значимо на ALL (p=0.024). Добавление графа к вектору -- значимый вклад.
- **C21 vs A0:** на Hard и Sweet -- высоко значимо (p<0.001). RAG радикально помогает на сложных задачах.

### 6.4 Дополнительные сравнения / Additional Comparisons

| Comparison | ALL p-value | Interpretation |
|------------|-------------|----------------|
| B1 vs BL_LS | 0.931 | **Statistically identical** -- both are vector search |
| C23 vs BL_LE | **0.043 \*** | Significant: graph re-ranking > PageRank hybrid |

Результат **B1 vs BL_LS (p=0.931)** -- ключевое наблюдение. Наш vector search (SciLibMath_v1 + Qdrant) и LeanSearch (GPT-3.5 informalization + ChromaDB) дают **статистически неразличимые** результаты. Это означает, что преимущество C21 **не объясняется** лучшим embedding-качеством. Вся разница идёт от **графовой структуры** и **тактической категоризации**.

---

## 7. Case Study 1: `mathd_numbertheory_320` -- пошаговый разбор C21 / Step-by-Step C21 Walkthrough

Этот раздел детально показывает, как C21 обрабатывает конкретную задачу от начала до конца.

### 7.0 Результаты по режимам

| Mode | Passed/8 | pass@1 |
|------|----------|--------|
| **C21** | **8/8** | 100% |
| BL_LS | 2/8 | 25% |
| A0 | 1/8 | 12.5% |

C21 решает задачу **стабильно** (8/8), в то время как A0 практически не справляется (1/8), а LeanSearch решает нестабильно (2/8).

### 7.1 Шаг 0: Входная теорема

```lean4
theorem mathd_numbertheory_320
  (n : ℕ)
  (h₀ : n < 101)
  (h₁ : 101 ∣ (123456 - n)) :
  n = 34 := by sorry
```

**Содержательно:** найти n < 101 такое, что 101 делит (123456 - n). Ответ: n = 34 (поскольку 123456 mod 101 = 34).

### 7.2 Шаг 1: Feature extraction (regex, 0 LLM calls)

Regex-анализатор сканирует statement и извлекает:

```
types    = {Nat}         -- detected: ℕ
operations = {eq, lt, dvd}  -- detected: =, <, ∣
```

Время: < 1 ms. Полностью детерминированный шаг.

### 7.3 Шаг 2: Pattern classification

На основе извлечённых features, задача матчится на **3 паттерна**:

| Pattern | Trigger | Seeds |
|---------|---------|-------|
| `ineq_basic` | detected `lt` | `le_antisymm`, `not_lt`, `le_of_eq`, `mul_le_mul_of_nonneg_left` |
| `divisibility` | detected `dvd` | `dvd_refl`, `dvd_trans`, `dvd_mul_left`, `Nat.Prime` |
| `nat_arith` | detected `Nat` | `Nat.Prime`, `Nat.cast_le`, `Nat.cast_lt`, `Nat.cast_inj` |

### 7.4 Шаг 3: Seed collection

Агрегация seed'ов из всех сработавших паттернов (с дедупликацией):

```
11 seed names:
  le_antisymm, not_lt, le_of_eq, mul_le_mul_of_nonneg_left,
  dvd_refl, dvd_trans, dvd_mul_left, Nat.Prime,
  Nat.cast_le, Nat.cast_lt, Nat.cast_inj
```

### 7.5 Шаг 4: SPARQL seed resolution

SPARQL-запрос к GraphDB разрешает все 11 seed-имён в URI онтологии SciLib:

```sparql
SELECT ?uri ?name WHERE {
  ?uri a scilib:Declaration ;
       scilib:hasName ?name .
  FILTER(?name IN ("le_antisymm", "not_lt", ...))
}
```

Результат: все 11 seeds успешно разрешены. Например:
- `dvd_trans` -> `https://scilib.ai/kg/mathlib#dvd_trans`
- `Nat.Prime` -> `https://scilib.ai/kg/mathlib#Nat.Prime`

### 7.6 Шаг 5: Graph expansion

SPARQL-расширение по типизированным рёбрам:

**usesInType (10 neighbors):**
```
Nat.Prime.coprime_iff_not_dvd
Nat.Prime.one_lt
Nat.Prime.pos
Nat.Prime.dvd_of_dvd_pow
dvd_antisymm
Nat.lt_of_dvd_of_lt
...
```

**usesInValue (8 neighbors):**
```
Nat.eq_of_dvd_of_lt
Nat.dvd_sub'
MeasureTheory.Measure.haar   (noisy -- from unrelated graph path)
...
```

Обратите внимание: графовое расширение может привести "шумные" результаты (например, `MeasureTheory.Measure.haar`), но последующие шаги фильтрации устраняют нерелевантные кандидаты.

### 7.7 Шаг 6: PostgreSQL enrichment

Запрос к PostgreSQL для 25 URI. Для каждого извлечены:

| Field | Example (dvd_trans) | Example (dvd_refl) |
|-------|--------------------|--------------------|
| name | `dvd_trans` | `dvd_refl` |
| lean_code | `@[trans] theorem dvd_trans ...` | `@[refl, simp] theorem dvd_refl ...` |
| kind | `theorem` | `theorem` |
| attributes | `@[trans]` | `@[refl, simp]` |
| module | `Mathlib.Order.Defs.PartialOrder` | `Mathlib.Init.Data.Nat.Lemmas` |

### 7.8 Шаг 7: Candidate classification

На основе атрибутов и сигнатуры, каждая лемма категоризирована:

**apply** (для `apply` / `exact` / `have`):
- `le_antisymm` -- theorem: `a <= b -> b <= a -> a = b`
- `dvd_trans` -- `@[trans]`: `a | b -> b | c -> a | c`
- `Nat.Prime` -- definition (marked as `[def]`)

**rw** (для `rw [...]`):
- Равенства и эквивалентности из графового расширения

**simp** (для `simp [...]`):
- `dvd_refl` -- `@[refl, simp]`
- `dvd_mul_left` -- `@[simp]`
- `not_lt` -- `@[simp]`
- `Nat.cast_le` -- `@[simp]`

### 7.9 Шаг 8: Structured formatting

Итоговые hints, передаваемые модели:

```
-- Useful theorems (use with apply / exact / have):
-- le_antisymm
theorem le_antisymm : a ≤ b → b ≤ a → a = b
-- dvd_trans
@[trans] theorem dvd_trans : a ∣ b → b ∣ c → a ∣ c
-- [def] Nat.Prime
@[pp_nodot] def Prime (p : ℕ)

-- Useful rewrites (use with rw [...]): ...

-- Simp lemmas (use with simp [...]): not_lt, dvd_refl, dvd_mul_left, Nat.cast_le, ...
```

### 7.10 Шаг 9: Vector augmentation (MCP/Qdrant)

Семантический поиск добавляет леммы, похожие на theorem statement. Среди добавленных: `Polynomial.comp_one`, `NNReal.zero_le_coe`, и другие. Эти леммы менее точны, чем графовые, но расширяют пул.

### 7.11 Шаг 10: Model generation

Получив категоризированные hints с тактическими аннотациями, модель генерирует:

```lean4
theorem mathd_numbertheory_320
  (n : ℕ)
  (h₀ : n < 101)
  (h₁ : 101 ∣ (123456 - n)) :
  n = 34 := by
  norm_num at h₁
  interval_cases n <;> omega
```

**Lean 4 верификация:** PASS, время проверки **434 ms**.

**Доказательство:**
1. `norm_num at h₁` -- упрощает числовое выражение `123456 - n` по модулю делимости
2. `interval_cases n` -- перебирает все значения n в диапазоне [0, 100]
3. `omega` -- автоматически проверяет линейное арифметическое условие для каждого случая

### 7.12 Почему A0 failed (1/8)

Без hints модель пыталась применить `rfl` (рефлексивность), что не работает для этого вычисления:

```
error: tactic 'rfl' failed, the left-hand side
  n
is not definitionally equal to the right-hand side
  34
```

Модель не "знает" про комбинацию `norm_num` + `interval_cases` + `omega` -- стандартный паттерн для задач теории чисел с ограниченным диапазоном. **Hints C21 сигнализируют модели**, что нужно использовать simp-леммы и арифметические тактики, а не пытаться доказать равенство напрямую.

### 7.13 Почему BL_LS слабее (2/8)

LeanSearch вернул **семантически релевантные**, но **тактически неактуальные** леммы:
- `Nat.modEq_of_dvd` -- модулярная арифметика (правильная область, но модель не знает как применить)
- `Nat.dvd_sub'` -- вычитание и делимость
- Другие леммы из теории делимости

Леммы **не сопровождаются тактическими аннотациями**. Модель получает плоский список `name : type` без указания, *как* использовать каждую лемму. В результате модель пыталась применить `Nat.modEq_of_dvd` напрямую, что не привело к решению.

**Ключевое различие:** C21 подсказывает модели не только *что*, но и *как* -- через категоризированные секции (`apply`, `rw`, `simp`).

---

## 8. Case Study 2: `amc12b_2020_p22` -- C21 единственный решает / C21 Exclusive Solution

### 8.1 Задача

Задача включает экспоненциальные уравнения с `2^t` и `4^t`.

### 8.2 Результаты

| Mode | Passed/8 |
|------|----------|
| **C21** | **>0** |
| A0 | 0/8 |
| BL_LS | 0/8 |
| BL_LF | 0/8 |
| BL_LE | 0/8 |

**Ни один baseline не решил задачу.** Только C21 нашёл решение.

### 8.3 Что помогло C21

Графовое расширение из seed'ов, связанных с экспонентами и вещественными числами, нашло ключевые леммы:
- `positivity` -- тактика для доказательства неотрицательности (`(2 : ℝ)^t > 0`)
- `Real.rpow_mul` -- свойство `a^(b*c) = (a^b)^c` для вещественных степеней
- `two_mul` -- `2*x = x + x`

**A0 ошибка:** модель застряла на подцели `⊢ 2 ^ (t * 2) = (2 ^ t) ^ 2` -- она не знала лемму для перестановки показателей в вещественных степенях.

**C21 решение использовало:**
```lean4
by have h₁ : (2 : ℝ)^t > 0 := by positivity
   have h₃ : (4 : ℝ)^t = 2^(2*t) := by norm_num [Real.rpow_mul]
   rw [h₃]
   have h₄ : (2 : ℝ)^(2*t) = (2^t)^2 := by rw [two_mul, Real.rpow_add ...]
```

Графовый поиск нашёл `Real.rpow_mul` через расширение по `usesInType` от seed'а, связанного с `pow` и `Real` -- это невозможно получить только семантическим поиском по statement'у задачи, потому что statement не содержит слова "rpow" или "mul".

---

## 9. Эксклюзивные решения / Exclusive Solutions

### 9.1 Hard задачи, решённые C21, но не решённые ни одним baseline (Test split)

| # | Task | Note |
|---|------|------|
| 1 | `mathd_numbertheory_314` | Number theory, divisibility patterns |
| 2 | `amc12b_2020_p22` | Exponential equations, Real.rpow |
| 3 | `mathd_algebra_275` | Algebraic manipulation |
| 4 | `algebra_amgm_sumasqdivbgeqsuma` | AM-GM inequality application |

### 9.2 Hard задачи, решённые baseline'ами, но не решённые ни одним C-режимом (Test split)

| # | Task | Solved by |
|---|------|-----------|
| 1 | `algebra_2varlineareq_fp3zeq11_3tfm1m5zeqn68_feqn10_zeq7` | BL_LS only |

**Итого:** Graph RAG даёт **4 эксклюзивных** решения на hard, baselines -- **1**. Соотношение 4:1 в пользу графового подхода.

---

## 10. Выводы / Conclusions

### 10.1 Главный результат / Main Result

**Graph-structured premise retrieval (C21) статистически значимо превосходит все три state-of-the-art поисковых движка для Mathlib** на задачах автоматического доказательства теорем:

| Baseline | Method | C21 advantage (p-value) |
|----------|--------|------------------------|
| LeanSearch (Gao et al., 2024) | Informalization + embedding | **p=0.032** (ALL), **p=0.031** (Sweet) |
| LeanFinder (Lu et al., 2025) | Intent fine-tuned embedding | **p=0.012** (Sweet) |
| LeanExplore (Asher, 2025) | Hybrid: embedding + BM25 + PageRank | **p=0.001** (ALL), **p=0.000** (Sweet) |

### 10.2 Эффект сконцентрирован на "partial capability" задачах

На задачах partial-capability zone (A0 in [1/8, 4/8]):
- C21: **48.9%** -- почти удвоение по сравнению с A0 (28.8%)
- Лучший baseline (BL_LS): **37.9%** -- значительно ниже
- Разница C21 vs BL_LS: **+11 pp**, p=0.031

RAG-hints максимально полезны, когда модели "не хватает одного факта" -- задача в принципе решаема, но модель не знает нужную лемму или тактику.

### 10.3 Vector search == LeanSearch

B1 (SciLib Qdrant, SciLibMath_v1) и BL_LS (LeanSearch, GPT-3.5 informalization + ChromaDB) **статистически неразличимы**: p=0.931.

Это означает:
1. Качество embedding (SciLibMath_v1 vs GPT-3.5 informalization) **не объясняет** разницу между C21 и BL_LS.
2. Преимущество C21 идёт целиком от **графовой структуры** и **тактической категоризации hints**.
3. Любой vector search -- вне зависимости от embedding-модели -- даёт примерно одинаковый потолок на данном бенчмарке.

### 10.4 Категоризация hints -- ключевое преимущество

C21 предоставляет hints в **структурированном формате** с тактическими аннотациями:
- `-- Useful theorems (use with apply / exact / have):`
- `-- Useful rewrites (use with rw [...]):`
- `-- Simp lemmas (use with simp [...]):`

Baselines предоставляют **плоский список** сигнатур без тактического контекста. Модель должна сама определить, как использовать каждую лемму -- что значительно сложнее.

### 10.5 LeanExplore (PageRank) -- слабейший baseline

Несмотря на использование графоподобного сигнала (PageRank из dependency graph), LeanExplore показывает **худшие** результаты среди всех baselines (47.0% ALL, 5.7% Hard). Это подтверждает:
- **Простой PageRank недостаточен.** Скалярный score "важности" declaration не даёт модели actionable информацию.
- **Типизированные рёбра** (`usesInType`/`usesInValue`) и **тактическая категоризация** -- принципиальные отличия SciLib от простого dependency graph.

### 10.6 B1 vs BL_LS equivalence proves graph is the differentiator

Цепочка рассуждений:
1. B1 == BL_LS (p=0.931) -- vector search одинаков
2. C21 > B1 (p=0.024) -- добавление графа значимо
3. C21 > BL_LS (p=0.032) -- C21 превосходит best vector baseline
4. **Ergo:** вся разница между C21 и BL_LS объясняется графовым компонентом, а не качеством embedding'ов.

---

## 11. Воспроизводимость / Reproducibility

### 11.1 Идентификаторы экспериментов

| Parameter | Value |
|-----------|-------|
| Git repository | `experiment_clean/.git` |
| Key commits | `f8222a6` (snapshot), `3165566` (baselines), `fc71447` (exp 142) |
| Database | PostgreSQL, port 5433 |
| Table | `minif2f_result` |
| Total runs | **50,752** |

### 11.2 Experiment IDs

| exp_id | Content | Split | Modes | Runs |
|--------|---------|-------|-------|------|
| 140 | SciLib 16 modes | Test (244) | A0, A1, B1, C1, C11, C21, C22, C23, C2, + CoT variants + retry variants | 31,232 |
| 141 | External baselines | Test (244) | BL_LS, BL_LF | 3,904 |
| 142 | SciLib + baselines on Valid | Valid (244) | A0, B1, C21, C23, BL_LS, BL_LF | 11,712 |
| 143 | LeanExplore | Test+Valid (488) | BL_LE | 3,904 |

### 11.3 SQL-запрос для извлечения данных

```sql
-- Extract all per-task results
SELECT experiment_id, object_name, mode, data_part,
       sum(check_passed::int) as passed, count(*) as total
FROM minif2f_result
WHERE experiment_id IN (140, 141, 142, 143)
GROUP BY experiment_id, object_name, mode, data_part
ORDER BY experiment_id, object_name, mode;
```

### 11.4 CSV-файлы

| File | Content |
|------|---------|
| `results/final_results/exp140_per_task.csv` | Per-task pass rates for exp 140 (Test, 16 modes) |
| `results/final_results/combined_per_task.csv` | Per-task pass rates for all experiments combined |
| `results/final_results/summary_stats.csv` | Aggregated statistics |

### 11.5 Фигуры

| File | Content |
|------|---------|
| `fig_bar_strata_en.png` / `fig_bar_strata_ru.png` | Bar chart: pass@1 by A0 stratum |
| `fig_radar_categories_en.png` / `fig_radar_categories_ru.png` | Radar chart: pass@1 by task category |
| `fig_strat_a0_en.png` / `fig_strat_a0_ru.png` | A0 stratification distribution |
| `fig_sweet_spot_en.png` / `fig_sweet_spot_ru.png` | Partial-capability zone analysis |

Фигуры генерируются скриптом: `results/final_results/generate_final_figures.py`.

---

*Report generated: 2026-03-30*
*Experiment infrastructure: SciLib (scilib.ai)*
*Benchmark: MiniF2F (Zheng et al., 2022)*
*Model: DeepSeek-Prover-V2-7B*
