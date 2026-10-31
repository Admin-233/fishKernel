"""Copy this directory to <archive>/Plugins/demo and enable it explicitly."""
import threading
import time
from fishkernel.plugin_sdk import Plugin


def echo(arguments):
    return {"text": arguments["text"]}


def wait(arguments):
    time.sleep(arguments["seconds"])
    return {"waited": arguments["seconds"]}


def start(plugin):
    interval = plugin.config.get("input_after_seconds")
    if isinstance(interval, (int, float)) and 0 < interval <= 3600:
        def notify():
            time.sleep(interval)
            plugin.input("Demo timer completed", external_id="demo-timer-once")
        threading.Thread(target=notify, daemon=True).start()


if __name__ == "__main__":
    Plugin({"echo": echo, "wait": wait}, on_start=start).run()
