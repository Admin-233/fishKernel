"""Keep the owner thread responsive while a synchronous model request runs."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import time
import uuid


class CoordinatedClient:
    def __init__(self, client, service, events, config):
        self.client = client
        self.service = service
        self.events = events
        self.config = config

    def complete(self, messages, **kwargs):
        # A request is never cancelled by new user input. The worker never writes events.
        with ThreadPoolExecutor(max_workers=1, thread_name_prefix="fish-llm") as pool:
            future = pool.submit(self.client.complete, messages, **kwargs)
            while not future.done():
                self.service()
                time.sleep(0.05)
            completion = future.result()
        call_id = uuid.uuid4().hex
        self.events.append("llm/usage", call_id=call_id, model=completion.model or self.config.model,
                           usage=completion.usage, prices=self.config.options.get("prices"),
                           thinking=self.config.thinking, reasoning_effort=self.config.effort)
        completion.usage_call_id = call_id
        return completion
