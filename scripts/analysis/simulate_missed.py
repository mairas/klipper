#!/usr/bin/env python3
"""Simulate missed-event detection against debug log data.

Replays the sequence of event additions and reversal evaluations
from the debug log, testing different slack and eval_delay values.
Reports which misses are genuine vs false positives, and optionally
plots results overlaid on scan data.

The segment slack (range extension) and eval deferral (time delay)
are independent parameters:
  - slack: extra segments past rev_seg to check for events (probe lag)
  - eval_delay: seconds to wait after reversal before evaluating (poll lag)

Usage:
    uv run simulate_missed.py [slack] [eval_delay]
"""
import bisect
import math
import re
import sys

import matplotlib.pyplot as plt

SEGS_PER_ROW = 173


def parse_debug_log(filename):
    """Parse debug log into per-row sequences of events."""
    rows = []
    current_row = None

    with open(filename) as f:
        for line in f:
            line = line.strip()

            m = re.match(r'probe_scan: zig-zag scan', line)
            if m:
                if current_row is not None:
                    rows.append(current_row)
                current_row = {'events': [], 'evals': []}
                continue

            if current_row is None:
                continue

            m = re.match(
                r'probe_scan: add_seg=(\d+) state=(\d+) pt=([\d.]+)',
                line)
            if m:
                current_row['events'].append({
                    'seg': int(m.group(1)),
                    'state': int(m.group(2)),
                    'pt': float(m.group(3)),
                })
                continue

            m = re.match(
                r'probe_scan: eval rev_seg=(\d+) prev_seg=(\d+) '
                r'has_event=(\w+) rev_pt=([\d.]+) deadline=([\d.]+) '
                r'event_segs=\[(.*?)\]', line)
            if m:
                event_segs_str = m.group(6).strip()
                if event_segs_str:
                    event_segs = [int(x.strip())
                                  for x in event_segs_str.split(',')]
                else:
                    event_segs = []
                current_row['evals'].append({
                    'rev_seg': int(m.group(1)),
                    'prev_seg': int(m.group(2)),
                    'has_event': m.group(3) == 'True',
                    'rev_pt': float(m.group(4)),
                    'deadline': float(m.group(5)),
                    'event_segs': event_segs,
                })
                continue

            m = re.match(r'probe_scan: scan complete', line)
            if m:
                if current_row is not None:
                    rows.append(current_row)
                    current_row = None

    if current_row is not None:
        rows.append(current_row)

    return rows


def parse_scan_data(filename):
    """Parse scan_data.txt for segments, points, and original misses."""
    points = []
    segments = []
    misses = []
    with open(filename) as f:
        for line in f:
            m = re.match(
                r"(?:// )?pt \d+: x=(-?[\d.]+) y=(-?[\d.]+) z=(-?[\d.]+)"
                r" s=(\d) rz=(-?[\d.]+)", line)
            if m:
                points.append({
                    'x': float(m.group(1)),
                    'y': float(m.group(2)),
                    'bed_z': float(m.group(3)),
                    'state': int(m.group(4)),
                    'raw_z': float(m.group(5)),
                })
                continue
            m = re.match(
                r"(?:// )?seg (\d+): x=(-?[\d.]+) y=(-?[\d.]+) z=(-?[\d.]+)",
                line)
            if m:
                segments.append({
                    'idx': int(m.group(1)),
                    'x': float(m.group(2)),
                    'y': float(m.group(3)),
                    'z': float(m.group(4)),
                })
                continue
            m = re.match(
                r"(?:// )?miss \d+: x=(-?[\d.]+) y=(-?[\d.]+) z=(-?[\d.]+)"
                r" asc=(\d)", line)
            if m:
                misses.append({
                    'x': float(m.group(1)),
                    'y': float(m.group(2)),
                    'z': float(m.group(3)),
                    'ascending': int(m.group(4)),
                })
    return points, segments, misses


def simulate_row(row, slack, effective_wait):
    """Simulate missed-event detection for one row."""
    all_event_segs = {e['seg'] for e in row['events']}
    event_pt_by_seg = {e['seg']: e['pt'] for e in row['events']}

    results = []
    for ev in row['evals']:
        if ev['has_event']:
            continue

        rev_seg = ev['rev_seg']
        prev_seg = ev['prev_seg']

        has_event_with_slack = any(
            prev_seg <= es <= rev_seg + slack
            for es in ev['event_segs'])

        has_event_anywhere = any(
            prev_seg <= es <= rev_seg + slack
            for es in all_event_segs)

        matching_segs = sorted(
            es for es in all_event_segs
            if prev_seg <= es <= rev_seg + slack)

        match_seg = matching_segs[0] if matching_segs else None
        match_pt = event_pt_by_seg.get(match_seg) if match_seg else None
        timing_gap = (match_pt - ev['rev_pt']) if match_pt else None

        defer_fixes = False
        if (not has_event_with_slack and has_event_anywhere
                and match_pt is not None):
            defer_fixes = match_pt <= ev['rev_pt'] + effective_wait

        # Classify
        if has_event_with_slack:
            classification = 'fixed_slack'
        elif defer_fixes:
            classification = 'fixed_defer'
        elif not has_event_anywhere:
            classification = 'genuine'
        else:
            classification = 'false_positive'

        results.append({
            'rev_seg': rev_seg,
            'prev_seg': prev_seg,
            'classification': classification,
            'event_segs_at_eval': ev['event_segs'],
            'matching_segs': matching_segs,
            'rev_pt': ev['rev_pt'],
            'deadline': ev['deadline'],
            'match_pt': match_pt,
            'timing_gap': timing_gap,
        })

    return results


def cum_distance(items, x_key='x', y_key='y'):
    dist = [0.0]
    for i in range(1, len(items)):
        dx = items[i][x_key] - items[i - 1][x_key]
        dy = items[i][y_key] - items[i - 1][y_key]
        dist.append(dist[-1] + math.hypot(dx, dy))
    return dist


def plot_results(segments, points, orig_misses, sim_results, params):
    """Plot scan data with simulation-classified missed events."""
    fig, ax = plt.subplots(figsize=(16, 5))

    seg_dist = cum_distance(segments)

    # Toolhead path
    ax.plot(seg_dist, [s['z'] for s in segments], '-', color='#888',
            linewidth=0.5, zorder=1, label='Toolhead path')

    # Row boundaries
    prev_y = segments[0]['y']
    for i in range(1, len(segments)):
        if segments[i]['y'] != prev_y:
            ax.axvline(seg_dist[i], color='lightgray', linestyle='--',
                       linewidth=0.8, zorder=0)
            prev_y = segments[i]['y']

    # Trigger points
    pt_dists = []
    for p in points:
        best_d = float('inf')
        best_seg_dist = 0.0
        for j, s in enumerate(segments):
            d = math.hypot(p['x'] - s['x'], p['y'] - s['y'])
            if d < best_d:
                best_d = d
                best_seg_dist = seg_dist[j]
        pt_dists.append(best_seg_dist)

    on_d = [pt_dists[i] for i, p in enumerate(points) if p['state'] == 1]
    on_z = [p['raw_z'] for p in points if p['state'] == 1]
    off_d = [pt_dists[i] for i, p in enumerate(points) if p['state'] == 0]
    off_z = [p['raw_z'] for p in points if p['state'] == 0]

    ax.scatter(on_d, on_z, marker='v', s=20, color='#d62728', zorder=3,
               label='Trigger ON')
    ax.scatter(off_d, off_z, marker='^', s=20, color='#1f77b4', zorder=3,
               label='Trigger OFF')

    # Simulation results: map per-row segment indices to global segments
    # and look up positions from segment data
    colors = {
        'genuine': '#ff7f0e',
        'false_positive': '#e377c2',
        'fixed_slack': '#2ca02c',
        'fixed_defer': '#17becf',
    }
    labels = {
        'genuine': 'Genuine miss (simulated)',
        'false_positive': 'False positive',
        'fixed_slack': 'Fixed by slack',
        'fixed_defer': 'Fixed by defer',
    }
    markers_plotted = set()

    for row_idx, row_results in sim_results:
        for r in row_results:
            global_seg = row_idx * SEGS_PER_ROW + r['rev_seg']
            if global_seg >= len(segments):
                continue
            d = seg_dist[global_seg]
            z = segments[global_seg]['z']
            cls = r['classification']
            label = labels[cls] if cls not in markers_plotted else None
            markers_plotted.add(cls)
            ax.scatter(d, z, marker='X', s=80,
                       color=colors[cls],
                       edgecolors='black', linewidths=0.5,
                       zorder=5, label=label)

    ax.set_xlabel('Cumulative distance (mm)')
    ax.set_ylabel('Toolhead Z (mm)')
    ax.set_title(
        f'Simulation: slack={params["slack"]}, '
        f'eval_delay={params["eval_delay"]:.3f}s, '
        f'effective_wait={params["effective_wait"]:.3f}s')
    ax.legend(loc='upper right', fontsize=8)
    ax.grid(alpha=0.3)
    fig.tight_layout()
    outfile = 'output/simulate_missed_plot.png'
    fig.savefig(outfile, dpi=150)
    print(f"Saved {outfile}")


def main():
    slack = int(sys.argv[1]) if len(sys.argv) > 1 else 4
    eval_delay = float(sys.argv[2]) if len(sys.argv) > 2 else None
    logfile = 'output/debug_log.txt'
    scanfile = 'output/scan_data.txt'

    segment_time = 0.04
    adapt_interval = 0.05

    if eval_delay is None:
        eval_delay = slack * segment_time
    effective_wait = eval_delay + 2 * adapt_interval

    rows = parse_debug_log(logfile)
    print(f"Parsed {len(rows)} rows")
    print(f"slack={slack} segs, eval_delay={eval_delay:.3f}s, "
          f"effective_wait={effective_wait:.3f}s\n")

    total_misses = 0
    counts = {'fixed_slack': 0, 'fixed_defer': 0,
              'genuine': 0, 'false_positive': 0}
    all_sim_results = []

    for i, row in enumerate(rows):
        results = simulate_row(row, slack, effective_wait)
        all_sim_results.append((i, results))
        if not results:
            continue

        print(f"Row {i+1}: {len(row['events'])} events, "
              f"{len(results)} misses flagged")

        for r in results:
            total_misses += 1
            cls = r['classification']
            counts[cls] += 1

            gap_str = (f"{r['timing_gap']:+.3f}s"
                       if r['timing_gap'] is not None else "  N/A  ")
            extra = ""
            if cls == 'false_positive' and r['timing_gap'] is not None:
                extra = (f" [need {r['timing_gap']:.3f}s, "
                         f"have {effective_wait:.3f}s]")
            print(f"  rev_seg={r['rev_seg']:3d} "
                  f"[{r['prev_seg']},{r['rev_seg']+slack}] "
                  f"segs_at_eval={r['event_segs_at_eval']!s:20s} "
                  f"match={r['matching_segs']!s:10s} "
                  f"gap={gap_str:>9s} "
                  f"→ {cls.upper().replace('_', ' ')}{extra}")
        print()

    print(f"Summary (slack={slack}, eval_delay={eval_delay:.3f}s, "
          f"effective_wait={effective_wait:.3f}s):")
    print(f"  Total misses flagged: {total_misses}")
    print(f"  Fixed by slack:       {counts['fixed_slack']}")
    print(f"  Fixed by defer:       {counts['fixed_defer']}")
    print(f"  Genuine misses:       {counts['genuine']}")
    print(f"  False positives:      {counts['false_positive']}")
    assert sum(counts.values()) == total_misses

    # Plot
    points, segments, orig_misses = parse_scan_data(scanfile)
    if segments:
        plot_results(segments, points, orig_misses, all_sim_results,
                     {'slack': slack, 'eval_delay': eval_delay,
                      'effective_wait': effective_wait})


if __name__ == '__main__':
    main()
