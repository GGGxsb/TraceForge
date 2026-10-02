"""Multi-file coding fixtures with hidden behavioral checks and reference fixes."""

from __future__ import annotations

from textwrap import dedent

from code_tasks import CodeTask


def s(value: str) -> str:
    return dedent(value).lstrip()


COMPLEX_TASKS = (
    CodeTask(
        id="config_precedence",
        suite="complex",
        prompt=("修复配置加载：TF_TIMEOUT、TF_RETRIES、TF_ENABLED 环境值优先于文件值，文件值优先于默认值。"
                "timeout 必须是正整数，retries 必须是非负整数；enabled 接受 true/false、yes/no、1/0（不区分大小写）。"
                "无效值抛 ValueError，不修改输入字典。worker_settings 必须返回同一套最终配置。运行现有测试。"),
        files={
            "config.py": s("""
                DEFAULTS = {"timeout": 10, "retries": 2, "enabled": False}

                def load_config(file_values=None, env=None):
                    result = DEFAULTS.copy()
                    result.update(file_values or {})
                    return result
            """),
            "worker.py": s("""
                from config import load_config

                def worker_settings(file_values=None, env=None):
                    config = load_config(file_values, env)
                    config["retries"] = 0
                    return config
            """),
            "test_public.py": s("""
                import unittest
                from config import load_config
                from worker import worker_settings

                class PublicTests(unittest.TestCase):
                    def test_defaults(self):
                        self.assertEqual(load_config()["timeout"], 10)
                    def test_worker(self):
                        self.assertEqual(worker_settings()["enabled"], False)
            """),
        },
        reference_files={
            "config.py": s("""
                DEFAULTS = {"timeout": 10, "retries": 2, "enabled": False}
                ENV_KEYS = {"timeout": "TF_TIMEOUT", "retries": "TF_RETRIES", "enabled": "TF_ENABLED"}

                def _integer(value, name, minimum):
                    if isinstance(value, bool):
                        raise ValueError(name)
                    try:
                        result = int(value)
                    except (TypeError, ValueError) as exc:
                        raise ValueError(name) from exc
                    if str(value).strip() != str(result) or result < minimum:
                        raise ValueError(name)
                    return result

                def _boolean(value):
                    if isinstance(value, bool):
                        return value
                    normalized = str(value).strip().lower()
                    if normalized in {"true", "yes", "1"}:
                        return True
                    if normalized in {"false", "no", "0"}:
                        return False
                    raise ValueError("enabled")

                def load_config(file_values=None, env=None):
                    file_values = file_values or {}
                    env = env or {}
                    result = DEFAULTS.copy()
                    for key in DEFAULTS:
                        if key in file_values:
                            result[key] = file_values[key]
                        if ENV_KEYS[key] in env:
                            result[key] = env[ENV_KEYS[key]]
                    result["timeout"] = _integer(result["timeout"], "timeout", 1)
                    result["retries"] = _integer(result["retries"], "retries", 0)
                    result["enabled"] = _boolean(result["enabled"])
                    return result
            """),
            "worker.py": s("""
                from config import load_config

                def worker_settings(file_values=None, env=None):
                    return load_config(file_values, env)
            """),
        },
        hidden_test=s("""
            import unittest
            from config import load_config
            from worker import worker_settings

            class HiddenTests(unittest.TestCase):
                def test_precedence(self):
                    self.assertEqual(load_config({"timeout": 20, "retries": 4},
                        {"TF_TIMEOUT": "30", "TF_ENABLED": "YES"}),
                        {"timeout": 30, "retries": 4, "enabled": True})
                def test_worker_uses_same_config(self):
                    self.assertEqual(worker_settings({"retries": 7}, {"TF_RETRIES": "3"})["retries"], 3)
                def test_inputs_unchanged(self):
                    file_values = {"timeout": 17}
                    env = {"TF_TIMEOUT": "19"}
                    load_config(file_values, env)
                    self.assertEqual(file_values, {"timeout": 17})
                    self.assertEqual(env, {"TF_TIMEOUT": "19"})
                def test_invalid(self):
                    for values, env in [({"timeout": 0}, {}), ({"retries": -1}, {}),
                                        ({}, {"TF_TIMEOUT": "bad"}), ({}, {"TF_ENABLED": "perhaps"})]:
                        with self.subTest(values=values, env=env), self.assertRaises(ValueError):
                            load_config(values, env)
                def test_boolean_variants(self):
                    self.assertFalse(load_config({"enabled": "NO"})["enabled"])
                    self.assertTrue(load_config({}, {"TF_ENABLED": "1"})["enabled"])
        """),
        expected_paths=("config.py", "worker.py"),
    ),
    CodeTask(
        id="stable_cursor_pagination",
        suite="complex",
        prompt=("修复列表分页。记录按 created_at 降序、相同时间按 id 降序排序；游标应指向上一页最后一条，"
                "下一页不能重复或漏掉同时间记录。limit 必须在 1..50，非法游标抛 ValueError，"
                "最后一页返回 None 游标。不修改输入记录。运行现有测试。"),
        files={
            "records.py": s("""
                def ordered(records):
                    return sorted(records, key=lambda row: row["created_at"], reverse=True)
            """),
            "api.py": s("""
                from records import ordered

                def list_page(records, limit=2, cursor=None):
                    rows = ordered(records)
                    offset = int(cursor or 0)
                    selected = rows[offset:offset + limit]
                    return selected, str(offset + limit) if selected else None
            """),
            "test_public.py": s("""
                import unittest
                from api import list_page

                class PublicTests(unittest.TestCase):
                    def test_first_page(self):
                        rows = [{"id": "b", "created_at": 2}, {"id": "a", "created_at": 1}]
                        self.assertEqual(list_page(rows, 1)[0][0]["id"], "b")
            """),
        },
        reference_files={
            "records.py": s("""
                def ordered(records):
                    return sorted(records, key=lambda row: (row["created_at"], row["id"]), reverse=True)
            """),
            "api.py": s("""
                import json
                from records import ordered

                def list_page(records, limit=2, cursor=None):
                    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 50:
                        raise ValueError("limit")
                    rows = ordered(records)
                    if cursor is not None:
                        try:
                            marker = json.loads(cursor)
                            if not isinstance(marker, list) or len(marker) != 2:
                                raise ValueError("cursor")
                            rows = [row for row in rows if (row["created_at"], row["id"]) <
                                    (marker[0], marker[1])]
                        except (TypeError, ValueError) as exc:
                            raise ValueError("cursor") from exc
                    selected = rows[:limit]
                    next_cursor = None
                    if len(rows) > limit:
                        last = selected[-1]
                        next_cursor = json.dumps([last["created_at"], last["id"]])
                    return selected, next_cursor
            """),
        },
        hidden_test=s("""
            import unittest
            from api import list_page
            from records import ordered

            class HiddenTests(unittest.TestCase):
                def setUp(self):
                    self.rows = [{"id": key, "created_at": stamp} for key, stamp in
                                 [("a", 2), ("c", 2), ("b", 2), ("z", 3), ("d", 1)]]
                def test_stable_pages(self):
                    collected, cursor = [], None
                    while True:
                        page, cursor = list_page(self.rows, 2, cursor)
                        collected += [row["id"] for row in page]
                        if cursor is None:
                            break
                    self.assertEqual(collected, ["z", "c", "b", "a", "d"])
                    self.assertEqual(len(set(collected)), 5)
                def test_invalid(self):
                    for value in [0, 51, "2", True]:
                        with self.subTest(value=value), self.assertRaises(ValueError):
                            list_page(self.rows, value)
                    with self.assertRaises(ValueError):
                        list_page(self.rows, 2, "bad")
                def test_input_not_mutated(self):
                    before = list(self.rows)
                    ordered(self.rows)
                    list_page(self.rows, 2)
                    self.assertEqual(self.rows, before)
                def test_empty(self):
                    self.assertEqual(list_page([]), ([], None))
        """),
        expected_paths=("records.py", "api.py"),
    ),
    CodeTask(
        id="atomic_csv_import",
        suite="complex",
        prompt=("修复 CSV 导入。CSV 含 id,name,quantity 三列；quantity 为非负整数，id 和 name 非空。"
                "整批先验证后写入，任何错误都不能部分修改 Store；同批重复 id 要报错。"
                "再次导入完全相同的已有记录应跳过，已有 id 内容冲突应报错。返回新插入的数量。运行现有测试。"),
        files={
            "store.py": s("""
                class Store:
                    def __init__(self, rows=None):
                        self.rows = dict(rows or {})

                    def add(self, row):
                        self.rows[row["id"]] = row
            """),
            "importer.py": s("""
                import csv
                from io import StringIO

                def import_csv(store, text):
                    count = 0
                    for row in csv.DictReader(StringIO(text)):
                        store.add(row)
                        count += 1
                    return count
            """),
            "test_public.py": s(r"""
                import unittest
                from store import Store
                from importer import import_csv

                class PublicTests(unittest.TestCase):
                    def test_one(self):
                        store = Store()
                        self.assertEqual(import_csv(store, "id,name,quantity\na,Apple,2\n"), 1)
                        self.assertIn("a", store.rows)
            """),
        },
        reference_files={
            "store.py": s("""
                class Store:
                    def __init__(self, rows=None):
                        self.rows = dict(rows or {})

                    def add(self, row):
                        identifier = row["id"]
                        if identifier in self.rows and self.rows[identifier] != row:
                            raise ValueError("conflicting id")
                        if identifier in self.rows:
                            return False
                        self.rows[identifier] = row
                        return True
            """),
            "importer.py": s("""
                import csv
                from io import StringIO

                def import_csv(store, text):
                    reader = csv.DictReader(StringIO(text))
                    if reader.fieldnames != ["id", "name", "quantity"]:
                        raise ValueError("columns")
                    pending = {}
                    for row in reader:
                        if None in row or not row["id"] or not row["name"]:
                            raise ValueError("row")
                        raw = row["quantity"]
                        if raw is None or not raw.isdecimal():
                            raise ValueError("quantity")
                        parsed = {"id": row["id"], "name": row["name"], "quantity": int(raw)}
                        if parsed["id"] in pending:
                            raise ValueError("duplicate id")
                        if parsed["id"] in store.rows and store.rows[parsed["id"]] != parsed:
                            raise ValueError("conflicting id")
                        pending[parsed["id"]] = parsed
                    inserted = 0
                    for row in pending.values():
                        inserted += store.add(row)
                    return inserted
            """),
        },
        hidden_test=s(r"""
            import unittest
            from store import Store
            from importer import import_csv

            class HiddenTests(unittest.TestCase):
                def test_atomic_on_invalid_later_row(self):
                    store = Store()
                    with self.assertRaises(ValueError):
                        import_csv(store, "id,name,quantity\na,Apple,2\nb,Banana,-1\n")
                    self.assertEqual(store.rows, {})
                def test_idempotent(self):
                    store = Store()
                    text = "id,name,quantity\na,Apple,2\nb,Banana,0\n"
                    self.assertEqual(import_csv(store, text), 2)
                    self.assertEqual(import_csv(store, text), 0)
                    self.assertEqual(store.rows["a"]["quantity"], 2)
                def test_conflict_is_atomic(self):
                    store = Store({"x": {"id": "x", "name": "Existing", "quantity": 1}})
                    with self.assertRaises(ValueError):
                        import_csv(store, "id,name,quantity\ny,New,3\nx,Other,1\n")
                    self.assertEqual(list(store.rows), ["x"])
                def test_duplicate_and_missing(self):
                    for text in ["id,name,quantity\na,A,1\na,B,2\n",
                                 "id,name,quantity\na,,1\n", "id,name,quantity\na,A,\n"]:
                        with self.subTest(text=text), self.assertRaises(ValueError):
                            import_csv(Store(), text)
        """),
        expected_paths=("store.py", "importer.py"),
    ),
    CodeTask(
        id="dependency_build_order",
        suite="complex",
        prompt=("修复依赖构建顺序：graph.topological_order 应按依赖优先返回所有节点，"
                "结果在同等可行顺序下按名称稳定排序；缺失依赖和环都抛 ValueError。"
                "build.plan_targets 只返回目标及其传递依赖，未知目标抛 ValueError。运行现有测试。"),
        files={
            "graph.py": s("""
                def topological_order(graph):
                    return list(graph)
            """),
            "build.py": s("""
                from graph import topological_order

                def plan_targets(graph, targets):
                    return topological_order(graph)
            """),
            "test_public.py": s("""
                import unittest
                from build import plan_targets

                class PublicTests(unittest.TestCase):
                    def test_empty(self):
                        self.assertEqual(plan_targets({}, []), [])
            """),
        },
        reference_files={
            "graph.py": s("""
                def topological_order(graph):
                    visiting, visited, result = set(), set(), []
                    def visit(name):
                        if name not in graph:
                            raise ValueError("missing dependency")
                        if name in visiting:
                            raise ValueError("cycle")
                        if name in visited:
                            return
                        visiting.add(name)
                        for dep in sorted(graph[name]):
                            visit(dep)
                        visiting.remove(name)
                        visited.add(name)
                        result.append(name)
                    for name in sorted(graph):
                        visit(name)
                    return result
            """),
            "build.py": s("""
                from graph import topological_order

                def plan_targets(graph, targets):
                    selected = set()
                    def include(name):
                        if name not in graph:
                            raise ValueError("unknown target or dependency")
                        if name in selected:
                            return
                        selected.add(name)
                        for dep in graph[name]:
                            include(dep)
                    order = topological_order(graph)
                    for name in targets:
                        include(name)
                    return [name for name in order if name in selected]
            """),
        },
        hidden_test=s("""
            import unittest
            from graph import topological_order
            from build import plan_targets

            class HiddenTests(unittest.TestCase):
                def test_transitive(self):
                    graph = {"app": ["core", "ui"], "ui": ["core"], "core": [], "misc": []}
                    order = topological_order(graph)
                    self.assertEqual(set(order), set(graph))
                    self.assertEqual(len(order), len(graph))
                    for name, dependencies in graph.items():
                        for dependency in dependencies:
                            self.assertLess(order.index(dependency), order.index(name))
                    self.assertEqual(order, topological_order(dict(reversed(list(graph.items())))))
                    self.assertEqual(plan_targets(graph, ["app"]), ["core", "ui", "app"])
                def test_missing(self):
                    with self.assertRaises(ValueError):
                        topological_order({"a": ["missing"]})
                    with self.assertRaises(ValueError):
                        plan_targets({"a": []}, ["absent"])
                def test_cycle(self):
                    with self.assertRaises(ValueError):
                        topological_order({"a": ["b"], "b": ["a"]})
                def test_deterministic(self):
                    self.assertEqual(topological_order({"z": [], "a": [], "m": []}), ["a", "m", "z"])
                def test_no_mutation(self):
                    graph = {"a": ["b"], "b": []}
                    plan_targets(graph, ["a"])
                    self.assertEqual(graph, {"a": ["b"], "b": []})
        """),
        expected_paths=("graph.py", "build.py"),
    ),
    CodeTask(
        id="retry_with_idempotency",
        suite="complex",
        prompt=("修复带幂等键的提交重试。只对状态 429、503 的 TransientError 重试，"
                "最多 max_attempts 次（含第一次）；其他错误立即传播。每次尝试使用相同 request_id，"
                "两次重试前分别调用注入的 sleep(0.1)、sleep(0.2)；max_attempts 小于 1 抛 ValueError。运行现有测试。"),
        files={
            "transport.py": s("""
                class TransientError(Exception):
                    def __init__(self, status):
                        self.status = status
                        super().__init__(str(status))

                def retryable(error):
                    return isinstance(error, TransientError)
            """),
            "client.py": s("""
                def post_with_retry(send, payload, request_id, sleep, max_attempts=3):
                    return send(payload, request_id=request_id)
            """),
            "test_public.py": s("""
                import unittest
                from client import post_with_retry

                class PublicTests(unittest.TestCase):
                    def test_success(self):
                        self.assertEqual(post_with_retry(lambda payload, request_id: payload,
                            "ok", "key", lambda _: None), "ok")
            """),
        },
        reference_files={
            "transport.py": s("""
                class TransientError(Exception):
                    def __init__(self, status):
                        self.status = status
                        super().__init__(str(status))

                def retryable(error):
                    return isinstance(error, TransientError) and error.status in {429, 503}
            """),
            "client.py": s("""
                from transport import retryable

                def post_with_retry(send, payload, request_id, sleep, max_attempts=3):
                    if isinstance(max_attempts, bool) or not isinstance(max_attempts, int) or max_attempts < 1:
                        raise ValueError("max_attempts")
                    for attempt in range(max_attempts):
                        try:
                            return send(payload, request_id=request_id)
                        except Exception as exc:
                            if not retryable(exc) or attempt == max_attempts - 1:
                                raise
                            sleep(0.1 * (2 ** attempt))
            """),
        },
        hidden_test=s("""
            import unittest
            from client import post_with_retry
            from transport import TransientError

            class HiddenTests(unittest.TestCase):
                def test_retry_and_key(self):
                    calls, delays = [], []
                    def send(payload, request_id):
                        calls.append((payload, request_id))
                        if len(calls) < 3:
                            raise TransientError(503)
                        return "done"
                    self.assertEqual(post_with_retry(send, {"a": 1}, "same-key", delays.append), "done")
                    self.assertEqual(calls, [({"a": 1}, "same-key")] * 3)
                    self.assertEqual(delays, [0.1, 0.2])
                def test_no_retry_on_other_status(self):
                    calls = []
                    def send(payload, request_id):
                        calls.append(1)
                        raise TransientError(400)
                    with self.assertRaises(TransientError):
                        post_with_retry(send, None, "key", lambda _: None)
                    self.assertEqual(len(calls), 1)
                def test_exhaustion(self):
                    calls = []
                    def send(payload, request_id):
                        calls.append(1)
                        raise TransientError(429)
                    with self.assertRaises(TransientError):
                        post_with_retry(send, None, "key", lambda _: None, max_attempts=2)
                    self.assertEqual(len(calls), 2)
                def test_invalid_attempts(self):
                    with self.assertRaises(ValueError):
                        post_with_retry(lambda *_: None, None, "key", lambda _: None, max_attempts=0)
        """),
        expected_paths=("transport.py", "client.py"),
    ),
    CodeTask(
        id="cache_ttl_invalidation",
        suite="complex",
        prompt=("修复用户缓存。TTLCache 使用注入的 clock，在过期时重新读取；ttl=0 不能复用旧值，"
                "ttl<0 抛 ValueError。loader 抛异常时不能缓存异常。UserService.update 写入成功后"
                "必须让该用户缓存失效；不同用户的缓存不互相影响。运行现有测试。"),
        files={
            "cache.py": s("""
                class TTLCache:
                    def __init__(self, ttl, clock):
                        self.ttl = ttl
                        self.clock = clock
                        self.values = {}

                    def get(self, key, loader):
                        if key not in self.values:
                            self.values[key] = loader()
                        return self.values[key]

                    def invalidate(self, key):
                        pass
            """),
            "users.py": s("""
                class UserService:
                    def __init__(self, cache, read, write):
                        self.cache = cache
                        self.read = read
                        self.write = write

                    def get(self, user_id):
                        return self.cache.get(user_id, lambda: self.read(user_id))

                    def update(self, user_id, value):
                        return self.write(user_id, value)
            """),
            "test_public.py": s("""
                import unittest
                from cache import TTLCache

                class PublicTests(unittest.TestCase):
                    def test_cached(self):
                        cache = TTLCache(10, lambda: 0)
                        self.assertEqual(cache.get("x", lambda: 3), 3)
                        self.assertEqual(cache.get("x", lambda: 4), 3)
            """),
        },
        reference_files={
            "cache.py": s("""
                class TTLCache:
                    def __init__(self, ttl, clock):
                        if ttl < 0:
                            raise ValueError("ttl")
                        self.ttl = ttl
                        self.clock = clock
                        self.values = {}

                    def get(self, key, loader):
                        now = self.clock()
                        cached = self.values.get(key)
                        if cached is not None and now < cached[0]:
                            return cached[1]
                        value = loader()
                        self.values[key] = (now + self.ttl, value)
                        return value

                    def invalidate(self, key):
                        self.values.pop(key, None)
            """),
            "users.py": s("""
                class UserService:
                    def __init__(self, cache, read, write):
                        self.cache = cache
                        self.read = read
                        self.write = write

                    def get(self, user_id):
                        return self.cache.get(user_id, lambda: self.read(user_id))

                    def update(self, user_id, value):
                        result = self.write(user_id, value)
                        self.cache.invalidate(user_id)
                        return result
            """),
        },
        hidden_test=s("""
            import unittest
            from cache import TTLCache
            from users import UserService

            class HiddenTests(unittest.TestCase):
                def test_expiry(self):
                    now = [0]
                    cache = TTLCache(5, lambda: now[0])
                    self.assertEqual(cache.get("x", lambda: 1), 1)
                    now[0] = 4
                    self.assertEqual(cache.get("x", lambda: 2), 1)
                    now[0] = 5
                    self.assertEqual(cache.get("x", lambda: 3), 3)
                def test_zero_and_invalid(self):
                    cache = TTLCache(0, lambda: 0)
                    self.assertEqual(cache.get("x", lambda: 1), 1)
                    self.assertEqual(cache.get("x", lambda: 2), 2)
                    with self.assertRaises(ValueError):
                        TTLCache(-1, lambda: 0)
                def test_loader_error_not_cached(self):
                    cache = TTLCache(10, lambda: 0)
                    def fail():
                        raise RuntimeError("read")
                    with self.assertRaises(RuntimeError):
                        cache.get("x", fail)
                    self.assertEqual(cache.get("x", lambda: 7), 7)
                def test_update_invalidation(self):
                    source = {"a": 1, "b": 2}
                    cache = TTLCache(100, lambda: 0)
                    service = UserService(cache, source.__getitem__, source.__setitem__)
                    self.assertEqual(service.get("a"), 1)
                    self.assertEqual(service.get("b"), 2)
                    service.update("a", 9)
                    self.assertEqual(service.get("a"), 9)
                    self.assertEqual(service.get("b"), 2)
        """),
        expected_paths=("cache.py", "users.py"),
    ),
    CodeTask(
        id="atomic_checkout",
        suite="complex",
        prompt=("修复订单结算。购物车是 (sku, quantity) 列表；quantity 必须是正整数且 bool 不算整数，"
                "相同 SKU 要合并数量。价格用 Decimal 精确计算并返回两位小数。"
                "任一 SKU 缺价、缺库存或库存不足时抛 ValueError，库存完全不变；"
                "成功时一次性扣减所有库存。不修改购物车输入。运行现有测试。"),
        files={
            "pricing.py": s("""
                def cart_total(cart, prices):
                    return sum(float(prices[sku]) * quantity for sku, quantity in cart)
            """),
            "inventory.py": s("""
                class Inventory:
                    def __init__(self, stock):
                        self.stock = dict(stock)

                    def reserve(self, sku, quantity):
                        self.stock[sku] -= quantity
                        if self.stock[sku] < 0:
                            raise ValueError("out of stock")
            """),
            "checkout.py": s("""
                from pricing import cart_total

                def checkout(cart, prices, inventory):
                    for sku, quantity in cart:
                        inventory.reserve(sku, quantity)
                    return cart_total(cart, prices)
            """),
            "test_public.py": s("""
                import unittest
                from inventory import Inventory
                from checkout import checkout

                class PublicTests(unittest.TestCase):
                    def test_simple(self):
                        stock = Inventory({"a": 3})
                        self.assertEqual(checkout([("a", 1)], {"a": "2.00"}, stock), 2)
                        self.assertEqual(stock.stock["a"], 2)
            """),
        },
        reference_files={
            "pricing.py": s("""
                from decimal import Decimal

                def cart_total(cart, prices):
                    total = Decimal("0")
                    for sku, quantity in cart:
                        if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity <= 0:
                            raise ValueError("quantity")
                        if sku not in prices:
                            raise ValueError("missing price")
                        total += Decimal(str(prices[sku])) * quantity
                    return total.quantize(Decimal("0.01"))
            """),
            "inventory.py": s("""
                class Inventory:
                    def __init__(self, stock):
                        self.stock = dict(stock)

                    def reserve_many(self, quantities):
                        for sku, quantity in quantities.items():
                            if sku not in self.stock or self.stock[sku] < quantity:
                                raise ValueError("out of stock")
                        for sku, quantity in quantities.items():
                            self.stock[sku] -= quantity
            """),
            "checkout.py": s("""
                from pricing import cart_total

                def checkout(cart, prices, inventory):
                    total = cart_total(cart, prices)
                    quantities = {}
                    for sku, quantity in cart:
                        quantities[sku] = quantities.get(sku, 0) + quantity
                    inventory.reserve_many(quantities)
                    return total
            """),
        },
        hidden_test=s("""
            import unittest
            from decimal import Decimal
            from inventory import Inventory
            from checkout import checkout

            class HiddenTests(unittest.TestCase):
                def test_decimal_and_duplicates(self):
                    stock = Inventory({"a": 3, "b": 4})
                    cart = [("a", 1), ("b", 2), ("a", 2)]
                    self.assertEqual(checkout(cart, {"a": "0.10", "b": "0.20"}, stock),
                                     Decimal("0.70"))
                    self.assertEqual(stock.stock, {"a": 0, "b": 2})
                    self.assertEqual(cart, [("a", 1), ("b", 2), ("a", 2)])
                def test_atomic_insufficient(self):
                    stock = Inventory({"a": 3, "b": 1})
                    with self.assertRaises(ValueError):
                        checkout([("a", 1), ("b", 2)], {"a": "1", "b": "2"}, stock)
                    self.assertEqual(stock.stock, {"a": 3, "b": 1})
                def test_atomic_missing_price(self):
                    stock = Inventory({"a": 3, "b": 3})
                    with self.assertRaises(ValueError):
                        checkout([("a", 1), ("b", 1)], {"a": "1"}, stock)
                    self.assertEqual(stock.stock, {"a": 3, "b": 3})
                def test_invalid_quantity_and_unknown_sku(self):
                    for cart in [[("a", 0)], [("a", True)], [("missing", 1)]]:
                        stock = Inventory({"a": 2})
                        with self.subTest(cart=cart), self.assertRaises(ValueError):
                            checkout(cart, {"a": "1", "missing": "1"}, stock)
                        self.assertEqual(stock.stock, {"a": 2})
        """),
        expected_paths=("pricing.py", "inventory.py", "checkout.py"),
    ),
)
