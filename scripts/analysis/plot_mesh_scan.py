#!/usr/bin/env python3
"""Plot mesh scan: cumulative distance vs toolhead Z with trigger markers.

Reads trigger points and segment waypoints from a scan output file.
Expected formats:
  // pt N: x=... y=... z=... s=0|1 rz=...
  // seg N: x=... y=... z=...

Usage:
    uv run plot_mesh_scan.py <scan_output_file> [output_png]
"""
import bisect
import math
import re
import sys

import matplotlib.pyplot as plt

OUTLIER_THRESHOLD = 0.05  # mm


def parse_file(filename):
    points = []
    segments = []
    misses = []
    with open(filename) as f:
        for line in f:
            # Support both raw format and gcode_store format (// prefix)
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
                r"(?:// )?seg \d+: x=(-?[\d.]+) y=(-?[\d.]+) z=(-?[\d.]+)",
                line)
            if m:
                segments.append({
                    'x': float(m.group(1)),
                    'y': float(m.group(2)),
                    'z': float(m.group(3)),
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


def cum_distance(items, x_key='x', y_key='y'):
    dist = [0.0]
    for i in range(1, len(items)):
        dx = items[i][x_key] - items[i - 1][x_key]
        dy = items[i][y_key] - items[i - 1][y_key]
        dist.append(dist[-1] + math.hypot(dx, dy))
    return dist


def interp_z_at_dist(d, seg_dist, seg_z):
    """Linearly interpolate segment Z at cumulative distance d."""
    k = bisect.bisect_right(seg_dist, d) - 1
    k = max(0, min(k, len(seg_dist) - 2))
    span = seg_dist[k + 1] - seg_dist[k]
    if span == 0:
        return seg_z[k]
    t = (d - seg_dist[k]) / span
    return seg_z[k] + t * (seg_z[k + 1] - seg_z[k])


def find_outliers(points, pt_dists, seg_dist, seg_z):
    """Return list of (index, point, diff, interp_z) for outlier points."""
    outliers = []
    for i, p in enumerate(points):
        iz = interp_z_at_dist(pt_dists[i], seg_dist, seg_z)
        diff = abs(p['raw_z'] - iz)
        if diff > OUTLIER_THRESHOLD:
            outliers.append((i, p, diff, iz))
    return outliers


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    infile = sys.argv[1]
    outfile = sys.argv[2] if len(sys.argv) > 2 else "mesh_scan_plot.png"

    points, segments, misses = parse_file(infile)
    if not points:
        print(f"No points found in {infile}")
        sys.exit(1)

    fig, ax = plt.subplots(figsize=(16, 5))

    # Plot segment waypoints as the actual toolhead path
    if segments:
        seg_dist = cum_distance(segments)
        ax.plot(seg_dist, [s['z'] for s in segments], '-', color='#888',
                linewidth=0.5, zorder=1, label='Toolhead path')

        # Row boundaries from segments
        prev_y = segments[0]['y']
        for i in range(1, len(segments)):
            if segments[i]['y'] != prev_y:
                ax.axvline(seg_dist[i], color='lightgray', linestyle='--',
                           linewidth=0.8, zorder=0)
                ax.text(seg_dist[i] + 5, ax.get_ylim()[0] or 0.85,
                        f"Y={segments[i]['y']:.0f}", fontsize=7, color='gray',
                        va='bottom')
                prev_y = segments[i]['y']

    # Compute cumulative distance for trigger points along the same path
    # by matching each point's XY to the nearest segment
    if segments:
        # For each trigger point, find where it falls on the segment path
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
    else:
        pt_dists = cum_distance(points)

    # Outlier analysis: compare trigger Z to interpolated segment path Z
    if segments and len(seg_dist) >= 2:
        outliers = find_outliers(points, pt_dists, seg_dist,
                                 [s['z'] for s in segments])
        if outliers:
            print(f"\nOutliers (|trigger_z - path_z| > {OUTLIER_THRESHOLD} mm):")
            for idx, p, diff, iz in outliers:
                print(f"  pt {idx}: xy=({p['x']:.1f},{p['y']:.1f}) "
                      f"trigger_z={p['raw_z']:.4f} path_z={iz:.4f} "
                      f"diff={diff:.4f} state={p['state']}")
        else:
            print("\nNo outliers detected.")

    # Separate by state
    on_d = [pt_dists[i] for i, p in enumerate(points) if p['state'] == 1]
    on_z = [p['raw_z'] for p in points if p['state'] == 1]
    off_d = [pt_dists[i] for i, p in enumerate(points) if p['state'] == 0]
    off_z = [p['raw_z'] for p in points if p['state'] == 0]

    ax.scatter(on_d, on_z, marker='v', s=20, color='#d62728', zorder=3,
               label='Trigger ON (state=1)')
    ax.scatter(off_d, off_z, marker='^', s=20, color='#1f77b4', zorder=3,
               label='Trigger OFF (state=0)')

    # Plot missed events
    if misses and segments:
        miss_dists = []
        for m in misses:
            best_d = float('inf')
            best_seg_dist = 0.0
            for j, s in enumerate(segments):
                d = math.hypot(m['x'] - s['x'], m['y'] - s['y'])
                if d < best_d:
                    best_d = d
                    best_seg_dist = seg_dist[j]
            miss_dists.append(best_seg_dist)
        miss_z = [m['z'] for m in misses]
        ax.scatter(miss_dists, miss_z, marker='X', s=60, color='#ff7f0e',
                   edgecolors='black', linewidths=0.5, zorder=4,
                   label='Missed event')

    ax.set_xlabel('Cumulative distance (mm)')
    ax.set_ylabel('Toolhead Z (mm)')
    ax.set_title('Probe Scan Mesh — Zig-Zag with Trigger Points')
    ax.legend(loc='upper right')
    ax.grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(outfile, dpi=150)
    print(f"Saved {outfile}")


if __name__ == "__main__":
    main()
