from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from sim.scheduler.score import submit_score_factors


ROOT = Path(__file__).resolve().parents[1]


def _lua_binary() -> str | None:
    for name in ("lua", "lua5.4", "lua5.3", "lua5.2", "luajit"):
        if path := shutil.which(name):
            return path
    return None


def _render_job_submit_lua() -> str:
    if not shutil.which("helm"):
        pytest.skip("helm is required to render the production job_submit.lua")

    rendered = subprocess.check_output(
        [
            "helm",
            "template",
            "slurm-platform",
            str(ROOT / "chart"),
            "--set",
            "slurm.jobSubmit.enabled=true",
            "--show-only",
            "templates/configmap-job-submit.yaml",
        ],
        cwd=ROOT,
        text=True,
    )

    lines = rendered.splitlines()
    try:
        start = next(i for i, line in enumerate(lines) if line.strip() == "job_submit.lua: |") + 1
    except StopIteration:
        raise AssertionError("rendered ConfigMap does not contain job_submit.lua")

    lua_lines: list[str] = []
    for line in lines[start:]:
        if line.startswith("    "):
            lua_lines.append(line[4:])
        elif not line.strip():
            lua_lines.append("")
        else:
            break
    return "\n".join(lua_lines) + "\n"


def _run_lua_cases(lua_source: str) -> dict[str, dict[str, float]]:
    lua = _lua_binary()
    if not lua:
        pytest.skip("Lua interpreter is required for score parity test")

    runner = r'''
slurm = {
  SUCCESS = 0,
  ERROR = -1,
  log_info = function(_) end,
  log_error = function(_) end,
}
dofile(arg[1])

local cases = {
  {"no_constraints", "", ""},
  {"mps_25_vram_12", "gpu:rtx4070:1,mps:25", "gpu-rtx4070&vram-12g"},
  {"mps_eq_50_vram_13", "gpu:rtx4070:1,mps=50", "vram-13g"},
  {"mps_100_vram_24", "mps:100", "vram-24g"},
  {"mps_over_vram_48", "mps:200", "vram-48g"},
}

for _, c in ipairs(cases) do
  local job_desc = { tres_per_node = c[2], features = c[3] }
  local score, mps_fit, vram_fit, topology, fragmentation, pred_runtime = compute_score(job_desc, nil)
  print(string.format(
    "%s\t%.12f\t%.12f\t%.12f\t%.12f\t%.12f\t%.12f",
    c[1], score, mps_fit, vram_fit, topology, fragmentation, pred_runtime))
end
'''

    with tempfile.TemporaryDirectory() as tmp:
        lua_path = Path(tmp) / "job_submit.lua"
        runner_path = Path(tmp) / "score_parity_runner.lua"
        lua_path.write_text(lua_source)
        runner_path.write_text(runner)
        output = subprocess.check_output([lua, str(runner_path), str(lua_path)], cwd=ROOT, text=True)

    actual: dict[str, dict[str, float]] = {}
    keys = ["score", "mps_fit", "vram_fit", "topology", "fragmentation", "pred_runtime"]
    for line in output.splitlines():
        name, *values = line.split("\t")
        actual[name] = dict(zip(keys, [float(v) for v in values]))
    return actual


def test_rendered_lua_score_matches_python_submit_reference():
    lua_source = _render_job_submit_lua()
    actual = _run_lua_cases(lua_source)

    cases = {
        "no_constraints": {"tres_per_node": "", "features": ""},
        "mps_25_vram_12": {
            "tres_per_node": "gpu:rtx4070:1,mps:25",
            "features": "gpu-rtx4070&vram-12g",
        },
        "mps_eq_50_vram_13": {"tres_per_node": "gpu:rtx4070:1,mps=50", "features": "vram-13g"},
        "mps_100_vram_24": {"tres_per_node": "mps:100", "features": "vram-24g"},
        "mps_over_vram_48": {"tres_per_node": "mps:200", "features": "vram-48g"},
    }

    assert set(actual) == set(cases)
    for name, kwargs in cases.items():
        expected = submit_score_factors(**kwargs)
        assert actual[name]["score"] == pytest.approx(expected.score)
        assert actual[name]["mps_fit"] == pytest.approx(expected.mps_fit)
        assert actual[name]["vram_fit"] == pytest.approx(expected.vram_fit)
        assert actual[name]["topology"] == pytest.approx(expected.topology)
        assert actual[name]["fragmentation"] == pytest.approx(expected.fragmentation)
        assert actual[name]["pred_runtime"] == pytest.approx(expected.pred_runtime)
