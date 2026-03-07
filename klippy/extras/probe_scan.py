# Continuous bed mesh scanning with binary (on/off) inductive probe
#
# Copyright (C) 2026  Matti Airas
#
# This file may be distributed under the terms of the GNU GPLv3 license.
import logging, struct
import pins
from . import manual_probe, probe

HYSTERESIS_SAMPLES = 3
HYSTERESIS_LIFT_CLEARANCE = 5.  # mm above z_offset for hysteresis cal
MIN_TRIGGER_EVENTS = 3
TRAVEL_SPEED = 50.              # mm/s for non-scanning moves
POST_SCAN_LIFT = 10.            # mm to raise after scan completes
REACTOR_POLL_DELAY = 0.001      # seconds to yield for serial processing
FINAL_EVENT_DELAY = 0.1         # seconds to wait for trailing events
# Retransmit countdown: number of poll ticks before re-sending unacked data.
# At 50us poll interval, 50 ticks = 2.5ms timeout.
RETRANSMIT_COUNT = 50


# MCU communication helper — manages probe_scan firmware module
class ProbeScanHelper:
    def __init__(self, config, mcu, pin_params):
        self._printer = config.get_printer()
        self._mcu = mcu
        self._pin = pin_params['pin']
        self._pullup = pin_params['pullup']
        self._invert = pin_params['invert']
        self._oid = self._mcu.create_oid()
        # Events accumulate across entire scan. Appends from the serial
        # callback and reads from the scan loop are safe under CPython's GIL
        # (list.append is atomic, slicing copies).
        self._all_events = []
        self._ack_count = 0
        self._start_cmd = self._stop_cmd = self._ack_cmd = None
        self._mcu.register_config_callback(self._build_config)

    def _build_config(self):
        self._mcu.add_config_cmd(
            "config_probe_scan oid=%d pin=%s pull_up=%d"
            % (self._oid, self._pin, self._pullup))
        cmd_queue = self._mcu.alloc_command_queue()
        self._start_cmd = self._mcu.lookup_command(
            "probe_scan_start oid=%c clock=%u rest_ticks=%u"
            " sample_count=%c invert=%c retransmit_count=%c", cq=cmd_queue)
        self._stop_cmd = self._mcu.lookup_command(
            "probe_scan_stop oid=%c", cq=cmd_queue)
        self._ack_cmd = self._mcu.lookup_command(
            "probe_scan_ack oid=%c count=%c", cq=cmd_queue)
        self._mcu.register_serial_response(
            self._handle_probe_scan_state,
            "probe_scan_state oid=%c ack_count=%c overflow=%c events=%*s",
            self._oid)

    def _handle_probe_scan_state(self, params):
        overflow = params['overflow']
        if overflow:
            logging.warning("probe_scan: MCU event buffer overflow "
                            "(%d events lost)" % overflow)
        # Deduplication: skip already-processed events on retransmit
        # (follows buttons.py ack_count pattern)
        ack_count = self._ack_count
        ack_diff = (params['ack_count'] - ack_count) & 0xff
        ack_diff -= (ack_diff & 0x80) << 1
        msg_ack_count = ack_count + ack_diff
        data = params['events']
        event_size = 5
        count = len(data) // event_size
        new_count = msg_ack_count + count - self._ack_count
        if new_count <= 0:
            return
        # Only process the last new_count events from this message
        start = count - new_count
        for i in range(start, count):
            offset = i * event_size
            clock32 = struct.unpack_from('<I', data, offset)[0]
            state = data[offset + 4]
            clock64 = self._mcu.clock32_to_clock64(clock32)
            print_time = self._mcu.clock_to_print_time(clock64)
            self._all_events.append((print_time, state))
        self._ack_cmd.send([self._oid, new_count])
        self._ack_count += new_count

    def start_collection(self, print_time, poll_us, sample_count):
        self._all_events = []
        self._ack_count = 0
        clock = self._mcu.print_time_to_clock(print_time)
        rest_ticks = self._mcu.seconds_to_clock(poll_us / 1e6)
        self._start_cmd.send(
            [self._oid, clock, rest_ticks, sample_count, self._invert,
             RETRANSMIT_COUNT],
            reqclock=clock)

    def stop_collection(self):
        self._stop_cmd.send([self._oid])

    def get_new_events_since(self, index):
        """Return events from index onward (non-destructive)."""
        return self._all_events[index:]

    def get_all_events(self):
        """Return all accumulated events for post-scan correlation."""
        return list(self._all_events)


# Endstop wrapper for probe_scan pin — enables probing_move() and
# query_endstop() without depending on a separate [probe] section.
# Uses a standard MCU_endstop on the same pin as the scan GPIO monitor.
class ProbeScanEndstop:
    def __init__(self, config, mcu, pin_params):
        self._printer = config.get_printer()
        # Create a standard MCU_endstop for probing moves
        self._mcu_endstop = mcu.setup_pin('endstop', pin_params)
        # Register Z steppers when they become available
        probe.LookupZSteppers(config, self._mcu_endstop.add_stepper)

    def get_mcu(self):
        return self._mcu_endstop.get_mcu()

    def add_stepper(self, stepper):
        self._mcu_endstop.add_stepper(stepper)

    def get_steppers(self):
        return self._mcu_endstop.get_steppers()

    def home_start(self, print_time, sample_time, sample_count, rest_time,
                   triggered=True):
        return self._mcu_endstop.home_start(
            print_time, sample_time, sample_count, rest_time,
            triggered=triggered)

    def home_wait(self, home_end_time):
        return self._mcu_endstop.home_wait(home_end_time)

    def query_endstop(self, print_time):
        return self._mcu_endstop.query_endstop(print_time)

    # Probe lifecycle — no-ops for a simple inductive probe
    def multi_probe_begin(self):
        pass

    def multi_probe_end(self):
        pass

    def probe_prepare(self, hmove):
        pass

    def probe_finish(self, hmove):
        pass


# Reactive scan controller — plans motion segments, reverses Z on triggers
class ScanController:
    def __init__(self, config):
        self.scan_speed = config.getfloat('scan_speed', 50., above=0.)
        self.z_speed = config.getfloat('z_speed', 5., above=0.)
        self.z_max_amplitude = config.getfloat('z_max_amplitude', 3.,
                                               above=0.)
        self.segment_length = config.getfloat('segment_length', 2., above=0.5)

    def run_scan(self, printer, probe_helper, x_start, x_end, y, z_start,
                 x_offset, y_offset, gcmd):
        toolhead = printer.lookup_object('toolhead')
        reactor = printer.get_reactor()

        scan_speed = self.scan_speed
        z_speed = self.z_speed
        segment_length = self.segment_length

        segment_time = segment_length / scan_speed
        dz_per_segment = z_speed * segment_time

        z_direction = -1  # Start descending toward bed
        x = x_start - x_offset
        z = z_start
        z_min = z_start - self.z_max_amplitude
        z_max = z_start + self.z_max_amplitude

        x_dir = 1 if x_end > x_start else -1
        x_travel = abs(x_end - x_start)
        x_done = 0.
        event_cursor = len(probe_helper.get_new_events_since(0))

        while x_done < x_travel:
            remaining = x_travel - x_done
            dx = min(segment_length, remaining) * x_dir

            dz = z_direction * dz_per_segment
            new_z = max(z_min, min(z_max, z + dz))

            new_x = x + dx
            toolhead.manual_move([new_x, y - y_offset, new_z], scan_speed)

            x = new_x
            z = new_z
            x_done += abs(dx)

            # Yield to reactor for serial processing
            eventtime = reactor.monotonic()
            reactor.pause(eventtime + REACTOR_POLL_DELAY)

            # Check for new trigger events to update Z direction
            new_events = probe_helper.get_new_events_since(event_cursor)
            event_cursor += len(new_events)
            for event_time, state in new_events:
                if state == 1:
                    z_direction = 1   # Triggered — ascend
                elif state == 0:
                    z_direction = -1  # Untriggered — descend

            # Safety bounds: reverse if stuck at limit
            if z <= z_min and z_direction == -1:
                gcmd.respond_info(
                    "probe_scan: Z hit lower bound (%.2f) at X=%.1f Y=%.1f"
                    % (z_min, x + x_offset, y))
                z_direction = 1
            elif z >= z_max and z_direction == 1:
                gcmd.respond_info(
                    "probe_scan: Z hit upper bound (%.2f) at X=%.1f Y=%.1f"
                    % (z_max, x + x_offset, y))
                z_direction = -1


# Correlates MCU events with toolhead positions via trapq lookup
class ScanEventCollector:
    def __init__(self, printer):
        self._printer = printer

    def correlate_events(self, events, hysteresis, x_offset, y_offset,
                         z_offset):
        toolhead = self._printer.lookup_object('toolhead')
        kin = toolhead.get_kinematics()
        points = []
        for print_time, state in events:
            kin_spos = {
                s.get_name(): s.mcu_to_commanded_position(
                    s.get_past_mcu_position(print_time))
                for s in kin.get_steppers()
            }
            pos = kin.calc_position(kin_spos)

            bed_x = pos[0] + x_offset
            bed_y = pos[1] + y_offset
            if state == 1:
                bed_z = pos[2] - z_offset
            else:
                bed_z = pos[2] - z_offset + hysteresis

            points.append((bed_x, bed_y, bed_z))
        return points


# Interpolates scattered probe points onto a regular grid using IDW
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
            results.append(manual_probe.ProbeResult(
                gx, gy, bed_z, gx, gy, bed_z))

        return results


# Main orchestrator — registers PROBE_SCAN_MESH command
class PrinterProbeScan:
    def __init__(self, config):
        self._printer = config.get_printer()

        # Register pin with share_type so both the scan GPIO monitor and
        # the endstop can use the same physical pin, while preventing
        # unrelated modules from claiming it.
        ppins = config.get_printer().lookup_object('pins')
        pin_params = ppins.lookup_pin(config.get('pin'),
                                      can_invert=True, can_pullup=True,
                                      share_type='probe_scan')
        mcu = pin_params['chip']

        # Continuous scan GPIO monitor
        self._probe_helper = ProbeScanHelper(config, mcu, pin_params)

        # Standard endstop on same pin for probing moves (hysteresis cal)
        self._endstop = ProbeScanEndstop(config, mcu, pin_params)

        # Probe offsets — z_offset is the trigger distance above the bed
        # (positive = probe triggers before nozzle reaches bed)
        self._x_offset = config.getfloat('x_offset', 0.)
        self._y_offset = config.getfloat('y_offset', 0.)
        self._z_offset = config.getfloat('z_offset')

        # Scan parameters
        self._poll_us = config.getfloat('poll_us', 50.)
        self._sample_count = config.getint('sample_count', 2, minval=1)
        self._max_hysteresis = config.getfloat('max_hysteresis', 0.5,
                                               above=0.)
        self._cal_point = config.getfloatlist('calibration_point', None,
                                              count=2)
        self._z_position_min = probe.lookup_minimum_z(config)
        self._scan_controller = ScanController(config)
        self._mesh_generator = ScanMeshGenerator()

        # Register command
        gcode = self._printer.lookup_object('gcode')
        gcode.register_command('PROBE_SCAN_MESH', self.cmd_PROBE_SCAN_MESH,
                               desc=self.cmd_PROBE_SCAN_MESH_help)

    cmd_PROBE_SCAN_MESH_help = "Perform continuous scanning bed mesh"

    def _get_calibration_point(self):
        if self._cal_point is not None:
            return self._cal_point
        bmc_obj = self._printer.lookup_object('bed_mesh')
        bmc = bmc_obj.bmc
        min_x, min_y = bmc.mesh_min
        max_x, max_y = bmc.mesh_max
        return ((min_x + max_x) / 2., (min_y + max_y) / 2.)

    def _measure_hysteresis(self, gcmd):
        """Measure probe hysteresis using our own endstop interface."""
        toolhead = self._printer.lookup_object('toolhead')
        phoming = self._printer.lookup_object('homing')

        cal_x, cal_y = self._get_calibration_point()
        gcmd.respond_info(
            "probe_scan: Measuring hysteresis at (%.1f, %.1f)"
            % (cal_x, cal_y))

        lift_z = self._z_offset + HYSTERESIS_LIFT_CLEARANCE
        toolhead.manual_move(
            [cal_x - self._x_offset, cal_y - self._y_offset, lift_z],
            TRAVEL_SPEED)
        toolhead.wait_moves()

        z_speed = self._scan_controller.z_speed
        endstop = self._endstop

        hysteresis_samples = []
        for i in range(HYSTERESIS_SAMPLES):
            # Descend until trigger — use configured Z minimum as bound
            pos = toolhead.get_position()
            target = list(pos)
            target[2] = self._z_position_min
            trig_pos = phoming.probing_move(endstop, target, z_speed)
            z_trigger_on = trig_pos[2]

            # Ascend until untrigger using triggered=False
            pos = toolhead.get_position()
            target = list(pos)
            target[2] = lift_z
            endstops = [(endstop, "probe")]
            untrig_pos = phoming.manual_home(
                toolhead, endstops, target, z_speed,
                probe_pos=True, triggered=False, check_triggered=True)
            z_trigger_off = untrig_pos[2]

            hyst = z_trigger_off - z_trigger_on
            hysteresis_samples.append(hyst)
            gcmd.respond_info(
                "  Sample %d: trigger=%.4f untrigger=%.4f hysteresis=%.4f"
                % (i + 1, z_trigger_on, z_trigger_off, hyst))

            # Raise for next sample
            toolhead.manual_move([None, None, lift_z], TRAVEL_SPEED)
            toolhead.wait_moves()

        hysteresis_samples.sort()
        hysteresis = hysteresis_samples[HYSTERESIS_SAMPLES // 2]

        gcmd.respond_info(
            "probe_scan: Measured hysteresis = %.4f mm" % hysteresis)

        if hysteresis > self._max_hysteresis:
            raise gcmd.error(
                "probe_scan: Measured hysteresis (%.4f) exceeds "
                "max_hysteresis (%.4f)" % (hysteresis, self._max_hysteresis))
        if hysteresis < 0.:
            raise gcmd.error(
                "probe_scan: Negative hysteresis (%.4f) — probe malfunction?"
                % hysteresis)

        return hysteresis

    def cmd_PROBE_SCAN_MESH(self, gcmd):
        bmc_obj = self._printer.lookup_object('bed_mesh')
        bmc = bmc_obj.bmc
        base_points = list(bmc.probe_mgr.get_base_points())
        mesh_config = dict(bmc.mesh_config)

        if not base_points:
            raise gcmd.error("probe_scan: No mesh points configured in "
                             "[bed_mesh]")

        x_count = mesh_config['x_count']
        y_count = mesh_config['y_count']

        min_x = min(p[0] for p in base_points)
        max_x = max(p[0] for p in base_points)
        min_y = min(p[1] for p in base_points)
        max_y = max(p[1] for p in base_points)

        gcmd.respond_info(
            "probe_scan: Mesh %d x %d points, "
            "X: %.1f-%.1f, Y: %.1f-%.1f"
            % (x_count, y_count, min_x, max_x, min_y, max_y))

        toolhead = self._printer.lookup_object('toolhead')

        # Step 1: Measure hysteresis
        hysteresis = self._measure_hysteresis(gcmd)

        # Step 2: Move to scan start position
        # z_offset is trigger distance above bed (positive).
        # Start scanning at z_offset so the probe oscillates around the
        # trigger threshold. Safety bounds extend ±z_max_amplitude from here.
        scan_z = self._z_offset
        toolhead.manual_move([None, None, scan_z + HYSTERESIS_LIFT_CLEARANCE],
                             TRAVEL_SPEED)
        toolhead.manual_move(
            [min_x - self._x_offset, min_y - self._y_offset, None],
            TRAVEL_SPEED)
        toolhead.manual_move([None, None, scan_z], TRAVEL_SPEED)
        toolhead.wait_moves()

        # Step 3: Start MCU event collection
        print_time = toolhead.get_last_move_time()
        self._probe_helper.start_collection(
            print_time, self._poll_us, self._sample_count)

        # Step 4: Run serpentine scan (try/finally ensures MCU stops polling)
        try:
            gcmd.respond_info("probe_scan: Starting scan...")
            y_rows = sorted(set(p[1] for p in base_points))

            for row_idx, y in enumerate(y_rows):
                if row_idx % 2 == 0:
                    x_start, x_end = min_x, max_x
                else:
                    x_start, x_end = max_x, min_x

                if row_idx > 0:
                    toolhead.manual_move(
                        [None, y - self._y_offset, None],
                        self._scan_controller.scan_speed)

                self._scan_controller.run_scan(
                    self._printer, self._probe_helper,
                    x_start, x_end, y, scan_z,
                    self._x_offset, self._y_offset, gcmd)
        finally:
            # Step 5: Stop collection even on error
            self._probe_helper.stop_collection()

        toolhead.wait_moves()
        toolhead.flush_step_generation()

        # Allow final serial events to arrive
        reactor = self._printer.get_reactor()
        eventtime = reactor.monotonic()
        reactor.pause(eventtime + FINAL_EVENT_DELAY)

        # Step 6: Correlate all events with toolhead positions
        all_events = self._probe_helper.get_all_events()
        gcmd.respond_info(
            "probe_scan: Collected %d trigger events" % len(all_events))

        if len(all_events) < MIN_TRIGGER_EVENTS:
            raise gcmd.error(
                "probe_scan: Too few trigger events (%d). Check probe "
                "connection and z_offset." % len(all_events))

        collector = ScanEventCollector(self._printer)
        scatter_points = collector.correlate_events(
            all_events, hysteresis,
            self._x_offset, self._y_offset, self._z_offset)

        gcmd.respond_info(
            "probe_scan: %d surface points from event correlation"
            % len(scatter_points))

        # Step 7: Interpolate onto mesh grid and finalize
        results = self._mesh_generator.generate_mesh(
            scatter_points, base_points)

        # If zero_reference_position is configured outside the mesh,
        # probe_finalize expects an extra point at the end to pop as
        # the Z reference. Interpolate it from our scatter data.
        from .bed_mesh import ZrefMode
        if bmc.probe_mgr.get_zero_ref_mode() == ZrefMode.PROBE:
            zref_pos = bmc.probe_mgr.get_zero_ref_pos()
            zref_results = self._mesh_generator.generate_mesh(
                scatter_points, [zref_pos])
            results.append(zref_results[0])

        gcmd.respond_info("probe_scan: Finalizing mesh...")
        bmc.probe_finalize(results)

        toolhead.manual_move([None, None, scan_z + POST_SCAN_LIFT],
                             TRAVEL_SPEED)
        gcmd.respond_info("probe_scan: Mesh scan complete")


def load_config(config):
    return PrinterProbeScan(config)
