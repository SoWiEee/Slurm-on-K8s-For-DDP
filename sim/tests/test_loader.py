"""Sanity tests — synthetic generator + round-trip through normalized JSON."""
import json
import os
import tempfile
import unittest

from sim.loader import (
    MPS_PER_GPU,
    Job,
    generate_philly_like,
    load_auto,
    write_normalized,
)


class LoaderTest(unittest.TestCase):
    def test_synthetic_generator_is_deterministic_and_well_shaped(self):
        a = generate_philly_like(100, seed=7)
        b = generate_philly_like(100, seed=7)
        self.assertEqual([j.as_dict() for j in a], [j.as_dict() for j in b])

        self.assertEqual(len(a), 100)
        for j in a:
            self.assertIsInstance(j, Job)
            self.assertGreater(j.runtime, 0)
            self.assertIn(j.gpu_count, {1, 2, 4, 8})
            self.assertGreaterEqual(j.mps_req, 1)
            self.assertLessEqual(j.mps_req, MPS_PER_GPU)
        # at least one fractional-MPS job within 100 samples
        self.assertTrue(any(j.mps_req < MPS_PER_GPU for j in a))
        # submit timestamps non-decreasing
        ts = [j.submit_ts for j in a]
        self.assertEqual(ts, sorted(ts))

    def test_normalized_roundtrip(self):
        jobs = generate_philly_like(20, seed=1)
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "trace.json")
            write_normalized(jobs, path)
            loaded = load_auto(path)
            self.assertEqual(len(loaded), 20)
            self.assertEqual(loaded[0].as_dict(), jobs[0].as_dict())

    def test_philly_format_autodetect(self):
        # minimal Philly-shaped record
        sample = [
            {
                "jobid": "x1",
                "user": "alice",
                "vc": "rtx4070",
                "submitted_time": "2018-01-01 00:00:00",
                "attempts": [{
                    "start_time": "2018-01-01 00:01:00",
                    "end_time": "2018-01-01 00:11:00",
                    "detail": [{"ip": "n1", "gpus": ["g0"]}],
                }],
            }
        ]
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "philly.json")
            with open(path, "w") as fh:
                json.dump(sample, fh)
            jobs = load_auto(path)
            self.assertEqual(len(jobs), 1)
            self.assertEqual(jobs[0].gpu_count, 1)
            self.assertAlmostEqual(jobs[0].runtime, 600.0, places=1)


if __name__ == "__main__":
    unittest.main()
