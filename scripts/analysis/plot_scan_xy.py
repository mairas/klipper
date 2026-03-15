#!/usr/bin/env python3
"""Plot scan events in XY space with mesh grid overlay.

Shows where probe triggers occurred relative to the mesh grid points,
highlighting coverage gaps.

Usage:
    uv run plot_scan_xy.py <scan_data_file> [output_png]
"""
import math
import re
import sys

import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import numpy as np


def parse_file(filename):
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
    return points, segments


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    infile = sys.argv[1]
    outfile = sys.argv[2] if len(sys.argv) > 2 else "output/scan_xy_plot.png"

    points, segments = parse_file(infile)
    if not points:
        print(f"No points found in {infile}")
        sys.exit(1)

    # Infer 11x11 mesh grid from segment extents
    min_x = min(s['x'] for s in segments)
    max_x = max(s['x'] for s in segments)
    min_y = min(s['y'] for s in segments)
    max_y = max(s['y'] for s in segments)

    grid_count = 11  # matches bed_mesh probe_count
    x_grid = np.linspace(min_x, max_x, grid_count)
    y_grid = np.linspace(min_y, max_y, grid_count)

    search_radius = 25.0  # match code default

    fig, ax = plt.subplots(figsize=(12, 10))

    # Plot segment path (faint)
    if segments:
        sx = [s['x'] for s in segments]
        sy = [s['y'] for s in segments]
        ax.plot(sx, sy, '-', color='#ccc', linewidth=0.3, zorder=0)

    # Plot trigger events
    on_x = [p['x'] for p in points if p['state'] == 1]
    on_y = [p['y'] for p in points if p['state'] == 1]
    off_x = [p['x'] for p in points if p['state'] == 0]
    off_y = [p['y'] for p in points if p['state'] == 0]

    ax.scatter(on_x, on_y, marker='v', s=15, color='#d62728', zorder=3,
               alpha=0.7, label='Trigger ON')
    ax.scatter(off_x, off_y, marker='^', s=15, color='#1f77b4', zorder=3,
               alpha=0.7, label='Trigger OFF')

    # Plot grid points and check coverage
    uncovered = []
    covered = []
    for gx in x_grid:
        for gy in y_grid:
            has_data = False
            for p in points:
                dx = gx - p['x']
                dy = gy - p['y']
                if dx * dx + dy * dy <= search_radius ** 2:
                    has_data = True
                    break
            if has_data:
                covered.append((gx, gy))
            else:
                uncovered.append((gx, gy))

    if covered:
        cx, cy = zip(*covered)
        ax.scatter(cx, cy, marker='s', s=40, facecolors='none',
                   edgecolors='green', linewidths=1, zorder=2,
                   label=f'Grid point (covered, r={search_radius})')
    if uncovered:
        ux, uy = zip(*uncovered)
        ax.scatter(ux, uy, marker='s', s=60, facecolors='none',
                   edgecolors='red', linewidths=2, zorder=4,
                   label=f'Grid point (NO DATA, r={search_radius})')
        # Draw search radius circles around uncovered points
        for x, y in uncovered:
            circle = plt.Circle((x, y), search_radius, fill=False,
                                color='red', linewidth=0.5, linestyle='--',
                                alpha=0.5)
            ax.add_patch(circle)

    ax.set_xlabel('X (mm)')
    ax.set_ylabel('Y (mm)')
    ax.set_title(f'Probe Scan XY Coverage — {len(points)} events, '
                 f'{len(uncovered)} uncovered grid points')
    ax.legend(loc='upper left', fontsize=8)
    ax.set_aspect('equal')
    ax.grid(alpha=0.2)
    fig.tight_layout()
    fig.savefig(outfile, dpi=150)
    print(f"Saved {outfile}")
    print(f"Covered: {len(covered)}, Uncovered: {len(uncovered)}")
    if uncovered:
        print("Uncovered grid points:")
        for x, y in uncovered:
            print(f"  ({x:.1f}, {y:.1f})")


if __name__ == "__main__":
    main()
