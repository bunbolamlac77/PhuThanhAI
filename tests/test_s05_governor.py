"""S05: governor reacts to rising swap, not only a static ratio."""

from __future__ import annotations

import unittest
from unittest import mock

from phuthanhai.runtime import resource as resource_module


class FakeSnapshot:
    def __init__(self, swap: int) -> None:
        self.swap_used_bytes = swap
        self.level = "pressured"
        self.available_bytes = 4 * 1024**3
        self.disk_free_bytes = 100 * 1024**3


class SwapRateTests(unittest.TestCase):
    def test_rising_swap_defers_heavy_work(self) -> None:
        governor = resource_module.ResourceGovernor()
        with mock.patch.object(resource_module, "snapshot", return_value=FakeSnapshot(1 * 1024**3)), \
             mock.patch.object(resource_module.time, "monotonic", return_value=100.0):
            self.assertEqual(governor.can_start_heavy_worker()[1], "MEMORY_PRESSURED")
        with mock.patch.object(resource_module, "snapshot", return_value=FakeSnapshot(2 * 1024**3)), \
             mock.patch.object(resource_module.time, "monotonic", return_value=130.0):
            allowed, code = governor.can_start_heavy_worker()
        self.assertFalse(allowed)
        self.assertEqual(code, "MEMORY_PRESSURE_RISING")
        self.assertGreater(governor.last_swap_rate_bytes_per_min, 512 * 1024**2)


if __name__ == "__main__":
    unittest.main()
