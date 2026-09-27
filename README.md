# BON (Binary Object Notation) v1.0

二进制对象序列化格式 + 单文件 C 库实现。设计目标：小、快、零拷贝、可被任何语言通过 FFI 直接使用。

- **格式规范**：`bon.md`（规范性文档，本 README 只讲怎么用）
- **实现**：`bon.cpp`（约 2400 行）+ `bon.h`（纯 C11 头文件）
- **完整示例**：`bon_example.cpp`（369 行，9 种类型全覆盖）

```
bon.h / bon.cpp    库本体
bon.md             协议规范（规范性文档）
bon_example.cpp    使用示例（纯 C 风格代码，.c / .cpp 都能编译）
example.bon        示例程序生成的示例文件
bon.dll / bon.lib  预编译的 Windows 动态库与导入库（MinGW-w64 GCC 16.2.0）
```

---

## 构建

### Windows（MinGW-w64 + GCC）

> 下面的命令在 **PowerShell** 下直接可用（`` ` `` 是 PowerShell 的续行符）；在 cmd / bash 里请写成一行。
> 需要先把 MinGW-w64 的 `bin` 目录加进 `PATH`。

生成 DLL 和导入库：

```powershell
cd E:\Projects\Project\bon
g++ -std=c++17 -O2 -Wall -Wextra -DBON_BUILD_DLL -shared -o "E:\Projects\Project\bon\bon.dll" bon.cpp -static-libgcc -static-libstdc++ "-Wl,--out-implib,bon.lib"
```

链接动态库编译自己的程序：

```powershell
g++ -std=c++17 -O2 -Wall -Wextra -DBON_USE_DLL -I "E:\Projects\Project\bon" -o app.exe app.cpp -L "E:\Projects\Project\bon" -lbon
```

运行前确保 `bon.dll` 位于可执行文件同目录（或已加入 `PATH`）。

不想用 DLL，直接把实现编进可执行文件：

```powershell
g++ -std=c++17 -O2 -Wall -Wextra -I "E:\Projects\Project\bon" -o app.exe app.cpp bon.cpp
```

> `bon.dll` 静态链接了 `libgcc` / `libstdc++`，运行 `app.exe` **不需要**随包分发 MinGW 运行库。

### Linux / macOS（GCC 或 Clang）

动态库：

```bash
g++ -std=c++17 -O2 -Wall -Wextra -fPIC -DBON_BUILD_DLL -shared -o libbon.so bon.cpp
```

静态库：

```bash
g++ -std=c++17 -O2 -Wall -Wextra -c bon.cpp -o bon.o
ar rcs libbon.a bon.o
```

链接动态库（需要 `LD_LIBRARY_PATH=.`）或静态库：

```bash
g++ -std=c++17 -O2 -Wall -Wextra -I. -o app app.cpp -L. -lbon            # 动态
g++ -std=c++17 -O2 -Wall -Wextra -I. -o app app.cpp libbon.a -pthread  # 静态
```

> Windows 用 `BON_USE_DLL` 区分导入/导出，Linux/macOS 不需要任何宏。

### 运行示例

```powershell
g++ -std=c++17 -O2 -Wall -Wextra -DBON_USE_DLL -I "E:\Projects\Project\bon" -o bon_example.exe bon_example.cpp -L "E:\Projects\Project\bon" -lbon
.\bon_example.exe
```

示例会打印文档树、键查找、迭代器、JSON 视图，并在当前目录生成 `example.bon`。

---

## 5 分钟快速上手

### 写

```cpp
#include "bon.h"
#include <stdio.h>

bon_writer* w = bon_writer_new(BON_OBJECT);   // 写出魔数 + 根对象头
bon_w_string(w, "name", 4);
bon_w_string(w, "Alice", 5);
bon_w_string(w, "age", 3);
bon_w_int64(w, 30);                            // 自动选 uint8 编码
bon_w_string(w, "tags", 4);
bon_w_array_begin(w);
    bon_w_string(w, "cpp", 3);
bon_w_array_end(w);

bon* root = bon_writer_finish(w);              // 成功时消耗 writer
if (!root) {
    bon_writer_free(w);                        // 失败时 writer 仍归你所有
    fprintf(stderr, "bon: %s\n", bon_last_error());
    return 1;
}
```

`bon_writer_new(BON_OBJECT)` 已经写好了根容器头，接下来写的第一个标量就属于根；
`bon_w_array_begin()` / `bon_w_object_begin()` 只是再往下嵌套一层。

### 读

```cpp
bon* root = bon_from_file("example.bon");      // 拷贝字节，自成一体
if (!root) {
    fprintf(stderr, "bon: %s\n", bon_last_error());
    return 1;
}
if (bon_validate(root) != 0) {                 // 严格校验整个文档
    fprintf(stderr, "bon: %s\n", bon_last_error());
}

bon* name = bon_find(root, "name", 4);         // 新引用，用完必须释放
if (name) {
    printf("name = %s\n", bon_get_cstr(name));
    bon_unref(name);
}

bon* tags = bon_find(root, "tags", 4);
for (size_t i = 0; i < bon_count(tags); ++i) {
    bon* item = bon_at(tags, i);               // ARRAY 取元素 / OBJECT 取值
    printf("[%zu] %s\n", i, bon_get_cstr(item));
    bon_unref(item);
}
bon_unref(tags);

const void* bytes = NULL;
size_t size = 0;
if (bon_stream(root, &bytes, &size) == BON_E_OK) {
    printf("文件共 %zu 字节\n", size);         // = 3 + bon_size(root)
}

bon_unref(root);
```

`bon_from_file()` 会把文件读进一块新内存；如果你已经有字节，用
`bon_from_stream_ref()` 可以完全零拷贝地借用（此时缓冲区必须活得比整棵节点树久）。

---

## API 速查

### 解析与文件

| 函数 | 说明 |
| --- | --- |
| `bon_from_stream(bytes, size)` | 解析并**拷贝**字节 |
| `bon_from_stream_ref(bytes, size)` | 解析并**借用**调用方的缓冲区（零拷贝） |
| `bon_from_file(path)` | 读文件并解析（拷贝） |
| `bon_validate(node)` | 严格校验：魔数、版本、保留类型、最短 varint、容器长度、子节点个数 |
| `bon_save_file(node, path)` | 写出完整文件（自动补魔数） |

### 结构与原始字节

| 函数 | 说明 |
| --- | --- |
| `bon_type(node)` / `bon_type_name(node)` | 类型码 / 类型名（`"STRING"` 等） |
| `bon_count(node)` | ARRAY / OBJECT 的子项个数；其余类型为 0 |
| `bon_data(node)` / `bon_size(node)` | 该节点自身的 TLV 字节与长度（**不含**魔数） |
| `bon_stream(node, &bytes, &size)` | 完整文件字节（仅根节点） |
| `bon_magic(node, &size)` | 3 字节魔数 |
| `bon_get_bytes(node, &size)` | STRING / BLOB 的载荷，**零拷贝** |
| `bon_root(node)` / `bon_is_root(node)` | 追溯根节点 / 是否为根 |

### 导航（全部返回新引用）

| 函数 | 说明 |
| --- | --- |
| `bon_at(node, i)` | 第 i 个子节点：ARRAY 取元素，OBJECT 取值 |
| `bon_key_at(node, i)` / `bon_value_at(node, i)` | OBJECT 键值对的两个半边 |
| `bon_find(node, key, key_size)` | 线性查找，只匹配 STRING 键；找不到返回 `NULL` |
| `bon_key_count(node, key, key_size)` | 同名键出现次数（对象可有重复键） |
| `bon_key_is(node, i, key, key_size)` | 第 i 个键是否等于给定字节 |
| `bon_iter_init(&it, node, which)` | `which=1` 遍历值，`which=0` 遍历键值对 |
| `bon_iter_next(&it, &item)` | 返回 1 = 产出，0 = 结束，-1 = 出错 |
| `bon_iter_key_next(&it, &key, &value)` | 键值对版本 |
| `bon_iter_index(&it)` | 刚取到的项的 0 基下标，可回传给 `bon_at()` |
| `bon_iter_done(&it)` | 必须调用（迭代器自己持有容器引用） |

### 标量读取

| 函数 | 返回方式 |
| --- | --- |
| `bon_get_bool(node)` | `int`（0/1） |
| `bon_get_int64(node, &v)` / `bon_get_uint64(node, &v)` | 任意精度整数落在范围内时成功 |
| `bon_get_int_str(node, buf, cap, &need)` | 十进制字符串（任意精度），`buf=NULL` 只测长度 |
| `bon_get_int_bytes(node, &p, &size, &neg)` | 最短二进制补码；**VARINT 零拷贝**，定宽走暂存 |
| `bon_get_int64` 溢出 | 返回 `BON_E_RANGE`，改用 `bon_get_int_str()` |
| `bon_get_double(node, &v)` | FLOAT 专用 |
| `bon_get_decimal(node, &coeff, &scale)` | DECIMAL 专用：`coeff × 10^-scale` |
| `bon_get_string(node, buf, cap, &need)` | 拷贝进调用方缓冲 |
| `bon_get_cstr(node)` | 线程局部暂存的 NUL 结尾字符串（见下） |

### 流式写入器

| 函数 | 说明 |
| --- | --- |
| `bon_writer_new(BON_ARRAY\|BON_OBJECT)` | 立即写出魔数 + 根容器头 |
| `bon_w_none/bool/int64/uint64/double/decimal` | 标量（返回 `BON_E_OK` 或错误码） |
| `bon_w_string(w, p, n)` / `bon_w_blob(w, p, n)` | 变长字节，长度单独给出，可含 `\0` |
| `bon_w_int_bytes(w, p, n)` | 自动选最短无损线格式（定宽或 VARINT） |
| `bon_w_int_bytes_varint(...)` | 强制 VARINT（需要定宽语义时**不要**用） |
| `bon_w_int_dec(w, digits, n)` | 任意精度十进制字符串 |
| `bon_w_array_begin/end`、`bon_w_object_begin/end` | 嵌套容器，结束时回填长度与个数 |
| `bon_writer_finish(w)` | **仅成功时消耗 writer**，返回根节点 |
| `bon_writer_free(w)` | 唯一的销毁函数，任何时候对持有的句柄都安全 |
| `bon_writer_data/size/depth` | 写入进度（回填前容器长度尚未最终确定） |

### 输出

| 函数 | 说明 |
| --- | --- |
| `bon_write_json(out, node)` | 单向调试视图，不可逆 |
| `bon_write_hex(out, bytes, size)` | 十六进制转储 |

### 错误

| 函数 | 说明 |
| --- | --- |
| `bon_last_error_code()` | 最近一次失败的错误码 |
| `bon_last_error()` | 错误描述字符串 |
| `bon_clear_error()` | 清空错误槽 |
| `bon_error_message(code)` | 按错误码查描述 |

错误码：`BON_E_INVALID` `BON_E_MAGIC` `BON_E_TRUNCATED` `BON_E_TYPE` `BON_E_MODE`
`BON_E_LENGTH` `BON_E_ROOT` `BON_E_RANGE` `BON_E_NOKEY` `BON_E_DEPTH` `BON_E_STATE`
`BON_E_IO` `BON_E_MEMORY` `BON_E_USAGE`

---

## 所有权与生命周期

| 规则 | 说明 |
| --- | --- |
| 每个 `bon*` 都是一次引用 | `bon_find()` / `bon_at()` / 迭代器产出的节点都归你所有 |
| 用完就 `bon_unref()` | 最后一个引用消失时节点才被销毁 |
| 子节点是父缓冲区的视图 | `bon_data(child)` 指向 `bon_data(parent)` 内部；子节点靠引用计数保活 |
| 父先释放，子还能用 | 子节点自己持有引用，所以可以先 `bon_unref(root)` 再用子节点 |
| 零拷贝指针的寿命 = 节点寿命 | `bon_get_bytes()` / `bon_data()` 返回的指针在节点释放后失效 |
| `bon_from_stream_ref()` 借用外部缓冲区 | 缓冲区必须活得比整棵树久，且期间不可修改 |
| writer 只被 `finish` 成功或 `free` 销毁 | `finish` 失败时 writer 被毒化，仍需 `bon_writer_free()` |

```cpp
bon* child = bon_at(root, 0);
bon_unref(root);      // 父节点没了
bon_get_cstr(child);  // 仍然合法
bon_unref(child);
```

---

## 三条最容易踩的规则

1. **`bon_data()` / `bon_size()` 是节点 TLV，不含魔数。**
   根文件大小 = `3 + bon_size(root)`，要完整字节用 `bon_stream()`。

2. **`bon_get_cstr()` / `bon_key_cstr()` 的结果在 4 个线程局部槽位里轮转。**
   可以同时持有多个结果（连续 3 次调用都不会覆盖最早的），第 4 次才复用。
   非 STRING 节点返回 `NULL`。要留更久请用 `bon_get_string()` 拷走。

3. **`bon_get_int_bytes()` 有两种生命周期。**
   VARINT 模式是零拷贝指针（活到节点释放）；定宽模式是线程局部暂存，
   内容是二进制补码字节、**没有 NUL 结尾**，符号单独通过 `negative` 输出。

---

## 线程与错误处理

- 所有 API 都是可重入的，除线程局部错误槽外没有共享可变状态；同一个 `bon` 对象可以被多线程并发读取。
- 写入器、流和文件 I/O 不是线程安全的（每个线程各用一个）。
- 多数函数在入口处清空错误槽，**失败后请立即读取** `bon_last_error()`，再做别的调用。

---

## 限制

- **格式没有深度和体积上限**：容器长度、元素个数都是 uint64 字段，实际只受内存和地址空间约束（32 位构建最多 `SIZE_MAX`，即 4 GiB）。
- 校验、JSON 输出、写入器都是逐层显式下潜，不用 C 递归，所以嵌套多深都不会爆栈。
- `bon_get_int64()` / `bon_get_uint64()` 溢出时返回 `BON_E_RANGE`，大整数请用 `bon_get_int_str()`。
- 变长字段的 `size` 用 `size_t`，跨语言绑定时注意 32 / 64 位差异。
- 键按字节精确匹配，不做 Unicode 归一化。
- `bon_write_json()` 是调试输出，不是可逆编码：DECIMAL 带 `#bon:decimal:` 标签，
  非 STRING 键带 `#bon:<TYPE>:` 标签，BLOB 输出 `#bon:blob:<size>:<hex>`。
- 浮点数按 IEEE 754 `binary64` 原样存储，**不保证跨平台位级一致**（NaN 等边缘值会走十进制路径）。

---

## 关于文档

- `bon.md` 是规范性文档：字节布局、varint 编码、整数表示法、校验规则、参考实现契约。
- 本 README 只描述怎么构建和使用；任何冲突以 `bon.md` 为准。
- 编码规范：文件以魔数 `1F 42 4F` 开头，版本号 `1`，大端 u16 长度。
