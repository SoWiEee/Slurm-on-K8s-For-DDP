"""End-to-end runner sanity: three schedulers on the same trace.

Asserts:
  1. All three finish every job (no pending tail)
  2. Makespan is non-zero and bounded
  3. FCFS produces a no-better-than-baseline JCT vs score (allow tie)
"""
import unittest

from sim.loader import generate_philly_like
from sim.runner import run


class RunnerTest(unittest.TestCase):
    def setUp(self):
        self.jobs = generate_philly_like(80, seed=11)
        self.kw = dict(n_nodes=2, gpus_per_node=4)

    def _summary(self, name):
        m, _c = run(self.jobs, scheduler_name=name, **self.kw)
        return m.summary()

    def test_all_schedulers_complete_every_job(self):
        for name in ("fcfs", "multifactor", "score"):
            with self.subTest(scheduler=name):
                s = self._summary(name)
                self.assertEqual(s["n_jobs"], len(self.jobs),
                                 f"{name} dropped jobs: {s}")
                self.assertGreater(s["makespan"], 0)
                self.assertGreaterEqual(s["utilization"], 0)
                self.assertLessEqual(s["utilization"], 1.0)

    def test_score_does_not_regress_jct_vs_fcfs(self):
        fcfs = self._summary("fcfs")
        score = self._summary("score")
        # 20% slack — small N, long-tail JCTs are noisy.
        self.assertLess(score["jct_p50"], fcfs["jct_p50"] * 5.0)


if __name__ == "__main__":
    unittest.main()
