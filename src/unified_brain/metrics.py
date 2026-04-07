"""Lightweight Prometheus metrics — no external dependencies.

Exposes counters and gauges in Prometheus text exposition format.
Thread-safe via a simple lock. Designed for the health endpoint at /metrics.
"""

import threading
import time


class _Metric:
    """Base metric with labels support."""

    def __init__(self, name: str, help_text: str, metric_type: str):
        self.name = name
        self.help_text = help_text
        self.metric_type = metric_type
        self._values: dict[tuple, float] = {}
        self._lock = threading.Lock()

    def _key(self, labels: dict) -> tuple:
        return tuple(sorted(labels.items())) if labels else ()

    def expose(self) -> str:
        lines = [
            f"# HELP {self.name} {self.help_text}",
            f"# TYPE {self.name} {self.metric_type}",
        ]
        with self._lock:
            for label_key, value in sorted(self._values.items()):
                label_str = ""
                if label_key:
                    pairs = ",".join(f'{k}="{v}"' for k, v in label_key)
                    label_str = f"{{{pairs}}}"
                # Format: integer if whole, else float
                val_str = str(int(value)) if value == int(value) else f"{value:.6f}"
                lines.append(f"{self.name}{label_str} {val_str}")
        return "\n".join(lines)


class Counter(_Metric):
    """Monotonically increasing counter."""

    def __init__(self, name: str, help_text: str):
        super().__init__(name, help_text, "counter")

    def inc(self, amount: float = 1.0, **labels):
        key = self._key(labels)
        with self._lock:
            self._values[key] = self._values.get(key, 0.0) + amount

    def get(self, **labels) -> float:
        key = self._key(labels)
        with self._lock:
            return self._values.get(key, 0.0)


class Gauge(_Metric):
    """Value that can go up and down."""

    def __init__(self, name: str, help_text: str):
        super().__init__(name, help_text, "gauge")

    def set(self, value: float, **labels):
        key = self._key(labels)
        with self._lock:
            self._values[key] = value

    def inc(self, amount: float = 1.0, **labels):
        key = self._key(labels)
        with self._lock:
            self._values[key] = self._values.get(key, 0.0) + amount

    def get(self, **labels) -> float:
        key = self._key(labels)
        with self._lock:
            return self._values.get(key, 0.0)


class MetricsRegistry:
    """Central registry for all metrics. Singleton."""

    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._metrics = []
            cls._instance._start_time = time.time()
        return cls._instance

    def register(self, metric: _Metric) -> _Metric:
        self._metrics.append(metric)
        return metric

    def expose(self) -> str:
        """Return all metrics in Prometheus text exposition format."""
        blocks = [m.expose() for m in self._metrics if m._values]
        # Add process uptime
        uptime = time.time() - self._start_time
        blocks.append(
            f"# HELP brain_uptime_seconds Time since service start\n"
            f"# TYPE brain_uptime_seconds gauge\n"
            f"brain_uptime_seconds {uptime:.1f}"
        )
        return "\n".join(blocks) + "\n"

    @classmethod
    def reset(cls):
        """Reset singleton — for testing only."""
        cls._instance = None


# Global metrics — import and use directly
registry = MetricsRegistry()

events_ingested = registry.register(
    Counter("brain_events_ingested_total", "Total events ingested by adapter")
)

events_processed = registry.register(
    Counter("brain_events_processed_total", "Total events processed by brain")
)

brain_decisions = registry.register(
    Counter("brain_decisions_total", "Brain action decisions by type")
)

dispatch_results = registry.register(
    Counter("brain_dispatch_results_total", "Dispatch outcomes (success/failure)")
)

respond_results = registry.register(
    Counter("brain_respond_results_total", "Active respond outcomes")
)

cycle_duration = registry.register(
    Gauge("brain_cycle_duration_seconds", "Duration of last poll-analyze-dispatch cycle")
)

cycle_count = registry.register(
    Counter("brain_cycles_total", "Total service cycles completed")
)

adapter_errors = registry.register(
    Counter("brain_adapter_errors_total", "Adapter poll errors by adapter")
)

brain_errors = registry.register(
    Counter("brain_processing_errors_total", "Brain processing errors")
)

llm_calls_total = registry.register(
    Counter("brain_llm_calls_total", "Total LLM calls by backend and outcome")
)

llm_active = registry.register(
    Gauge("brain_llm_active_calls", "Currently running LLM calls")
)

llm_duration = registry.register(
    Gauge("brain_llm_last_duration_seconds", "Duration of last LLM call by backend")
)
