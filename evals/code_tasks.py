"""Small, isolated coding tasks. Hidden checks are never copied into the agent workspace until it stops."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CodeTask:
    id: str
    prompt: str
    files: dict[str, str]
    hidden_test: str | None = None
    answer_contains: str | None = None
    expected_paths: tuple[str, ...] = ()
    suite: str = "smoke"
    reference_files: dict[str, str] | None = None


TASKS = (
    CodeTask(
        id="median_even",
        prompt="修复 stats.py 中 median 对偶数长度列表的计算：返回两个中间数的平均值。保留空列表抛 ValueError 的行为，并运行现有测试。",
        files={
            "stats.py": (
                "def median(values):\n"
                "    if not values:\n        raise ValueError('empty values')\n"
                "    ordered = sorted(values)\n    return ordered[len(ordered) // 2]\n"
            ),
            "test_stats.py": (
                "import unittest\nfrom stats import median\n\n"
                "class StatsTests(unittest.TestCase):\n"
                "    def test_odd(self):\n        self.assertEqual(median([9, 1, 3]), 3)\n"
                "    def test_empty(self):\n"
                "        with self.assertRaises(ValueError):\n            median([])\n"
            ),
        },
        hidden_test=(
            "import unittest\nfrom stats import median\n\n"
            "class HiddenTests(unittest.TestCase):\n"
            "    def test_even(self):\n        self.assertEqual(median([8, 2, 6, 4]), 5)\n"
            "    def test_negative(self):\n        self.assertEqual(median([-9, -5]), -7)\n"
            "    def test_does_not_mutate(self):\n"
            "        data = [4, 1, 3, 2]\n        median(data)\n"
            "        self.assertEqual(data, [4, 1, 3, 2])\n"
        ),
        expected_paths=("stats.py", "test_stats.py"),
    ),
    CodeTask(
        id="clamp_feature",
        prompt=(
            "在 arithmetic.py 新增 clamp(value, lower, upper)：值低于 lower 返回 lower，高于 upper 返回 upper，"
            "否则返回原值；当 lower > upper 时抛 ValueError。保留现有 add 行为，并运行测试。"
        ),
        files={
            "arithmetic.py": "def add(left, right):\n    return left + right\n",
            "test_numbers.py": (
                "import unittest\nfrom arithmetic import add\n\n"
                "class NumberTests(unittest.TestCase):\n"
                "    def test_add(self):\n        self.assertEqual(add(2, 3), 5)\n"
            ),
        },
        hidden_test=(
            "import unittest\nfrom arithmetic import add, clamp\n\n"
            "class HiddenTests(unittest.TestCase):\n"
            "    def test_inside(self):\n        self.assertEqual(clamp(5, 1, 10), 5)\n"
            "    def test_bounds(self):\n"
            "        self.assertEqual(clamp(-3, 0, 7), 0)\n"
            "        self.assertEqual(clamp(12, 0, 7), 7)\n"
            "    def test_equal_bounds(self):\n        self.assertEqual(clamp(3, 3, 3), 3)\n"
            "    def test_invalid(self):\n"
            "        with self.assertRaises(ValueError):\n            clamp(3, 9, 1)\n"
            "    def test_existing(self):\n        self.assertEqual(add(2, 3), 5)\n"
        ),
        expected_paths=("arithmetic.py", "test_numbers.py"),
    ),
    CodeTask(
        id="read_only_timeout",
        prompt="只读调查：settings.py 里 DEFAULT_TIMEOUT 的值是多少？不要修改任何文件。",
        files={"settings.py": "DEFAULT_TIMEOUT = 37\nMAX_RETRIES = 4\n"},
        answer_contains="37",
    ),
)
