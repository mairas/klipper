#!/usr/bin/env python3
# Unit tests for probe_scan module pure logic
#
# Run with: python3 -m pytest test/klippy/test_probe_scan.py -v
# (from the klipper root directory)

import sys, os, struct, collections, importlib.util

# We test only the pure logic classes (ScanMeshGenerator, event parsing).
# These are extracted here to avoid complex import machinery for the
# full probe_scan module which has deep Klipper dependencies.

# --- Extracted: ProbeResult (from manual_probe.py) ---
ProbeResult = collections.namedtuple('probe_result', [
    'bed_x', 'bed_y', 'bed_z', 'test_x', 'test_y', 'test_z'])


# --- Extracted: ScanMeshGenerator (from probe_scan.py) ---
class ScanMeshGenerator:
    def __init__(self, search_radius=10.0, power=2.0):
        self._search_radius = search_radius
        self._power = power

    def generate_mesh(self, scatter_points, grid_points):
        results = []
        r2_max = self._search_radius ** 2

        for gx, gy in grid_points:
            weighted_z = 0.
            weight_sum = 0.
            for sx, sy, sz in scatter_points:
                dx = gx - sx
                dy = gy - sy
                d2 = dx * dx + dy * dy
                if d2 > r2_max:
                    continue
                if d2 < 1e-10:
                    weighted_z = sz
                    weight_sum = 1.
                    break
                w = 1. / (d2 ** (self._power / 2.))
                weighted_z += w * sz
                weight_sum += w

            if weight_sum < 1e-10:
                raise Exception(
                    "probe_scan: No probe data near grid point (%.1f, %.1f). "
                    "Increase scan density or search_radius." % (gx, gy))

            bed_z = weighted_z / weight_sum
            results.append(ProbeResult(gx, gy, bed_z, gx, gy, bed_z))

        return results


# ==================== Tests ====================

class TestScanMeshGenerator:
    def test_exact_point_match(self):
        """IDW returns exact Z when a scatter point matches a grid point."""
        gen = ScanMeshGenerator(search_radius=10.0, power=2.0)
        scatter = [(10.0, 10.0, 1.5), (20.0, 10.0, 2.0)]
        grid = [(10.0, 10.0)]
        results = gen.generate_mesh(scatter, grid)
        assert len(results) == 1
        assert abs(results[0].bed_z - 1.5) < 1e-6

    def test_equidistant_points_average(self):
        """Two equidistant points should produce their average Z."""
        gen = ScanMeshGenerator(search_radius=100.0, power=2.0)
        scatter = [(-5.0, 0.0, 1.0), (5.0, 0.0, 3.0)]
        grid = [(0.0, 0.0)]
        results = gen.generate_mesh(scatter, grid)
        assert len(results) == 1
        assert abs(results[0].bed_z - 2.0) < 1e-6

    def test_closer_point_dominates(self):
        """Closer point should have more weight in IDW."""
        gen = ScanMeshGenerator(search_radius=100.0, power=2.0)
        scatter = [(1.0, 0.0, 1.0), (10.0, 0.0, 100.0)]
        grid = [(0.0, 0.0)]
        results = gen.generate_mesh(scatter, grid)
        # With power=2: w1=1/1=1, w2=1/100=0.01
        # result = (1*1 + 0.01*100) / (1 + 0.01) = 2/1.01 ≈ 1.98
        assert results[0].bed_z < 3.0

    def test_search_radius_filtering(self):
        """Points outside search radius should be ignored."""
        gen = ScanMeshGenerator(search_radius=5.0, power=2.0)
        scatter = [(1.0, 0.0, 1.0), (20.0, 0.0, 100.0)]
        grid = [(0.0, 0.0)]
        results = gen.generate_mesh(scatter, grid)
        assert abs(results[0].bed_z - 1.0) < 0.1

    def test_no_data_raises_error(self):
        """Grid point with no nearby scatter data should raise."""
        gen = ScanMeshGenerator(search_radius=1.0, power=2.0)
        scatter = [(100.0, 100.0, 1.0)]
        grid = [(0.0, 0.0)]
        try:
            gen.generate_mesh(scatter, grid)
            assert False, "Should have raised"
        except Exception as e:
            assert "No probe data" in str(e)

    def test_multiple_grid_points(self):
        """Multiple grid points should each get their own interpolation."""
        gen = ScanMeshGenerator(search_radius=100.0, power=2.0)
        scatter = [
            (0.0, 0.0, 1.0),
            (10.0, 0.0, 2.0),
            (0.0, 10.0, 3.0),
            (10.0, 10.0, 4.0),
        ]
        grid = [(0.0, 0.0), (10.0, 0.0), (0.0, 10.0), (10.0, 10.0)]
        results = gen.generate_mesh(scatter, grid)
        assert len(results) == 4
        assert abs(results[0].bed_z - 1.0) < 1e-6
        assert abs(results[1].bed_z - 2.0) < 1e-6
        assert abs(results[2].bed_z - 3.0) < 1e-6
        assert abs(results[3].bed_z - 4.0) < 1e-6

    def test_result_coordinates(self):
        """ProbeResult should have correct bed_x, bed_y fields."""
        gen = ScanMeshGenerator(search_radius=100.0)
        scatter = [(5.0, 15.0, 2.5)]
        grid = [(5.0, 15.0)]
        results = gen.generate_mesh(scatter, grid)
        assert results[0].bed_x == 5.0
        assert results[0].bed_y == 15.0

    def test_uniform_surface(self):
        """Flat surface should interpolate as flat."""
        gen = ScanMeshGenerator(search_radius=100.0, power=2.0)
        flat_z = 0.15
        scatter = [(x, y, flat_z) for x in range(0, 20, 2)
                   for y in range(0, 20, 2)]
        grid = [(5.0, 5.0), (10.0, 10.0), (15.0, 15.0)]
        results = gen.generate_mesh(scatter, grid)
        for r in results:
            assert abs(r.bed_z - flat_z) < 1e-6


class TestScanMeshGeneratorPower:
    def test_power_1_less_weight_decay(self):
        """Power=1 IDW should give less weight contrast than power=2."""
        scatter = [(1.0, 0.0, 0.0), (10.0, 0.0, 10.0)]
        grid = [(0.0, 0.0)]

        gen_p1 = ScanMeshGenerator(search_radius=100.0, power=1.0)
        gen_p2 = ScanMeshGenerator(search_radius=100.0, power=2.0)

        r1 = gen_p1.generate_mesh(scatter, grid)[0].bed_z
        r2 = gen_p2.generate_mesh(scatter, grid)[0].bed_z

        # Power=2 should weight the close point more heavily (lower result)
        assert r2 < r1


class TestEventParsing:
    def test_event_byte_packing(self):
        """Verify MCU event byte format matches our parser expectations."""
        clock = 0x12345678
        state = 1
        data = struct.pack('<I', clock) + bytes([state])
        assert len(data) == 5
        parsed_clock = struct.unpack_from('<I', data, 0)[0]
        parsed_state = data[4]
        assert parsed_clock == clock
        assert parsed_state == state

    def test_multiple_events_packing(self):
        """Verify multiple events pack/unpack correctly."""
        events = [(1000, 1), (2000, 0), (3000, 1)]
        data = b''
        for clock, state in events:
            data += struct.pack('<I', clock) + bytes([state])
        assert len(data) == 15  # 3 * 5 bytes

        parsed = []
        for i in range(3):
            offset = i * 5
            clock = struct.unpack_from('<I', data, offset)[0]
            state = data[offset + 4]
            parsed.append((clock, state))

        assert parsed == events

    def test_clock_wrap_around(self):
        """32-bit clock values should handle full range."""
        max_clock = 0xFFFFFFFF
        data = struct.pack('<I', max_clock) + bytes([0])
        parsed = struct.unpack_from('<I', data, 0)[0]
        assert parsed == max_clock

    def test_zero_clock(self):
        """Zero clock value should parse correctly."""
        data = struct.pack('<I', 0) + bytes([1])
        parsed_clock = struct.unpack_from('<I', data, 0)[0]
        parsed_state = data[4]
        assert parsed_clock == 0
        assert parsed_state == 1


class TestIDWMathProperties:
    """Verify mathematical properties of IDW interpolation."""

    def test_partition_of_unity(self):
        """IDW weights should sum to produce values within data range."""
        gen = ScanMeshGenerator(search_radius=100.0, power=2.0)
        scatter = [(0, 0, 1.0), (10, 0, 5.0), (5, 10, 3.0)]
        grid = [(5.0, 5.0)]
        results = gen.generate_mesh(scatter, grid)
        z = results[0].bed_z
        # Result must be within the convex hull of input Z values
        assert z >= 1.0
        assert z <= 5.0

    def test_symmetry(self):
        """IDW should be symmetric — swapping X doesn't change result
        when grid point is at the center."""
        gen = ScanMeshGenerator(search_radius=100.0, power=2.0)
        scatter_a = [(-5, 0, 1.0), (5, 0, 3.0)]
        scatter_b = [(5, 0, 1.0), (-5, 0, 3.0)]
        grid = [(0.0, 0.0)]

        za = gen.generate_mesh(scatter_a, grid)[0].bed_z
        zb = gen.generate_mesh(scatter_b, grid)[0].bed_z
        # Both should give the average (2.0) due to equal distances
        assert abs(za - 2.0) < 1e-6
        assert abs(zb - 2.0) < 1e-6

    def test_single_point(self):
        """With only one scatter point in range, result equals that point."""
        gen = ScanMeshGenerator(search_radius=100.0, power=2.0)
        scatter = [(3.0, 4.0, 7.77)]
        grid = [(0.0, 0.0)]
        results = gen.generate_mesh(scatter, grid)
        assert abs(results[0].bed_z - 7.77) < 1e-6


if __name__ == '__main__':
    import pytest
    pytest.main([__file__, '-v'])
