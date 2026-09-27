#!/usr/bin/env python3
"""BON v1.0 使用示例 —— 对应 ``example/bon_example.cpp``。

演示内容：
  1. 用流式写入器构建一个嵌套对象
  2. 解析字节流并遍历所有节点
  3. 读取标量值（字符串、整数、浮点、布尔、小数、二进制）
  4. 通过键查找值
  5. 用迭代器遍历对象
  6. 输出 JSON 调试视图与十六进制转储
  7. 保存到文件并重新加载
  8. 深层嵌套（格式本身没有深度上限）
  9. dumps / loads 高层接口

运行：
    python bon_example.py
会在当前目录生成 ``example.bon``。

两层 API
--------
``bonlib`` 是 C 接口的逐函数移植：``bon_get_int_bytes(n, &p, &s, &neg)`` 在这里
就是 ``bonlib.get_int_bytes(n) -> (payload, negative)``，出参变成返回值，``size``
参数处处可选。``bonpy`` 是 C 没有的那一层：与 Python 对象互转，以及
``get_int`` / ``iter_items`` 之类的便捷函数。两者都是纯静态方法，所以
``bonlib.w_int64(w, v)`` 与 ``lib = bonlib(); lib.w_int64(w, v)`` 等价。

与 C 版示例的差异（都是 Python 语义的必然结果）
------------------------------------------------
* **没有 ``bon_unref()``**：C 用引用计数保活缓冲区，Python 靠对象本身。
  子节点共享同一个底层缓冲区，所以 ``del root`` 之后子节点依然可用 —— 这正是
  C 文档里“父先释放，子还能用”那条保证的 Python 写法。
* **错误用异常而不是错误码**：所有会失败的调用都抛 ``bonlib.BonError``，
  ``exc.code`` 就是 C 版的 ``BON_E_*``。
* **字符串直接给 ``str``**：``bonlib.get_string()`` 返回 Python 字符串，天然
  不可变且自带结尾符；需要 C 字符串语义时用 ``bonlib.get_cstr()`` /
  ``bonlib.key_cstr()``。
* **遍历用显式堆栈而非递归**：C 版用递归打印（``dump_node``），格式没有深度
  上限，所以这里改成显式栈，嵌套再深也不会爆栈。
"""

from __future__ import annotations

import os
import sys
import tempfile
from typing import Any

from bon import bonlib, bonpy

# 节点是不透明句柄：公开 API 只把它当窗口用，``bonlib.Bon`` 仅用于类型标注。
Node = bonlib.Bon


# ----------------------------------------------------------------------
# 辅助：打印缩进
# ----------------------------------------------------------------------
def indent(depth: int) -> None:
    for _ in range(depth):
        print("  ", end="")


# ----------------------------------------------------------------------
# 打印一个 BON 节点（显式堆栈版，可处理任意深度）
# ----------------------------------------------------------------------
def dump_node(node: Node, depth: int) -> None:
    """把整棵子树打印成缩进的文本。

    每一层只占一个栈帧里的表项，从不用 Python 递归，所以嵌套多深都安全。
    """
    # 栈项：[节点, 下一个子项下标, 子项总数, 对象里下一个该打印的是键还是值]
    stack = [[node, 0, bonlib.count(node), "value"]]

    while stack:
        frame = stack[-1]
        current = frame[0]
        index = frame[1]

        if index >= frame[2]:
            stack.pop()
            continue

        # 取出一个子节点：数组取元素，对象按键 / 值交替
        if bonlib.type(current) == bonlib.BON_OBJECT:
            if frame[3] == "key":
                child = bonlib.key_at(current, index)
                frame[3] = "value"
                print()
                indent(depth + 1)
                if bonlib.type(child) == bonlib.BON_STRING:
                    print('key "%s":' % bonlib.get_string(child))
                else:
                    print("key (%s, 非字符串):" % bonlib.type_name(child))
                stack.append([child, 0, bonlib.count(child), "value"])
                continue
            frame[3] = "key"
            frame[1] = index + 1
            child = bonlib.value_at(current, index)
        else:
            frame[1] = index + 1
            child = bonlib.at(current, index)

        kind = bonlib.type(child)
        indent(depth)
        print("[%s] size=%d" % (bonlib.type_name(child), bonlib.size(child)), end="")

        if kind == bonlib.BON_NONE:
            print(" (null)")
        elif kind == bonlib.BON_BOOL:
            print(" value=%s" % ("true" if bonlib.get_bool(child) else "false"))
        elif kind == bonlib.BON_NUMBER:
            # Python 的 int 是任意精度的：bonpy.get_int() 一次搞定，不需要像 C
            # 那样先试 int64、再试 uint64、最后退回十进制字符串。
            mode = bonlib.get_number_mode(child)
            print(" int=%d mode=%s" % (bonpy.get_int(child), bonpy.mode_name(mode)))
        elif kind == bonlib.BON_FLOAT:
            print(" double=%g" % bonlib.get_double(child))
        elif kind == bonlib.BON_STRING:
            # STRING/BLOB 的 TLV 头只有 type(1) + length(8) = 9 字节（没有 mode）
            print(' "%s" (len=%d)' % (bonlib.get_string(child), bonlib.size(child) - 9))
        elif kind == bonlib.BON_BLOB:
            payload = bonlib.get_bytes(child).tobytes()
            head = " ".join("%02x" % b for b in payload[:16])
            print(" blob[%d] = %s%s" % (len(payload), head, " ..." if len(payload) > 16 else ""))
        elif kind == bonlib.BON_DECIMAL:
            coeff, scale = bonlib.get_decimal(child)
            # 出参在 bonlib 里是元组；要一个 Decimal 对象就交给 bonpy。
            print(" decimal=%dE-%d (= %s)" % (coeff, scale, bonpy.to_python(child)))
        elif kind in (bonlib.BON_ARRAY, bonlib.BON_OBJECT):
            label = "array" if kind == bonlib.BON_ARRAY else "object"
            print(" %s[%d]" % (label, bonlib.count(child)))
            stack.append([child, 0, bonlib.count(child), "value"])

    print()


# ----------------------------------------------------------------------
# 示例 1：用写入器构建一个文档
# ----------------------------------------------------------------------
def build_document() -> Node:
    """构造一个覆盖全部 9 种类型的对象。"""
    with bonlib.writer_new(bonlib.BON_OBJECT) as w:
        # --- name: "Alice" ---
        bonlib.w_string(w, "name")
        bonlib.w_string(w, "Alice")

        # --- age: 30 ---  自动选 uint8
        bonlib.w_string(w, "age")
        bonlib.w_int64(w, 30)

        # --- score: 95.5 ---
        bonlib.w_string(w, "score")
        bonlib.w_double(w, 95.5)

        # --- active: true ---
        bonlib.w_string(w, "active")
        bonlib.w_bool(w, True)

        # --- tags: ["c", "bon", "ffi"] ---
        bonlib.w_string(w, "tags")
        bonlib.w_array_begin(w)
        bonlib.w_string(w, "c")
        bonlib.w_string(w, "bon")
        bonlib.w_string(w, "ffi")
        bonlib.w_array_end(w)

        # --- address: { city: "Beijing", zip: 100000 } ---
        bonlib.w_string(w, "address")
        bonlib.w_object_begin(w)
        bonlib.w_string(w, "city")
        bonlib.w_string(w, "Beijing")
        bonlib.w_string(w, "zip")
        bonlib.w_int64(w, 100000)
        bonlib.w_object_end(w)

        # --- blob: 0xDEADBEEF ---
        bonlib.w_string(w, "blob")
        bonlib.w_blob(w, bytes([0xDE, 0xAD, 0xBE, 0xEF]))

        # --- price: 123.45 -> 系数 12345, scale 2 ---
        bonlib.w_string(w, "price")
        bonlib.w_decimal(w, 12345, 2)

        # --- nothing: null ---
        bonlib.w_string(w, "nothing")
        bonlib.w_none(w)

        # writer_finish() 成功才消耗 writer；失败时 with 语句会替我们 free()。
        return bonlib.writer_finish(w)


# ----------------------------------------------------------------------
# 示例 2：通过键查找并读取值
# ----------------------------------------------------------------------
def lookup_demo(root: Node) -> None:
    print("\n===== 键查找示例 =====")

    name = bonlib.find(root, "name")
    if name is not None:
        print('name = "%s"' % bonlib.get_string(name))

    age = bonlib.find(root, "age")
    if age is not None:
        print("age = %d" % bonlib.get_int64(age))

    # 键查找返回 None 而不是抛异常 —— 键不存在是一个正常结果，不是失败。
    missing = bonlib.find(root, "missing")
    print("missing 键查找结果: %s（预期 None）" % missing)

    # 也可以按字节匹配，支持非 UTF-8 的键。
    raw = bonlib.find(root, b"t\x00g")
    print("按原始字节查找 b't\\x00g': %s" % (bonlib.get_string(raw) if raw else None))

    print('键 "tags" 出现 %d 次' % bonlib.key_count(root, "tags"))
    print("第 0 个键是 name: %s" % bonlib.key_is(root, 0, "name"))
    # C 没有 bon_key_string()，解码键就是 key_at() 之后再 get_string()。
    print("第 0 个键名: %s" % bonlib.get_string(bonlib.key_at(root, 0)))
    print("第 0 个键的原始字节: %r" % bytes(bonlib.key_cstr(root, 0)))

    # 任意类型都可以做键，也可以有重复键。
    pairs = bonpy.ObjectPairs([("k", 1), ("k", 2), (7, "seven")])
    dup = bonlib.from_stream(bonpy.dumps(pairs))
    print('重复键 "k" 出现 %d 次' % bonlib.key_count(dup, "k"))
    for key, value in bonpy.iter_items(dup):
        if bonlib.type(key) == bonlib.BON_STRING:
            label = repr(bonlib.get_string(key))
        else:
            label = "<%s %d>" % (bonlib.type_name(key), bonpy.get_int(key))
        print("  %s -> %r" % (label, bonpy.to_python(value)))


# ----------------------------------------------------------------------
# 示例 3：用迭代器遍历对象
# ----------------------------------------------------------------------
def iter_demo(root: Node) -> None:
    print("\n===== 迭代器示例 =====")

    # which=1 -> 只遍历值；下标由游标自己记录。
    print("--- 显式游标（which=1 -> 值）---")
    cursor = bonlib.iter_init(root, 1)
    while True:
        # C 的 bon_iter_next(it, &item) 走到头时 item 为空指针，这里就是 None。
        item = bonlib.iter_next(cursor)
        if item is None:
            break
        print("  值 #%d: 类型=%s size=%d"
              % (bonlib.iter_index(cursor), bonlib.type_name(item), bonlib.size(item)))
    bonlib.iter_done(cursor)   # C 的 bon_iter_done() 是析构，不是“是否读完”

    print("--- 键值对 ---")
    for key, value in bonpy.iter_items(root):
        if bonlib.type(key) == bonlib.BON_STRING:
            name = bonlib.get_string(key)
        else:
            name = "<%s>" % bonlib.type_name(key)
        print("  %s -> %s" % (name, bonlib.type_name(value)))

    print("--- 游标在容器被释放后依然可用 ---")
    tags = bonlib.find(root, "tags")
    cursor = bonlib.iter_init(tags, 0)
    del tags
    first = bonlib.iter_next(cursor)
    print("  parent 已删除，仍读到: %s" % bonlib.get_string(first))
    bonlib.iter_done(cursor)


# ----------------------------------------------------------------------
# 示例 4：JSON 调试输出与十六进制转储
# ----------------------------------------------------------------------
def json_demo(root: Node) -> None:
    print("\n===== JSON 调试输出 =====")
    print(bonlib.to_json(root))

    print("\n===== 十六进制转储 =====")
    # stream() 只给根 TLV；想连魔数一起看就用 bonpy.stream_bytes()。
    print(bonlib.to_hex(bonlib.stream(root)), end="")


# ----------------------------------------------------------------------
# 示例 5：保存到文件并重新加载
# ----------------------------------------------------------------------
def file_demo(root: Node) -> None:
    print("\n===== 文件保存/加载示例 =====")

    path = "example.bon"
    bonlib.save_file(root, path)
    print("已保存到 %s（%d 字节）" % (path, bonlib.size(root) + bonlib.MAGIC_SIZE))

    loaded = bonlib.from_file(path)
    print("加载后根节点类型: %s, 子项数: %d"
          % (bonlib.type_name(loaded), bonlib.count(loaded)))

    # 验证加载的文档与原始文档逐字节相同
    identical = bonpy.stream_bytes(loaded) == bonpy.stream_bytes(root)
    print("加载后与原文档字节一致: %s" % identical)

    # 零拷贝解析：直接借用缓冲区，不复制任何字节
    raw = open(path, "rb").read()
    borrowed = bonlib.from_stream_ref(raw)
    print("零拷贝解析结果一致: %s（缓冲区 %d 字节被原地借用）"
          % (bytes(bonpy.stream_bytes(borrowed)) == raw, len(raw)))

    name = bonlib.find(loaded, "name")
    if name is not None:
        print('加载后 name = "%s"' % bonlib.get_string(name))

    # 严格校验整个文档
    bonlib.validate(loaded)
    print("严格校验通过")


# ----------------------------------------------------------------------
# 示例 6：深层嵌套 —— 格式本身没有深度上限
# ----------------------------------------------------------------------
def deep_demo(depth: int = 20_000) -> None:
    print("\n===== 深层嵌套示例（深度 %d）=====" % depth)
    w = bonlib.writer_new(bonlib.BON_ARRAY)
    for _ in range(depth):
        bonlib.w_array_begin(w)
    bonlib.w_int64(w, 1)
    for _ in range(depth):
        bonlib.w_array_end(w)
    root = bonlib.writer_finish(w)

    # 显式栈遍历：Python 的递归上限（约 1000 层）完全够不着这里
    total = 0
    stack: list[tuple[Node, int]] = [(root, 1)]
    while stack:
        node, level = stack.pop()
        total += 1
        if bonlib.type(node) == bonlib.BON_ARRAY and bonlib.count(node):
            stack.append((bonlib.at(node, 0), level + 1))
    print("文档共 %d 字节，遍历到最深处 %d 层，共访问 %d 个节点"
          % (bonlib.size(root) + bonlib.MAGIC_SIZE, depth, total))
    bonlib.validate(root)
    print("深层文档严格校验通过")


# ----------------------------------------------------------------------
# 示例 7：高层 dumps / loads
# ----------------------------------------------------------------------
def high_level_demo() -> None:
    print("\n===== 高层 dumps / loads =====")
    from decimal import Decimal

    data = {
        "text": "héllo",
        "big": 2 ** 70,             # 超出 uint64 -> varint 模式
        "neg": -(2 ** 70) - 1,
        "pi": 3.14159,
        "money": Decimal("123.45"),
        "raw": b"\x00\x01\x02",
        "missing": None,
        "list": [1, "two", 3.0, True],
        "nested": {"inner": "value"},
    }
    encoded = bonpy.dumps(data)
    print("dumps -> %d 字节，前 16 字节: %s"
          % (len(encoded), " ".join("%02x" % b for b in encoded[:16])))
    print("loads ->", bonpy.loads(encoded))
    print("往返字节一致:", bonpy.dumps(bonpy.loads(encoded)) == encoded)

    # 对象可以是任意键类型，也可以重复，所以往返用 ObjectPairs 而不是 dict
    pairs = bonpy.ObjectPairs([("a", 1), ("a", 2), (7, "seven")])
    print("ObjectPairs 往返一致:",
          bonpy.dumps(pairs) == bonpy.dumps(bonpy.loads(bonpy.dumps(pairs))))


# ----------------------------------------------------------------------
# 示例 8：bontools —— json 形状的那一层
# ----------------------------------------------------------------------
def bontools_demo() -> None:
    """``bon.load`` 拿节点，``.bontools.kv`` 按路径读写。

    和 json 的差别只有两处，都是有意为之：
    ``bon.dumps`` 收的是节点（``bonpy.dumps`` 才收 Python 数据），
    写操作是重编码后把节点指向新字节，所以不重新赋值也立刻可见。
    """
    import bon as bon_module
    from bon import bontype

    print("\n===== bontools：按路径读写 =====")
    data = bon_module.loads(bonpy.dumps({
        "name": "Alice", "age": 30, "tags": ["c", "bon"], "address": {"zip": 100000},
    }))

    print("name              ->", data.bontools.kv.key("name").get())
    print("tags[1]           ->", data.bontools.kv.key("tags").index(1).get())
    print("address/zip       ->", data.bontools.kv.key("address").key("zip").get())
    print("missing 键        ->", data.bontools.kv.key("nope").get(), "（预期 None）")
    print("get(raw=True)     ->", repr(data.bontools.kv.key("tags").get(raw=True)))

    # 写：同一个 data 对象不重新赋值，下一次 get() 就已经是新值
    data.bontools.kv.key("age").set(31)
    data.bontools.kv.key("tags").index(2).set("ffi")          # 数组末尾追加
    data.bontools.kv.key("address").key("city").set("Beijing")  # 不存在的键就新增
    data.bontools.kv.key("id").set(bontype.number("1267650600228229401496703205376"))
    print("改完 age          ->", data.bontools.kv.key("age").get())
    print("改完 tags         ->", data.bontools.kv.key("tags").get())
    print("新增 city         ->", data.bontools.kv.key("address").key("city").get())
    big = data.bontools.kv.key("id").get(raw=True)
    print("bontype.number 越界 ->", bonpy.get_int(big), "mode:", bonpy.mode_name(
        bonlib.get_number_mode(big)))

    # 没动的地方逐字节不变
    print("重编码仍然合法    ->", bonlib.validate(bon_module.loads(
        bon_module.dumps(data))) is not None)
    path = tmp_path("bontools.bon")
    bon_module.dump(data, path)
    print("dump 后重新 load  ->", bon_module.load(path).bontools.kv.key("name").get())
    os.remove(path)


def tmp_path(name: str) -> str:
    return os.path.join(tempfile.gettempdir(), name)


# ----------------------------------------------------------------------
# 示例 9：错误处理
# ----------------------------------------------------------------------
def _array_doc(*children: bytes) -> bytes:
    """把若干原始子 TLV 包成一个单层 ARRAY 文档。

    容器头是 ``type(1) + total_len(8) + count(8)``，``total_len`` 只算子节点字节数，
    和参考实现一致（``bonlib.size()`` 会把它换算成整个 TLV 的大小）。
    """
    body = b"".join(children)
    return (bonlib.MAGIC + bytes([bonlib.BON_ARRAY])
            + len(body).to_bytes(8, "little")
            + len(children).to_bytes(8, "little") + body)


def _varint(payload: bytes) -> bytes:
    """一个带指定载荷的 NUMBER/VARINT 子节点（不做合法性检查，方便造坏数据）。"""
    return (bytes([bonlib.BON_NUMBER, bonlib.BON_MODE_VARINT])
            + len(payload).to_bytes(8, "little") + payload)


def error_demo() -> None:
    print("\n===== 错误处理示例 =====")
    # from_stream() 只解析根 TLV（浅解析），结构性问题要交给 validate()，
    # 这一点和参考实现一致：下面两段分别演示同一次解析的两个阶段。
    for label, action in (
        ("魔数不对", lambda: bonlib.from_stream(b"xxx" + bytes(20))),
        # 根 TLV 一律按容器头的 17 字节起步来读，所以不足 20 字节的流先报截断，
        # 哪怕第 4 个字节已经写着标量类型（参考实现同样如此）。
        ("流太短", lambda: bonlib.from_stream(bonlib.MAGIC + bytes(16))),
        ("根是标量", lambda: bonlib.from_stream(
            bonlib.MAGIC + bytes([bonlib.BON_NONE]) + bytes(16))),
        ("根长度超界", lambda: bonlib.from_stream(
            bonlib.MAGIC + bytes([bonlib.BON_ARRAY]) + (1 << 40).to_bytes(8, "little")
            + (0).to_bytes(8, "little"))),
        ("根后有多余字节", lambda: bonlib.from_stream(
            bonpy.dumps([1]) + b"\x00\x00")),
    ):
        try:
            action()
        except bonlib.BonError as exc:
            print("  parse    %-16s -> code=%-2d %s" % (label, exc.code, exc.message))

    for label, doc in (
        ("保留的子类型", _array_doc(bytes([0x09, 0x00]))),
        ("保留的 NUMBER 模式", _array_doc(bytes([bonlib.BON_NUMBER, 0x09, 0x00]))),
        ("BOOL 载荷非法", _array_doc(bytes([bonlib.BON_BOOL, 0x02]))),
        ("varint 非最小", _array_doc(_varint(bytes([0x00, 0x00, 0x00])))),
        ("varint 为空", _array_doc(_varint(b""))),
    ):
        try:
            bonlib.validate(bonlib.from_stream(doc))
        except bonlib.BonError as exc:
            print("  validate %-18s -> code=%-2d %s" % (label, exc.code, exc.message))


# ----------------------------------------------------------------------
# main
# ----------------------------------------------------------------------
def main() -> int:
    print("BON 版本: %s（Python 纯实现，无 C 扩展）" % bonlib.VERSION)
    print("魔数: %s" % " ".join("%02x" % b for b in bonlib.MAGIC))

    # ---------- 构建文档 ----------
    root = build_document()

    # ---------- 打印整个文档树 ----------
    print("\n===== 文档树 =====")
    dump_node(root, 0)

    # ---------- 原始字节 ----------
    print("\n===== 原始流 =====")
    print("文件总大小: %d 字节 = 3（魔数）+ %d（根 TLV）"
          % (bonlib.size(root) + bonlib.MAGIC_SIZE, bonlib.size(root)))
    head = " ".join("%02x" % b for b in bonlib.stream(root)[:32])
    print("前 32 字节: %s" % head)
    # data() 是零拷贝视图：两个节点的视图指向同一段底层缓冲区
    name_node: Any = bonlib.find(root, "name")
    print("子节点是父缓冲区的视图: root.data 与 find('name').data 指向同一段内存: %s"
          % (bonlib.data(root).obj is bonlib.data(name_node).obj))

    # ---------- 各类演示 ----------
    lookup_demo(root)
    iter_demo(root)
    json_demo(root)
    file_demo(root)
    deep_demo()
    high_level_demo()
    bontools_demo()
    error_demo()

    print("\n完成。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
