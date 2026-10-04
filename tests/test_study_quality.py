import copy

import pytest

from scripts.benchmark_week06 import load_handoff, phase_candidates
from src.analyze_week06 import model_memory_gib
from src.common import load_yaml, write_json
from src.study_contract import serve_command, validate_week05
from src.validate_outputs import check_output


def response(text):
    return {
        "choices": [{"text": text, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 4, "total_tokens": 14},
    }


def test_output_checks_do_not_confuse_valid_schema_with_correct_answer():
    case = {"check": {"type": "json", "value": {"answer": 42}}}
    assert check_output(case, response('{"answer":42}'), 10, 16)["passed"]
    wrong = check_output(case, response('{"answer":41}'), 10, 16)
    assert wrong["schema_valid"] and not wrong["passed"]
    for value in ("", "\ufffd", "NaN", "word " * 40):
        assert not check_output(case, response(value), 10, 16)["schema_valid"]
    assert not check_output(case, response('{"answer":42}'), 11, 16)["passed"]


def test_tuning_is_sequential_and_changes_one_parameter():
    config = load_yaml("configs/week06.yaml")
    assert [name for name, _ in phase_candidates(config, "representation")] == ["bf16", "awq"]
    with pytest.raises(ValueError, match="selected_variant"):
        phase_candidates(config, "sequences")
    config["tuning"]["selected_variant"] = "awq"
    assert [o for _, o in phase_candidates(config, "sequences")] == [
        {"max_num_seqs": n} for n in (8, 16, 32)
    ]
    with pytest.raises(ValueError, match="selected_max_num_seqs"):
        phase_candidates(config, "tokens")
    config["tuning"]["selected_max_num_seqs"] = 16
    assert phase_candidates(config, "tokens")[0][1] == {
        "max_num_seqs": 16,
        "max_num_batched_tokens": 2048,
    }


def test_variants_pin_tokenizer_and_keep_kv_cache_dtype_explicit():
    config = load_yaml("configs/week06.yaml")
    base = validate_week05(load_yaml("configs/week05.yaml"))
    argv = serve_command(base, variant=config["variants"]["awq"], tokenizer=config["tokenizer"])
    assert argv[argv.index("--dtype") + 1] == "float16"
    assert argv[argv.index("--quantization") + 1] == "awq"
    assert argv[argv.index("--tokenizer-revision") + 1] == config["tokenizer"]["revision"]
    assert argv[argv.index("--kv-cache-dtype") + 1] == "auto"


def test_unready_handoff_cannot_start_performance_runs(tmp_path):
    config = copy.deepcopy(load_yaml("configs/week06.yaml"))
    config["handoff"] = str(tmp_path / "handoff.json")
    write_json(config["handoff"], {"ready": False})
    with pytest.raises(ValueError, match="incomplete"):
        load_handoff(config)


def test_weight_memory_is_distinct_from_reserved_gpu_memory():
    assert model_memory_gib("Model loading took 0.945 GiB memory and 3 seconds") == 0.945
    assert model_memory_gib("GPU KV cache size: 128,000 tokens") is None
