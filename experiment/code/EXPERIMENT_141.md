# Experiment 141 — External Baselines Comparison

## Постановка задачи

Сравнить три внешних поисковых системы (premise retrieval) с нашей системой SciLib RAG
на задаче автоматического доказательства теорем. Все три бейзлайна — это публичные
search engines для Mathlib4, которые возвращают релевантные леммы по запросу.

**Гипотеза:** SciLib RAG (graph + vector + ontology) даёт более качественные подсказки
для модели, чем generic semantic search, особенно на сложных задачах (AIME/IMO/AMC).

**Сравнение с экспериментом 140:** Эксп. 140 тестировал 16 внутренних режимов SciLib.
Эксп. 141 тестирует 3 внешних бейзлайна в тех же условиях, чтобы результаты были
напрямую сопоставимы.

## Три бейзлайна

| ID режима | Источник | Статья | Метод premise retrieval |
|-----------|----------|--------|------------------------|
| **BL_LS** | LeanSearch (leansearch.net) | Gao et al., 2024 "A Semantic Search Engine for Mathlib4" | Informalization GPT-3.5 → embedding → ChromaDB cosine |
| **BL_LE** | LeanExplore (leanexplore.com) | Asher, 2025 "LeanExplore: A search engine for Lean 4 declarations" | Multi-source embeddings (bge-base-en-v1.5) + BM25+ + PageRank |
| **BL_LF** | LeanFinder (HuggingFace) | Lu et al., 2025 "Lean Finder: Semantic Search for Mathlib that Understands User Intents" | User-intent fine-tuned embeddings + synthetic queries + RLHF |

### Что делает каждый бейзлайн в контексте эксперимента

1. Получает formal statement задачи (теорему из MiniF2F)
2. Отправляет запрос в публичный API внешней системы
3. Получает список релевантных лемм Mathlib
4. Форматирует их как hints в промпте (тот же шаблон что mode B1 в эксп. 140)
5. Передаёт промпт модели DeepSeek-Prover-V2-7B
6. Проверяет результат через Lean checker (Kafka)

## Обязательные требования

### 1. Идентичность условий (КРИТИЧНО)

Все параметры должны быть **ровно такими же**, как в эксперименте 140:

| Параметр | Значение | Источник |
|----------|----------|----------|
| Модель | `deepseek-ai/DeepSeek-Prover-V2-7B` | config.py: MODEL_ID |
| Температура | 0.6 | config.py: MODEL_TEMPERATURE |
| max_new_tokens | 8192 | model.py: generate() default |
| top_p | 0.95 | model.py: generate() default |
| top_k | 40 | model.py: generate() default |
| repetition_penalty | 1.05 | model.py: generate() default |
| Benchmark | MiniF2F Test | 244 задачи |
| Pass@K | 8 | 8 попыток на каждую (task, mode) пару |
| Lean timeout | 30s | config.py: LEAN_CHECK_TIMEOUT |
| LEAN_WRAPPER | `import Mathlib\nimport Aesop\nset_option maxHeartbeats 2000000\n\n` | config.py |
| Validation | `_has_proof_content()` | solver.py — отсекает comment-only и NL |
| Prompt layout | hints BEFORE code fence | solver.py: `_HINTS_PREFIX + _CODE_FENCE` |

### 2. Формат промпта (идентичный mode B1)

```
You may find the following Mathlib lemmas useful:
{hints от внешнего API — список лемм}

Complete the following Lean 4 code:

```lean4
{formal_statement — полный .lean файл задачи}
```
```

Hints — это строки вида `theorem name : type` или `name : signature`, по одной на строку.
Формат нормализуется из ответа каждого API в единообразный вид.

### 3. Те же 244 задачи

Используются ровно те же задачи из `data/miniF2F-lean4/MiniF2F/Test/*.lean`,
что и в эксперименте 140. Порядок обхода задач определяется `RANDOM_SEED=42`.

### 4. Сохранность данных (КРИТИЧНО)

- **exp_id = 141** — все результаты пишутся с этим ID в таблицу `minif2f_result` (PostgreSQL)
- Данные экспериментов 92 и 140 **НЕ ТРОГАЮТСЯ** — ни перезаписью, ни удалением
- Resume support: повторный запуск пропускает уже завершённые (task, mode, variant) тройки
- data_source = "miniF2F-lean4", data_part = "Test" — как в эксп. 140

### 5. Код эксперимента — изолирован

- Новый код **только** в `baselines_paper/` (retrieval.py, run_baselines.py)
- Существующие файлы (run_experiment.py, solver.py, rag.py и т.д.) **НЕ модифицируются**
- Используем modules/model.py, modules/db.py, modules/solver.py через import
- Git snapshot сделан перед началом работы: коммит `f8222a6`

## API доступа к бейзлайнам

### LeanSearch (BL_LS)
- Эндпоинт: `POST https://leansearch.net/search`
- Авторизация: не требуется
- Тело: `{"query": ["<formal_statement>"], "num_results": 10}`
- Ответ: `[[ {"result": {"name": [...], "type": "...", ...}, "distance": 0.23}, ... ]]`
- Из ответа берём: name (join ".") + type → формируем hint

### LeanExplore (BL_LE)
- Эндпоинт: `GET https://www.leanexplore.com/api/v2/search?q=<query>&limit=10`
- Авторизация: Bearer token (бесплатная регистрация)
- Ответ: `{"results": [{"name": "...", "source_text": "...", "informalization": "..."}, ...]}`
- Из ответа берём: name + source_text (сигнатура) → формируем hint

### LeanFinder (BL_LF)
- Эндпоинт: Gradio API на HuggingFace (`delta-lab-ai/Lean-Finder`)
- Авторизация: не требуется
- Вызов: `gradio_client.Client("delta-lab-ai/Lean-Finder").predict(query, k, "Normal")`
- Из ответа берём: formal_statement → формируем hint

## Критичное отличие: формат подсказок

Наша система SciLib RAG (особенно mode C11) даёт **категоризированные** подсказки,
разбитые на секции по тактикам:
```
-- Useful theorems (use with apply / exact / have):
-- dvd_trans
-- Useful rewrites (use with rw [...]):
-- mul_comm
-- Simp lemmas (use with simp [...]): dvd_refl, dvd_mul_left, ...
```

Модель знает **какой тактикой** использовать каждую лемму (apply, rw, simp).
Источник: GraphDB (онтология SciLib, edges usesInType/usesInValue) + PostgreSQL.

Бейзлайны дают **плоский список** лемм без указания способа применения:
```
Nat.pow_mod (a b n : ℕ) : a ^ b % n = (a % n) ^ b % n
Int.ModEq.pow_eq_pow : Nat.Prime p → p - 1 ∣ x - y → ...
```

Это **честное сравнение** — бейзлайны не имеют доступа к нашей онтологии и
структурным зависимостям. Формат промпта для бейзлайнов идентичен нашему mode B1
(плоский список hint'ов → code fence), т.к. B1 тоже не категоризирует.

### Количество и вес hint'ов (сопоставимость)

Из анализа реальных промптов эксп. 140:

| Режим | Avg блоков/строк | Avg chars в секции hints | Формат одного hint |
|-------|------------------|--------------------------|---------------------|
| B1 (наш vector) | ~10 блоков | ~2868 | name + полный lean-код (~300 chars) |
| C11 (struct graph) | ~21 строк | ~1617 | категоризированные секции |
| C23 (graph rerank) | ~7 строк | ~445 | компактные сигнатуры |
| **BL_LS (LeanSearch)** | **~10 строк** | **~1354** | `name : type` (~130 chars) |
| **BL_LF (LeanFinder)** | **~10 строк** | **~1325** | `theorem name : type` (~130 chars) |

Бейзлайны получают **10 hint'ов** (k=10). Это сопоставимо с B1 по количеству блоков,
но каждый hint бейзлайна содержит **только сигнатуру** (не полный исходник леммы).
Бейзлайны дают модели **меньше информации на hint** чем B1, что делает сравнение
консервативным (в пользу бейзлайнов, если они покажут похожие результаты).

## Ожидаемый результат

Таблица pass@1 и pass@8 для трёх бейзлайнов на тех же 244 задачах,
с разбивкой на All / Hard (132 задачи с A0 ≤ 25%),
напрямую сопоставимая с таблицей из report_exp140.md.

## Структура файлов

```
baselines_paper/
├── EXPERIMENT_141.md     # Этот файл — постановка задачи
├── retrieval.py          # Три функции premise retrieval (LeanSearch, LeanExplore, LeanFinder)
├── run_baselines.py      # Runner эксперимента 141
├── 2403.13310v2.pdf      # Статья LeanSearch
├── 2506.11085v1 (1).pdf  # Статья LeanExplore
└── 2510.15940v1.pdf      # Статья LeanFinder
```
