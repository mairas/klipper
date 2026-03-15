#!/usr/bin/env python3
"""Detect missed triggers from scan data ground truth.

Identifies zigzag half-cycles from the segment Z path, then checks
whether each half-cycle contains the expected trigger event.
Plots results overlaid on the scan data.

Usage:
    uv run ground_truth_misses.py [scan_data_file]
"""
import math
import re
import sys

import matplotlib.pyplot as plt


def parse_scan_data(filename):
    points = []
    segments = []
    with open(filename) as f:
        for line in f:
            m = re.match(
                r"(?:// )?pt \d+: x=(-?[\d.]+) y=(-?[\d.]+) z=(-?[\d.]+)"
                r" s=(\d) rz=(-?[\d.]+)", line)
            if m:
                points.append({
                    'x': float(m.group(1)),
                    'y': float(m.group(2)),
                    'z': float(m.group(3)),
                    'state': int(m.group(4)),
                    'rz': float(m.group(5)),
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
    return points, segments


def find_reversals(segments):
    """Find peaks and valleys in the Z path."""
    reversals = []
    for i in range(1, len(segments) - 1):
        z_prev = segments[i - 1]['z']
        z_curr = segments[i]['z']
        z_next = segments[i + 1]['z']
        if z_curr > z_prev and z_curr >= z_next:
            reversals.append((i, True))   # peak
        elif z_curr < z_prev and z_curr <= z_next:
            reversals.append((i, False))  # valley
    return reversals


def find_half_cycles(reversals):
    """Build half-cycles between consecutive reversals.

    Each half-cycle goes from one reversal to the next.
    Ascending (valley->peak) expects a trigger OFF (state=0).
    Descending (peak->valley) expects a trigger ON (state=1).
    """
    half_cycles = []
    for i in range(len(reversals) - 1):
        start_idx, start_is_peak = reversals[i]
        end_idx, end_is_peak = reversals[i + 1]
        ascending = not start_is_peak  # valley to peak
        # Ascending half-cycle expects untrigger (state=0)
        # Descending half-cycle expects trigger (state=1)
        expected_state = 0 if ascending else 1
        half_cycles.append({
            'start_seg': start_idx,
            'end_seg': end_idx,
            'ascending': ascending,
            'expected_state': expected_state,
        })
    return half_cycles


def match_points_to_segments(points, segments):
    """Find the nearest segment index for each trigger point."""
    pt_seg_indices = []
    for p in points:
        best_d = float('inf')
        best_idx = 0
        for j, s in enumerate(segments):
            d = math.hypot(p['x'] - s['x'], p['y'] - s['y'])
            if d < best_d:
                best_d = d
                best_idx = j
        pt_seg_indices.append(best_idx)
    return pt_seg_indices


def cum_distance(segments):
    dist = [0.0]
    for i in range(1, len(segments)):
        dx = segments[i]['x'] - segments[i - 1]['x']
        dy = segments[i]['y'] - segments[i - 1]['y']
        dist.append(dist[-1] + math.hypot(dx, dy))
    return dist


def main():
    scanfile = sys.argv[1] if len(sys.argv) > 1 else 'output/scan_data.txt'

    points, segments = parse_scan_data(scanfile)
    reversals = find_reversals(segments)
    half_cycles = find_half_cycles(reversals)
    pt_seg_indices = match_points_to_segments(points, segments)

    # Find row boundaries (where Y changes)
    row_boundaries = []
    prev_y = segments[0]['y']
    for i in range(1, len(segments)):
        if segments[i]['y'] != prev_y:
            row_boundaries.append(i)
            prev_y = segments[i]['y']

    # A half-cycle is a row-boundary artifact if it crosses a row
    # boundary or is a short stub (< 6 segs) with an endpoint
    # within 2 segments of a boundary.
    def is_row_boundary(hc):
        span = hc['end_seg'] - hc['start_seg']
        for rb in row_boundaries:
            # Crosses boundary
            if hc['start_seg'] < rb < hc['end_seg']:
                return True
            # Short stub touching boundary
            if span < 6:
                if (abs(hc['start_seg'] - rb) <= 2
                        or abs(hc['end_seg'] - rb) <= 2):
                    return True
        return False

    # For each half-cycle, check if it contains the expected event
    hits = []
    genuine_misses = []
    boundary_misses = []
    for hc in half_cycles:
        s0, s1 = hc['start_seg'], hc['end_seg']
        expected = hc['expected_state']
        has_event = any(
            s0 <= pt_seg_indices[i] <= s1
            and points[i]['state'] == expected
            for i in range(len(points)))
        if has_event:
            hits.append(hc)
        elif is_row_boundary(hc):
            boundary_misses.append(hc)
        else:
            genuine_misses.append(hc)

    all_misses = genuine_misses + boundary_misses
    print(f"{len(segments)} segments, {len(points)} trigger points, "
          f"{len(reversals)} reversals, {len(half_cycles)} half-cycles")
    print(f"Hits: {len(hits)}, Genuine misses: {len(genuine_misses)}, "
          f"Row-boundary misses: {len(boundary_misses)}")
    print()
    for hc in genuine_misses:
        direction = "ascending" if hc['ascending'] else "descending"
        expected = "OFF(0)" if hc['expected_state'] == 0 else "ON(1)"
        print(f"  GENUINE MISS: segs [{hc['start_seg']},{hc['end_seg']}] "
              f"{direction} expected={expected}")
    for hc in boundary_misses:
        direction = "ascending" if hc['ascending'] else "descending"
        expected = "OFF(0)" if hc['expected_state'] == 0 else "ON(1)"
        print(f"  ROW BOUNDARY: segs [{hc['start_seg']},{hc['end_seg']}] "
              f"{direction} expected={expected}")

    # Plot
    seg_dist = cum_distance(segments)

    fig, ax = plt.subplots(figsize=(16, 5))

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
    on_d = [seg_dist[pt_seg_indices[i]]
            for i in range(len(points)) if points[i]['state'] == 1]
    on_z = [p['rz'] for p in points if p['state'] == 1]
    off_d = [seg_dist[pt_seg_indices[i]]
             for i in range(len(points)) if points[i]['state'] == 0]
    off_z = [p['rz'] for p in points if p['state'] == 0]

    ax.scatter(on_d, on_z, marker='v', s=20, color='#d62728', zorder=3,
               label='Trigger ON')
    ax.scatter(off_d, off_z, marker='^', s=20, color='#1f77b4', zorder=3,
               label='Trigger OFF')

    # Genuine misses
    if genuine_misses:
        gm_d = [seg_dist[hc['end_seg']] for hc in genuine_misses]
        gm_z = [segments[hc['end_seg']]['z'] for hc in genuine_misses]
        ax.scatter(gm_d, gm_z, marker='X', s=80, color='#ff7f0e',
                   edgecolors='black', linewidths=0.5, zorder=5,
                   label=f'Genuine miss ({len(genuine_misses)})')

    # Row-boundary misses (dimmed)
    if boundary_misses:
        bm_d = [seg_dist[hc['end_seg']] for hc in boundary_misses]
        bm_z = [segments[hc['end_seg']]['z'] for hc in boundary_misses]
        ax.scatter(bm_d, bm_z, marker='x', s=40, color='#999',
                   zorder=4,
                   label=f'Row-boundary miss ({len(boundary_misses)})')

    ax.set_xlabel('Cumulative distance (mm)')
    ax.set_ylabel('Toolhead Z (mm)')
    ax.set_title('Ground Truth: Half-cycles with missing expected trigger')
    ax.legend(loc='upper right', fontsize=8)
    ax.grid(alpha=0.3)
    fig.tight_layout()
    outfile = 'output/ground_truth_misses.png'
    fig.savefig(outfile, dpi=150)
    print(f"\nSaved {outfile}")


if __name__ == '__main__':
    main()
