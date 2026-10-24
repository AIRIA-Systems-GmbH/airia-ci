# SPDX-FileCopyrightText: 2026 AIRIA Systems GmbH
# SPDX-License-Identifier: Apache-2.0
"""runners/measure.sh: the sampler for the next burst (#27).

What these tests hold: a sample line carries the load, the "some avg10" pressure values, the free
memory and the listening-port count read from the /proc root it is given; a listener that appears
between two samples is named on the later line and only there; a host without /proc or without
pressure files degrades to NA instead of failing; the output goes to stdout or is appended to a
file; and a bad duration is refused. The /proc root is a scratch directory (MEASURE_PROC).

Run: python3 -m unittest discover -s tests
"""

from __future__ import annotations

import re
import unittest

from steps import ROOT, Runner

MEASURE = ROOT / "runners/measure.sh"
TCP_HEADER = "  sl  local_address rem_address   st tx_queue rx_queue\n"


def tcp_line(port: int, state: str = "0A") -> str:
    return f"   0: 0100007F:{port:04X} 00000000:0000 {state} 00000000:00000000 00:00000000 0 0 1 1\n"


class Measure(unittest.TestCase):
    def setUp(self):
        self.r = Runner(self)
        self.proc = self.r.tmp / "proc"
        (self.proc / "net").mkdir(parents=True)
        (self.proc / "pressure").mkdir()

    def write(self, name, text):
        (self.proc / name).write_text(text)

    def full_proc(self, ports=(22, 6556)):
        self.write("loadavg", "6.38 2.36 0.92 9/1345 1259798\n")
        self.write("meminfo", "MemTotal: 16777216 kB\nMemAvailable:   13562880 kB\n")
        self.write("pressure/cpu", "some avg10=0.28 avg60=0.48 avg300=0.18 total=1\nfull avg10=0.00 avg60=0 avg300=0 total=1\n")
        self.write("pressure/io", "some avg10=0.17 avg60=2.66 avg300=1.52 total=1\nfull avg10=0.12 avg60=0 avg300=0 total=1\n")
        self.write("pressure/memory", "some avg10=0.00 avg60=0 avg300=0 total=1\n")
        self.write("net/tcp", TCP_HEADER + "".join(tcp_line(p) for p in ports) + tcp_line(9999, "01"))
        self.write("net/tcp6", TCP_HEADER + tcp_line(22))

    def measure(self, *args, **env):
        return self.r.script(MEASURE, *args, MEASURE_PROC=str(self.proc), MEASURE_INTERVAL="0", **env)

    def test_a_sample_line_carries_every_measure(self):
        self.full_proc()
        p = self.measure("1")
        self.assertEqual(p.returncode, 0, p.stderr)
        line = p.stdout.strip()
        self.assertRegex(line, r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ ")
        self.assertIn(
            "load=6.38,2.36,0.92 psi_cpu=0.28 psi_io=0.17 psi_mem=0.00 mem_avail_mb=13245 listen=2 new=-", line
        )

    def test_one_line_per_second_requested(self):
        self.full_proc()
        self.assertEqual(len(self.measure("3").stdout.splitlines()), 3)

    def test_a_connected_socket_is_not_a_listener_and_a_port_on_both_stacks_counts_once(self):
        self.full_proc(ports=(22, 80))
        self.assertIn("listen=2 ", self.measure("1").stdout)  # 22 (v4 and v6) and 80; 9999 is state 01

    def test_a_new_listener_is_named_on_the_sample_it_first_appears_in(self):
        self.full_proc(ports=(22,))
        # A fake `sleep` opens a new listener between the first and the second sample.
        self.r.tool(
            "sleep",
            f'#!/usr/bin/env bash\nprintf %s \'{TCP_HEADER}{tcp_line(22)}{tcp_line(31893)}\' > "{self.proc}/net/tcp"\n',
        )
        lines = self.measure("3").stdout.splitlines()
        news = [re.search(r"new=(\S+)$", ln).group(1) for ln in lines]
        self.assertEqual(news, ["-", "31893", "-"])

    def test_the_first_sample_is_the_baseline_and_names_nothing(self):
        self.full_proc()
        self.assertTrue(self.measure("1").stdout.strip().endswith("new=-"))

    def test_without_proc_every_value_is_NA_and_it_still_succeeds(self):
        p = self.r.script(MEASURE, "1", MEASURE_PROC=str(self.r.tmp / "absent"), MEASURE_INTERVAL="0")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("load=NA psi_cpu=NA psi_io=NA psi_mem=NA mem_avail_mb=NA listen=0 new=-", p.stdout)

    def test_without_pressure_files_only_those_values_are_NA(self):
        self.full_proc()
        for f in ("cpu", "io", "memory"):
            (self.proc / "pressure" / f).unlink()
        self.assertIn("load=6.38,2.36,0.92 psi_cpu=NA psi_io=NA psi_mem=NA mem_avail_mb=13245", self.measure("1").stdout)

    def test_an_outfile_is_appended_to_and_stdout_stays_empty(self):
        self.full_proc()
        out = self.r.tmp / "burst.log"
        out.write_text("earlier\n")
        p = self.measure("2", str(out))
        self.assertEqual(p.stdout, "")
        lines = out.read_text().splitlines()
        self.assertEqual(lines[0], "earlier")
        self.assertEqual(len(lines), 3)

    def test_the_default_interval_is_one_second_between_samples(self):
        self.full_proc()
        self.r.tool("sleep", f'#!/usr/bin/env bash\necho "$1" >> "{self.r.tmp}/sleeps"\n')
        p = self.r.script(MEASURE, "2", MEASURE_PROC=str(self.proc))
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual((self.r.tmp / "sleeps").read_text(), "1\n")  # between two samples, not after the last

    def test_a_duration_that_is_not_a_number_is_refused(self):
        p = self.measure("soon")
        self.assertEqual(p.returncode, 2)
        self.assertIn("usage", p.stderr)


if __name__ == "__main__":
    unittest.main()
