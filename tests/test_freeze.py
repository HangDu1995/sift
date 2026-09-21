import json
from pathlib import Path

import pytest

from sift.evals.baseline import freeze_baseline, read_meta, skill_name_mismatch
from sift.models import EvalCase, EvalSet


def _write_evals(path: Path, skill_name="sk", evals=None):
    es = EvalSet(skill_name=skill_name, evals=evals or [EvalCase(id=1, prompt="p", expectations=["e"])])
    path.write_text(json.dumps(es.model_dump(mode="json"), ensure_ascii=False), encoding="utf-8")
    return path


def test_freeze_produces_snapshot_and_meta(tmp_path):
    src = _write_evals(tmp_path / "evals.json", skill_name="log-error-analyzer")
    baseline, meta = freeze_baseline(src, name="baseline", target_skill_version="1.2.0")
    assert baseline == tmp_path / "baseline.json"
    assert meta == tmp_path / "baseline.meta.json"
    assert json.loads(baseline.read_text(encoding="utf-8"))["skill_name"] == "log-error-analyzer"
    m = json.loads(meta.read_text(encoding="utf-8"))
    assert m["skill_name"] == "log-error-analyzer"
    assert m["target_skill_version"] == "1.2.0"
    assert m["sift_version"]  # 非空
    assert m["frozen_at"]
    assert m["source_evals"] == "evals.json"


def test_freeze_out_dir_and_name(tmp_path):
    src = _write_evals(tmp_path / "evals.json")
    out = tmp_path / "baselines"
    baseline, meta = freeze_baseline(src, name="v1", out_dir=out)
    assert baseline == out / "v1.json"
    assert meta == out / "v1.meta.json"
    assert out.is_dir()


def test_freeze_overwrite_idempotent(tmp_path):
    src = _write_evals(tmp_path / "evals.json")
    freeze_baseline(src)
    baseline, meta = freeze_baseline(src)  # 二次
    assert baseline.exists() and meta.exists()


def test_freeze_rejects_invalid_evals(tmp_path):
    bad = tmp_path / "evals.json"
    bad.write_text("{not valid json", encoding="utf-8")
    with pytest.raises(Exception):
        freeze_baseline(bad)


def test_read_meta_present_and_absent(tmp_path):
    src = _write_evals(tmp_path / "evals.json")
    assert read_meta(src) is None  # 冻结前无 sidecar
    freeze_baseline(src, name="baseline")
    assert read_meta(tmp_path / "baseline.json") is not None  # baseline.json 有同名 sidecar


def test_skill_name_mismatch_no_sidecar():
    # 无 sidecar → 不校验(None)
    assert skill_name_mismatch(Path("/nonexistent/baseline.json"), "sk") is None


def test_skill_name_mismatch_detected(tmp_path):
    src = _write_evals(tmp_path / "evals.json", skill_name="skill-A")
    freeze_baseline(src, name="baseline")
    baseline = tmp_path / "baseline.json"
    assert skill_name_mismatch(baseline, "skill-B") is not None  # 不等 → 警告
    assert skill_name_mismatch(baseline, "skill-A") is None  # 相等 → 不警告


def test_freeze_preserves_trigger_probes(tmp_path):
    """freeze 是字节拷贝(copy2)→ trigger_probes 段原样钉进 baseline,不丢。"""
    from sift.models import TriggerProbe

    es = EvalSet(
        skill_name="sk",
        evals=[EvalCase(id=1, prompt="p", expectations=["e"])],
        trigger_probes=[
            TriggerProbe(id=1, prompt="q", expect_trigger=True),
            TriggerProbe(id=2, prompt="r", expect_trigger=False),
        ],
    )
    src = tmp_path / "evals.json"
    src.write_text(json.dumps(es.model_dump(mode="json"), ensure_ascii=False), encoding="utf-8")
    baseline, _meta = freeze_baseline(src, name="baseline")
    data = json.loads(baseline.read_text(encoding="utf-8"))
    assert len(data["trigger_probes"]) == 2
    assert data["trigger_probes"][0]["expect_trigger"] is True
    assert data["trigger_probes"][1]["expect_trigger"] is False
