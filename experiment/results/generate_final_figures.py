#!/usr/bin/env python3
"""Generate final figures for experiments 140-143 report.

Produces bar charts, radar charts in both Russian and English.
"""

import csv
import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch
import os

OUT = os.path.dirname(os.path.abspath(__file__))

# ═══ Load data ═══

def load_combined():
    rows = []
    with open(os.path.join(OUT, 'combined_per_task.csv')) as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append(row)
    return rows

def load_summary():
    rows = []
    with open(os.path.join(OUT, 'summary_stats.csv')) as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append(row)
    return rows

# ═══ Color scheme ═══

COLORS = {
    'A0': '#9E9E9E',
    'B1': '#42A5F5',
    'C21': '#2E7D32',
    'C23': '#66BB6A',
    'BL_LS': '#EF5350',
    'BL_LF': '#FF7043',
    'BL_LE': '#FFA726',
}

MODE_LABELS = {
    'A0': 'A0 (Bare)',
    'B1': 'B1 (Vector)',
    'C21': 'C21 (Graph+Vec)',
    'C23': 'C23 (Graph Rerank)',
    'BL_LS': 'LeanSearch',
    'BL_LF': 'LeanFinder',
    'BL_LE': 'LeanExplore',
}

MODE_LABELS_RU = {
    'A0': 'A0 (Без RAG)',
    'B1': 'B1 (Вектор)',
    'C21': 'C21 (Граф+Вектор)',
    'C23': 'C23 (Граф Реранк)',
    'BL_LS': 'LeanSearch',
    'BL_LF': 'LeanFinder',
    'BL_LE': 'LeanExplore',
}

MODES_ORDER = ['A0', 'B1', 'C21', 'C23', 'BL_LS', 'BL_LF', 'BL_LE']


def make_bar_chart(data_dict, title, filename, ylabel='pass@1 (%)', lang='en'):
    """Bar chart comparing modes across strata."""
    labels = MODE_LABELS if lang == 'en' else MODE_LABELS_RU

    fig, ax = plt.subplots(figsize=(12, 6))

    strata_names = list(data_dict.keys())
    x = np.arange(len(strata_names))
    width = 0.11
    n_modes = len(MODES_ORDER)

    for i, mode in enumerate(MODES_ORDER):
        offset = (i - n_modes/2 + 0.5) * width
        vals = [data_dict[s].get(mode, 0) for s in strata_names]
        bars = ax.bar(x + offset, vals, width, label=labels[mode],
                      color=COLORS[mode], edgecolor='white', linewidth=0.5)

    ax.set_ylabel(ylabel, fontsize=12)
    ax.set_title(title, fontsize=14, fontweight='bold')
    ax.set_xticks(x)
    ax.set_xticklabels(strata_names, fontsize=10)
    ax.legend(loc='upper right', fontsize=9)
    ax.set_ylim(0, max(max(d.values()) for d in data_dict.values()) * 1.15)
    ax.grid(axis='y', alpha=0.3)
    ax.spines['top'].set_visible(False)
    ax.spines['right'].set_visible(False)

    plt.tight_layout()
    plt.savefig(os.path.join(OUT, filename), dpi=150, bbox_inches='tight')
    plt.close()
    print(f'  {filename}')


def make_radar_chart(cat_data, title, filename, lang='en'):
    """Radar chart with categories as axes."""
    labels_map = MODE_LABELS if lang == 'en' else MODE_LABELS_RU
    categories = list(cat_data.keys())
    N = len(categories)

    angles = [n / float(N) * 2 * np.pi for n in range(N)]
    angles += angles[:1]

    fig, ax = plt.subplots(figsize=(10, 10), subplot_kw=dict(polar=True))

    for mode in ['C21', 'BL_LS', 'BL_LF', 'BL_LE', 'A0']:
        values = [cat_data[cat].get(mode, 0) for cat in categories]
        values += values[:1]
        ax.plot(angles, values, 'o-', linewidth=2, label=labels_map[mode],
                color=COLORS[mode], markersize=5)
        ax.fill(angles, values, alpha=0.05, color=COLORS[mode])

    ax.set_xticks(angles[:-1])
    ax.set_xticklabels(categories, fontsize=11)
    ax.set_title(title, fontsize=14, fontweight='bold', pad=20)
    ax.legend(loc='upper right', bbox_to_anchor=(1.3, 1.1), fontsize=10)

    plt.tight_layout()
    plt.savefig(os.path.join(OUT, filename), dpi=150, bbox_inches='tight')
    plt.close()
    print(f'  {filename}')


def make_stratification_chart(strata_data, title, filename, lang='en'):
    """Grouped bar chart showing C21 vs baselines across A0 strata."""
    labels_map = MODE_LABELS if lang == 'en' else MODE_LABELS_RU
    modes_show = ['C21', 'BL_LS', 'BL_LF', 'BL_LE', 'A0']

    strata = list(strata_data.keys())
    fig, ax = plt.subplots(figsize=(14, 6))

    x = np.arange(len(strata))
    width = 0.15

    for i, mode in enumerate(modes_show):
        offset = (i - len(modes_show)/2 + 0.5) * width
        vals = [strata_data[s].get(mode, 0) for s in strata]
        ax.bar(x + offset, vals, width, label=labels_map[mode],
               color=COLORS[mode], edgecolor='white', linewidth=0.5)

    ax.set_ylabel('pass@1 (%)', fontsize=12)
    ax.set_title(title, fontsize=14, fontweight='bold')
    ax.set_xticks(x)
    ax.set_xticklabels(strata, fontsize=9, rotation=15)
    ax.legend(fontsize=10)
    ax.grid(axis='y', alpha=0.3)
    ax.spines['top'].set_visible(False)
    ax.spines['right'].set_visible(False)

    plt.tight_layout()
    plt.savefig(os.path.join(OUT, filename), dpi=150, bbox_inches='tight')
    plt.close()
    print(f'  {filename}')


# ═══ Main ═══

def main():
    data = load_combined()
    summary = load_summary()

    # Compute per-mode per-stratum rates from combined data
    def compute_rates(tasks, mode_col):
        vals = [float(row[mode_col]) for row in tasks if float(row[f'{mode_col.split("_rate")[0]}_total']) > 0]
        return round(100 * sum(vals) / len(vals), 1) if vals else 0

    # ═══ Figure 1: Bar chart — All / Hard / Sweet ═══
    print('Generating figures...')

    strata_bar = {}
    for sname, sfilt in [
        ('All (488)', lambda r: True),
        ('Hard\nA0≤25%\n(232)', lambda r: float(r['a0_rate']) <= 0.25),
        ('Sweet\n[1/8,4/8]\n(59)', lambda r: 0 < float(r['a0_rate']) <= 0.5),
        ('Easy\n>50%\n(241)', lambda r: float(r['a0_rate']) > 0.5),
    ]:
        filtered = [r for r in data if sfilt(r)]
        strata_bar[sname] = {}
        for m in MODES_ORDER:
            rates = [float(r[f'{m}_rate']) for r in filtered if float(r[f'{m}_total']) > 0]
            strata_bar[sname][m] = round(100 * sum(rates) / len(rates), 1) if rates else 0

    make_bar_chart(strata_bar,
                   'pass@1 by Mode and Difficulty Stratum (MiniF2F Test+Valid, 488 tasks)',
                   'fig_bar_strata_en.png', lang='en')
    make_bar_chart(strata_bar,
                   'pass@1 по режимам и стратам сложности (MiniF2F Test+Valid, 488 задач)',
                   'fig_bar_strata_ru.png', lang='ru')

    # ═══ Figure 2: Radar chart by category ═══
    cat_data = {}
    for cat in ['IMO', 'AIME', 'AMC', 'MATHD_Algebra', 'MATHD_NumberTheory', 'Other']:
        filtered = [r for r in data if r['category'] == cat or
                    (cat == 'Other' and r['category'] not in ['IMO','AIME','AMC','MATHD_Algebra','MATHD_NumberTheory','MATHD_Other'])]
        if not filtered and cat.startswith('MATHD'):
            filtered = [r for r in data if r['category'] == cat]
        cat_data[cat] = {}
        for m in MODES_ORDER:
            rates = [float(r[f'{m}_rate']) for r in filtered if float(r[f'{m}_total']) > 0]
            cat_data[cat][m] = round(100 * sum(rates) / len(rates), 1) if rates else 0

    make_radar_chart(cat_data,
                     'pass@1 by Task Category — C21 vs Baselines',
                     'fig_radar_categories_en.png', lang='en')
    make_radar_chart(cat_data,
                     'pass@1 по категориям задач — C21 vs Бейзлайны',
                     'fig_radar_categories_ru.png', lang='ru')

    # ═══ Figure 3: Stratification chart ═══
    strat_data = {}
    for sname, sfilt in [
        ('A0=0/8\n(190)', lambda r: float(r['a0_rate']) == 0),
        ('A0=1/8\n(16)', lambda r: abs(float(r['a0_rate']) - 0.125) < 0.01),
        ('A0=2/8\n(22)', lambda r: abs(float(r['a0_rate']) - 0.25) < 0.01),
        ('A0=3/8\n(6)', lambda r: abs(float(r['a0_rate']) - 0.375) < 0.01),
        ('A0=4/8\n(13)', lambda r: abs(float(r['a0_rate']) - 0.5) < 0.01),
        ('A0=5/8\n(12)', lambda r: abs(float(r['a0_rate']) - 0.625) < 0.01),
        ('A0=6/8\n(26)', lambda r: abs(float(r['a0_rate']) - 0.75) < 0.01),
        ('A0=7/8\n(32)', lambda r: abs(float(r['a0_rate']) - 0.875) < 0.01),
        ('A0=8/8\n(161)', lambda r: float(r['a0_rate']) == 1.0),
    ]:
        filtered = [r for r in data if sfilt(r)]
        strat_data[sname] = {}
        for m in ['C21', 'BL_LS', 'BL_LF', 'BL_LE', 'A0']:
            rates = [float(r[f'{m}_rate']) for r in filtered if float(r[f'{m}_total']) > 0]
            strat_data[sname][m] = round(100 * sum(rates) / len(rates), 1) if rates else 0

    make_stratification_chart(strat_data,
                               'pass@1 by A0 Baseline Score — C21 vs External Baselines',
                               'fig_strat_a0_en.png', lang='en')
    make_stratification_chart(strat_data,
                               'pass@1 по скору A0 — C21 vs Внешние бейзлайны',
                               'fig_strat_a0_ru.png', lang='ru')

    # ═══ Figure 4: Sweet spot zoom ═══
    sweet_modes = ['C21', 'B1', 'C23', 'BL_LS', 'BL_LF', 'BL_LE', 'A0']
    sweet_tasks = [r for r in data if 0 < float(r['a0_rate']) <= 0.5]

    fig, ax = plt.subplots(figsize=(10, 6))
    vals = []
    colors = []
    labels = []
    for m in sweet_modes:
        rates = [float(r[f'{m}_rate']) for r in sweet_tasks if float(r[f'{m}_total']) > 0]
        v = round(100 * sum(rates) / len(rates), 1) if rates else 0
        vals.append(v)
        colors.append(COLORS[m])
        labels.append(MODE_LABELS[m])

    bars = ax.bar(range(len(vals)), vals, color=colors, edgecolor='white', linewidth=0.5)
    ax.set_xticks(range(len(vals)))
    ax.set_xticklabels(labels, fontsize=10, rotation=15)
    ax.set_ylabel('pass@1 (%)', fontsize=12)
    ax.set_title(f'Partial-Capability Zone: A0 ∈ [1/8, 4/8] — {len(sweet_tasks)} tasks', fontsize=14, fontweight='bold')

    for bar, val in zip(bars, vals):
        ax.text(bar.get_x() + bar.get_width()/2., bar.get_height() + 0.5,
                f'{val}%', ha='center', va='bottom', fontsize=11, fontweight='bold')

    ax.grid(axis='y', alpha=0.3)
    ax.spines['top'].set_visible(False)
    ax.spines['right'].set_visible(False)
    plt.tight_layout()
    plt.savefig(os.path.join(OUT, 'fig_partial_capability_en.png'), dpi=150, bbox_inches='tight')
    plt.close()
    print('  fig_partial_capability_en.png')

    # Russian version
    fig, ax = plt.subplots(figsize=(10, 6))
    labels_ru = [MODE_LABELS_RU[m] for m in sweet_modes]
    bars = ax.bar(range(len(vals)), vals, color=colors, edgecolor='white', linewidth=0.5)
    ax.set_xticks(range(len(vals)))
    ax.set_xticklabels(labels_ru, fontsize=10, rotation=15)
    ax.set_ylabel('pass@1 (%)', fontsize=12)
    ax.set_title(f'Partial-Capability Zone: A0 ∈ [1/8, 4/8] — {len(sweet_tasks)} задач', fontsize=14, fontweight='bold')
    for bar, val in zip(bars, vals):
        ax.text(bar.get_x() + bar.get_width()/2., bar.get_height() + 0.5,
                f'{val}%', ha='center', va='bottom', fontsize=11, fontweight='bold')
    ax.grid(axis='y', alpha=0.3)
    ax.spines['top'].set_visible(False)
    ax.spines['right'].set_visible(False)
    plt.tight_layout()
    plt.savefig(os.path.join(OUT, 'fig_partial_capability_ru.png'), dpi=150, bbox_inches='tight')
    plt.close()
    print('  fig_partial_capability_ru.png')

    print('Done. All figures saved.')


if __name__ == '__main__':
    main()
