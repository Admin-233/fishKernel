from fishkernel.events import Event, read_events, clear_parse_cache
from fishkernel.tokens import fold_usage
from fishkernel.usage import sync_usage, usage_events


def test_usage_without_prices_is_unknown():
    totals = fold_usage([Event(1, "", "assistant/message", {"usage": {"prompt_tokens": 100, "completion_tokens": 20}})])
    assert totals.calls == 1 and totals.prompt_tokens == 100 and totals.output_tokens == 20
    assert totals.cost_yuan is None and "未知" in totals.summary()


def test_recorded_prices_and_cache_are_used():
    totals = fold_usage([Event(1, "", "llm/usage", {
        "call_id": "call", "usage": {"prompt_tokens": 1000000, "prompt_cache_hit_tokens": 900000,
                                        "prompt_cache_miss_tokens": 100000, "completion_tokens": 100000},
        "prices": {"input": 4, "cache_input": 0.5, "output": 12, "currency": "CNY"}})])
    assert abs(totals.cost_yuan - 2.05) < 1e-9


def test_missing_cache_data_does_not_invent_hit_rate():
    totals = fold_usage([Event(1, "", "llm/usage", {
        "usage": {"prompt_tokens": 1000000, "completion_tokens": 1000},
        "prices": {"input": 4, "cache_input": 0.5, "output": 12}})])
    assert totals.cost_yuan is None and totals.calls_without_cache_data == 1


def test_usage_is_not_double_counted_and_survives_context_trim(archive, log):
    log.append("assistant/message", usage={"prompt_tokens": 100})
    log.append("llm/usage", call_id="new", usage={"prompt_tokens": 200})
    reply = log.append("assistant/message", usage_call_id="new", usage={"prompt_tokens": 200})
    sync_usage(archive)
    sync_usage(archive)
    assert len(read_events(archive.runtime_dir / "usage.jsonl")) == 2
    archive.events_path.write_text(reply.to_json() + "\n", encoding="utf-8")
    clear_parse_cache(archive.events_path)
    totals = fold_usage(usage_events(archive))
    assert totals.calls == 2 and totals.prompt_tokens == 300


def test_usage_partial_append_is_recovered_from_event_source(archive, log):
    record = log.append("llm/usage", call_id="one", usage={"prompt_tokens":100})
    path = archive.runtime_dir / "usage.jsonl"
    path.write_bytes(record.to_json().encode()[:20])
    sync_usage(archive)
    sync_usage(archive)
    assert read_events(path) == [record]


def test_response_without_usage_is_counted_with_unknown_cost():
    totals = fold_usage([Event(1, "", "llm/usage", {"call_id":"one", "usage":{}})])
    assert totals.calls == 1 and totals.cost_yuan is None
