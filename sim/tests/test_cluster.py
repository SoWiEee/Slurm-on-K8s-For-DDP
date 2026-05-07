"""Cluster allocation: whole-GPU spanning, MPS fractions, release."""
import unittest

from sim.cluster import Cluster
from sim.loader import MPS_PER_GPU, Job


def _job(jid, gpu, mps=MPS_PER_GPU):
    return Job(job_id=jid, user="u", gpu_count=gpu, gpu_type="rtx4070",
               submit_ts=0.0, runtime=10.0, mem_req=0.0, mps_req=mps)


class ClusterTest(unittest.TestCase):
    def test_whole_gpu_allocate_and_release(self):
        c = Cluster(n_nodes=2, gpus_per_node=2)
        self.assertEqual(c.total_gpus(), 4)
        self.assertTrue(c.try_allocate(_job("j1", 4)))
        self.assertEqual(c.utilization(), 1.0)
        self.assertIsNone(c.try_allocate(_job("j2", 1)))
        c.release("j1")
        self.assertEqual(c.utilization(), 0.0)
        self.assertTrue(c.try_allocate(_job("j2", 1)))

    def test_mps_fractional_packs_on_one_gpu(self):
        c = Cluster(n_nodes=1, gpus_per_node=1, mps_per_gpu=4)
        self.assertTrue(c.try_allocate(_job("a", 1, mps=2)))
        self.assertTrue(c.try_allocate(_job("b", 1, mps=2)))
        self.assertIsNone(c.try_allocate(_job("c", 1, mps=1)))
        c.release("a")
        self.assertTrue(c.try_allocate(_job("c", 1, mps=2)))

    def test_multi_node_span(self):
        c = Cluster(n_nodes=2, gpus_per_node=2)
        # 3-GPU job must span both nodes
        plan = c.try_allocate(_job("big", 3))
        self.assertIsNotNone(plan)
        self.assertEqual(sum(len(a.gpu_indices) for a in plan), 3)
        nodes_used = {a.node_id for a in plan}
        self.assertEqual(nodes_used, {0, 1})


if __name__ == "__main__":
    unittest.main()
