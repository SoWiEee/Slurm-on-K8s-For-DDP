"""Phase 6 M4 — offline trace replay simulator.

Pure-Python (stdlib only). Drives a discrete-event cluster simulation from
a normalized job trace and reports JCT / makespan / utilization / slowdown
across pluggable schedulers (FCFS, Slurm-multifactor, M3 score).
"""
