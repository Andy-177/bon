/*
 * bon_example.c — BON v1.0 C ABI 使用示例
 *
 * 演示内容：
 *   1. 用流式写入器构建一个嵌套对象
 *   2. 解析字节流并遍历所有节点
 *   3. 读取标量值（字符串、整数、浮点、布尔）
 *   4. 通过键查找值
 *   5. 输出 JSON 调试视图
 *   6. 保存到文件并重新加载
 *
 * 编译（Linux/macOS）：
 *   cc -o bon_example bon_example.c -lbon
 *
 * 编译（Windows MSVC）：
 *   cl bon_example.c bon.lib
 */

#include "bon.h"

#include <stdio.h>
#include <string.h>
#include <stdlib.h>

/* ------------------------------------------------------------------ */
/* 辅助：打印缩进                                                     */
/* ------------------------------------------------------------------ */
static void indent(int depth)
{
    for (int i = 0; i < depth; i++)
        fputs("  ", stdout);
}

/* ------------------------------------------------------------------ */
/* 递归遍历并打印一个 BON 节点                                        */
/* ------------------------------------------------------------------ */
static void dump_node(const bon* node, int depth)
{
    int type = bon_type(node);

    indent(depth);
    printf("[%s] size=%zu", bon_type_name(node), bon_size(node));

    switch (type) {
    case BON_NONE:
        printf(" (null)");
        break;

    case BON_BOOL:
        printf(" value=%s", bon_get_bool(node) ? "true" : "false");
        break;

    case BON_NUMBER: {
        int64_t  i64;
        uint64_t u64;
        if (bon_get_int64(node, &i64) == BON_E_OK)
            printf(" int64=%lld", (long long)i64);
        else if (bon_get_uint64(node, &u64) == BON_E_OK)
            printf(" uint64=%llu", (unsigned long long)u64);
        else {
            const char* s = bon_get_cstr(node);
            printf(" int_str=%s", s ? s : "?");
        }
        break;
    }

    case BON_FLOAT: {
        double d = 0.0;
        bon_get_double(node, &d);
        printf(" double=%g", d);
        break;
    }

    case BON_STRING: {
        size_t len = 0;
        const char* s = bon_get_cstr(node);
        printf(" \"%s\" (len=%zu)", s ? s : "?", bon_get_bytes(node, &len) ? len : 0);
        break;
    }

    case BON_BLOB: {
        size_t len = 0;
        const void* p = bon_get_bytes(node, &len);
        printf(" blob[%zu] =", len);
        const unsigned char* b = (const unsigned char*)p;
        for (size_t i = 0; i < len && i < 16; i++)
            printf(" %02x", b[i]);
        if (len > 16) printf(" ...");
        break;
    }

    case BON_DECIMAL: {
        int64_t  coeff = 0;
        uint64_t scale = 0;
        bon_get_decimal(node, &coeff, &scale);
        printf(" decimal=%lldE-%llu", (long long)coeff, (unsigned long long)scale);
        break;
    }

    case BON_ARRAY: {
        size_t n = bon_count(node);
        printf(" array[%zu]", n);
        for (size_t i = 0; i < n; i++) {
            bon* child = bon_at(node, i);
            if (child) {
                printf("\n");
                dump_node(child, depth + 1);
                bon_unref(child);
            }
        }
        break;
    }

    case BON_OBJECT: {
        size_t n = bon_count(node);
        printf(" object[%zu]", n);
        for (size_t i = 0; i < n; i++) {
            bon* key   = bon_key_at(node, i);
            bon* value = bon_value_at(node, i);
            printf("\n");
            indent(depth + 1);
            if (key) {
                const char* ks = bon_get_cstr(key);
                printf("key \"%s\":\n", ks ? ks : "?");
                bon_unref(key);
            } else {
                printf("key (non-string):\n");
            }
            if (value) {
                dump_node(value, depth + 2);
                bon_unref(value);
            }
        }
        break;
    }

    default:
        printf(" (unknown type)");
        break;
    }

    printf("\n");
}

/* ------------------------------------------------------------------ */
/* 示例 1：用写入器构建一个文档                                       */
/* ------------------------------------------------------------------ */
static bon* build_document(void)
{
    bon_writer* w = bon_writer_new(BON_OBJECT);
    if (!w) {
        fprintf(stderr, "bon_writer_new failed: %s\n", bon_last_error());
        return NULL;
    }

    /* --- name: "Alice" --- */
    bon_w_string(w, "name", 4);
    bon_w_string(w, "Alice", 5);

    /* --- age: 30 --- */
    bon_w_string(w, "age", 3);
    bon_w_int64(w, 30);

    /* --- score: 95.5 --- */
    bon_w_string(w, "score", 5);
    bon_w_double(w, 95.5);

    /* --- active: true --- */
    bon_w_string(w, "active", 6);
    bon_w_bool(w, 1);

    /* --- tags: ["c", "bon", "ffi"] --- */
    bon_w_string(w, "tags", 4);
    bon_w_array_begin(w);
    bon_w_string(w, "c", 1);
    bon_w_string(w, "bon", 3);
    bon_w_string(w, "ffi", 3);
    bon_w_array_end(w);

    /* --- address: { city: "Beijing", zip: 100000 } --- */
    bon_w_string(w, "address", 7);
    bon_w_object_begin(w);
    bon_w_string(w, "city", 4);
    bon_w_string(w, "Beijing", 7);
    bon_w_string(w, "zip", 3);
    bon_w_int64(w, 100000);
    bon_w_object_end(w);

    /* --- blob: 0xDEADBEEF --- */
    {
        const unsigned char raw[] = { 0xDE, 0xAD, 0xBE, 0xEF };
        bon_w_string(w, "blob", 4);
        bon_w_blob(w, raw, sizeof(raw));
    }

    /* --- decimal: 123.45 -> coefficient=12345, scale=2 --- */
    bon_w_string(w, "price", 5);
    bon_w_decimal(w, 12345, 2);

    /* --- none: null --- */
    bon_w_string(w, "nothing", 7);
    bon_w_none(w);

    bon* root = bon_writer_finish(w);
    if (!root) {
        fprintf(stderr, "bon_writer_finish failed: %s\n", bon_last_error());
        bon_writer_free(w);   /* finish 失败后必须手动释放 */
        return NULL;
    }
    return root;
}

/* ------------------------------------------------------------------ */
/* 示例 2：通过键查找并读取值                                         */
/* ------------------------------------------------------------------ */
static void lookup_demo(const bon* root)
{
    puts("\n===== 键查找示例 =====");

    /* 查找 name */
    bon* name = bon_find(root, "name", 4);
    if (name) {
        printf("name = \"%s\"\n", bon_get_cstr(name));
        bon_unref(name);
    }

    /* 查找 age */
    bon* age = bon_find(root, "age", 3);
    if (age) {
        int64_t v = 0;
        bon_get_int64(age, &v);
        printf("age = %lld\n", (long long)v);
        bon_unref(age);
    }

    /* 查找不存在的键 */
    bon* missing = bon_find(root, "missing", 7);
    if (!missing)
        printf("missing 键不存在（预期行为）\n");
    else
        bon_unref(missing);

    /* 统计同名键出现次数 */
    size_t n = bon_key_count(root, "tags", 4);
    printf("键 \"tags\" 出现 %zu 次\n", n);

    /* 检查第 0 个键是否为 "name" */
    if (bon_key_is(root, 0, "name", 4))
        printf("第 0 个键是 \"name\"\n");
}

/* ------------------------------------------------------------------ */
/* 示例 3：用迭代器遍历对象                                           */
/* ------------------------------------------------------------------ */
static void iter_demo(const bon* root)
{
    puts("\n===== 迭代器示例 =====");

    bon_iter it;
    bon_iter_init(&it, (bon*)root, 1);   /* which=1 -> 遍历值 */

    bon* item = NULL;
    while (bon_iter_next(&it, &item) == 1) {
        size_t idx = bon_iter_index(&it);
        printf("值 #%zu: 类型=%s\n", idx, bon_type_name(item));
        bon_unref(item);
    }
    bon_iter_done(&it);

    /* 键值对迭代 */
    puts("--- 键值对 ---");
    bon_iter_init(&it, (bon*)root, 0);
    bon* key   = NULL;
    bon* value = NULL;
    while (bon_iter_key_next(&it, &key, &value) == 1) {
        const char* ks = key ? bon_get_cstr(key) : "(non-string)";
        printf("  %s -> %s\n", ks ? ks : "?", bon_type_name(value));
        if (key)   bon_unref(key);
        if (value) bon_unref(value);
    }
    bon_iter_done(&it);
}

/* ------------------------------------------------------------------ */
/* 示例 4：JSON 调试输出                                              */
/* ------------------------------------------------------------------ */
static void json_demo(const bon* root)
{
    puts("\n===== JSON 调试输出 =====");
    bon_write_json(stdout, root);
    putchar('\n');
}

/* ------------------------------------------------------------------ */
/* 示例 5：保存到文件并重新加载                                       */
/* ------------------------------------------------------------------ */
static void file_demo(const bon* root)
{
    puts("\n===== 文件保存/加载示例 =====");

    const char* path = "example.bon";
    if (bon_save_file(root, path) != BON_E_OK) {
        fprintf(stderr, "保存失败: %s\n", bon_last_error());
        return;
    }
    printf("已保存到 %s\n", path);

    bon* loaded = bon_from_file(path);
    if (!loaded) {
        fprintf(stderr, "加载失败: %s\n", bon_last_error());
        return;
    }

    printf("加载后根节点类型: %s, 子项数: %zu\n",
           bon_type_name(loaded), bon_count(loaded));

    /* 验证加载的文档与原始文档等价 */
    bon* name = bon_find(loaded, "name", 4);
    if (name) {
        printf("加载后 name = \"%s\"\n", bon_get_cstr(name));
        bon_unref(name);
    }

    bon_unref(loaded);
}

/* ------------------------------------------------------------------ */
/* main                                                               */
/* ------------------------------------------------------------------ */
int main(void)
{
    printf("BON 版本: %s\n", bon_version());

    /* ---------- 构建文档 ---------- */
    bon* root = build_document();
    if (!root) {
        fprintf(stderr, "构建文档失败\n");
        return 1;
    }

    /* 打印整个文档树 */
    puts("\n===== 文档树 =====");
    dump_node(root, 0);

    /* ---------- 获取原始字节 ---------- */
    {
        const void* bytes = NULL;
        size_t size = 0;
        if (bon_stream(root, &bytes, &size) == BON_E_OK) {
            printf("\n原始流大小: %zu 字节\n", size);
            printf("前 32 字节: ");
            const unsigned char* p = (const unsigned char*)bytes;
            for (size_t i = 0; i < size && i < 32; i++)
                printf("%02x ", p[i]);
            putchar('\n');
        }
    }

    /* ---------- 各种演示 ---------- */
    lookup_demo(root);
    iter_demo(root);
    json_demo(root);
    file_demo(root);

    /* ---------- 清理 ---------- */
    bon_unref(root);
    puts("\n完成。");
    return 0;
}