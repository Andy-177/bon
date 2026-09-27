/*
 * BON (Binary Object Notation) v1.0 - reference implementation.
 *
 * Object model: a bon object is a byte stream.  The object owns one buffer
 * ("bon stream"); the object itself is a window (offset + length) into that
 * buffer, always starting exactly on a TLV boundary.  The root window starts
 * right after the 3 magic bytes, so the root object's TLV plus the magic is
 * the complete file byte stream.  Navigation hands out child windows, which
 * are zero-copy views into the same buffer; the buffer is reference counted,
 * so a child stays alive as long as any window into it is alive.
 *
 * The whole public surface is plain C: byte buffers, offsets, sizes and
 * fixed-width scalars.  No language type ever crosses the ABI.
 */

#include "bon.h"

#include <atomic>
#include <cstdarg>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <new>
#include <vector>

namespace {

const unsigned char kMagic[3] = { 0x62, 0x6F, 0x6E };
const size_t         kMagicLen = 3;

thread_local int  g_err = BON_E_OK;
thread_local char g_msg[256];

/* bon_get_cstr()/bon_key_cstr()/bon_get_int_bytes() hand out C strings from a
 * small rotation of thread-local scratch slots, so several results can be alive
 * at the same time (e.g. printf("%s %s", cstr(k), cstr(v))).  A result stays
 * valid until the slot it landed in comes around again, kScratchSlots calls
 * later.  The slots are only used for the NUL-terminated copies; zero-copy
 * payloads always point into the node itself. */
enum { kScratchSlots = 4 };
thread_local std::vector<char> g_scratch[kScratchSlots];
thread_local unsigned         g_scratch_at = 0;
thread_local std::vector<char> g_json;

std::vector<char>& scratch_next()
{
    std::vector<char>& slot = g_scratch[g_scratch_at];
    g_scratch_at = (g_scratch_at + 1) % kScratchSlots;
    return slot;
}

void clear_err()
{
    g_err = BON_E_OK;
    g_msg[0] = '\0';
}

void set_err(int code, const char* fmt, ...)
{
    g_err = code;
    va_list ap;
    va_start(ap, fmt);
    std::vsnprintf(g_msg, sizeof(g_msg), fmt, ap);
    va_end(ap);
}

uint64_t rd_u64(const unsigned char* p)
{
    uint64_t v = 0;
    for (int i = 7; i >= 0; --i) {
        v = (v << 8) | (uint64_t)p[i];
    }
    return v;
}

void wr_u64(unsigned char* p, uint64_t v)
{
    for (int i = 0; i < 8; ++i) {
        p[i] = (unsigned char)(v & 0xFFu);
        v >>= 8;
    }
}

size_t fixed_mode_size(unsigned char mode)
{
    switch (mode) {
        case BON_MODE_UINT8:
        case BON_MODE_NUINT8:  return 1;
        case BON_MODE_UINT16:
        case BON_MODE_NUINT16: return 2;
        case BON_MODE_UINT32:
        case BON_MODE_NUINT32: return 4;
        case BON_MODE_UINT64:
        case BON_MODE_NUINT64: return 8;
        default:               return 0;
    }
}

size_t fixed_mode_width(unsigned char mode)
{
    size_t n = fixed_mode_size(mode);
    return n == 0 ? 1 : n;
}

bool is_container(unsigned type)
{
    return type == BON_ARRAY || type == BON_OBJECT;
}

size_t trim_2c(const unsigned char* p, size_t n)
{
    while (n > 1) {
        unsigned char hi = p[n - 1];
        unsigned char next = p[n - 2];
        if (hi == 0x00 && (next & 0x80) == 0) {
            --n;
        } else if (hi == 0xFF && (next & 0x80) != 0) {
            --n;
        } else {
            break;
        }
    }
    return n;
}

void shrink_2c(std::vector<unsigned char>& v)
{
    v.resize(trim_2c(v.data(), v.size()));
}

bool mag_fits_u64(const unsigned char* mag, size_t n, uint64_t& out)
{
    if (n > 9) {
        return false;
    }
    if (n == 9) {
        if (mag[8] != 0x00) {
            return false;
        }
        n = 8;
    }
    uint64_t v = 0;
    for (size_t i = n; i-- > 0; ) {
        v = (v << 8) | (uint64_t)mag[i];
    }
    out = v;
    return true;
}

void mag_of_int(const unsigned char* p, size_t n, int negative,
                std::vector<unsigned char>& mag)
{
    mag.clear();
    mag.reserve(n + 1);
    unsigned carry = negative ? 1u : 0u;
    for (size_t i = 0; i < n; ++i) {
        unsigned b = negative ? (unsigned)(~p[i] & 0xFFu) : (unsigned)p[i];
        unsigned v = b + carry;
        carry = v >> 8;
        mag.push_back((unsigned char)(v & 0xFFu));
    }
    while (carry != 0) {
        mag.push_back((unsigned char)(carry & 0xFFu));
        carry >>= 8;
    }
    while (mag.size() > 1 && mag.back() == 0x00) {
        mag.pop_back();
    }
}

void negate_2c(const std::vector<unsigned char>& magnitude,
               std::vector<unsigned char>& out)
{
    size_t k = magnitude.size();
    if (k == 0) {
        out.assign(1, 0x00);
        return;
    }
    while (k > 1 && magnitude[k - 1] == 0x00) {
        --k;
    }
    bool extra;
    if (magnitude[k - 1] > 0x01) {
        extra = true;
    } else if (magnitude[k - 1] == 0x01) {
        extra = false;
        for (size_t i = 0; i + 1 < k; ++i) {
            if (magnitude[i] != 0x00) {
                extra = true;
                break;
            }
        }
    } else {
        extra = false;
    }
    size_t total = extra ? k + 1 : k;
    out.assign(total, 0x00);
    for (size_t i = 0; i < k; ++i) {
        out[i] = (unsigned char)(~magnitude[i] & 0xFFu);
    }
    if (extra) {
        out[k] = 0xFF;
    }
    unsigned carry = 1;
    for (size_t i = 0; i < total; ++i) {
        unsigned v = (unsigned)out[i] + carry;
        carry = v >> 8;
        out[i] = (unsigned char)(v & 0xFFu);
    }
    shrink_2c(out);
}

void u64_to_2c(uint64_t v, size_t w, std::vector<unsigned char>& out)
{
    out.resize(w);
    for (size_t i = 0; i < w; ++i) {
        out[i] = (unsigned char)(v >> (8 * i));
    }
    if ((out[w - 1] & 0x80) != 0) {
        out.push_back(0x00);
    }
}

void stored_neg_to_2c(uint64_t stored, size_t w,
                      std::vector<unsigned char>& out)
{
    (void)w;
    std::vector<unsigned char> mag;
    if (stored == 0xFFFFFFFFFFFFFFFFull) {
        mag.assign(8, 0x00);
        mag.push_back(0x01);
    } else {
        unsigned char raw[8];
        wr_u64(raw, stored + 1);
        mag.assign(raw, raw + 8);
    }
    negate_2c(mag, out);
}

unsigned char pick_pos_mode(uint64_t v)
{
    if (v <= 0xFFull)       return BON_MODE_UINT8;
    if (v <= 0xFFFFull)     return BON_MODE_UINT16;
    if (v <= 0xFFFFFFFFull) return BON_MODE_UINT32;
    return BON_MODE_UINT64;
}

unsigned char pick_neg_mode(uint64_t stored)
{
    if (stored <= 0xFFull)       return BON_MODE_NUINT8;
    if (stored <= 0xFFFFull)     return BON_MODE_NUINT16;
    if (stored <= 0xFFFFFFFFull) return BON_MODE_NUINT32;
    return BON_MODE_NUINT64;
}

bool bytes_to_i64(const unsigned char* p, size_t n, int64_t& out)
{
    if (n == 0) {
        return false;
    }
    bool negative = (p[n - 1] & 0x80) != 0;
    if (n <= 8) {
        uint64_t v = 0;
        for (size_t i = 0; i < n; ++i) {
            v |= (uint64_t)p[i] << (8 * i);
        }
        if (negative) {
            uint64_t pow2 = (n == 8) ? 0ull : (1ull << (8 * n));
            out = (int64_t)(v - pow2);
        } else {
            out = (int64_t)v;
        }
        return true;
    }
    unsigned char expect = negative ? 0xFF : 0x00;
    for (size_t i = 8; i < n; ++i) {
        if (p[i] != expect) {
            return false;
        }
    }
    uint64_t low = 0;
    for (int i = 7; i >= 0; --i) {
        low = (low << 8) | (uint64_t)p[i];
    }
    if (negative) {
        if (low < 0x8000000000000000ull) {
            return false;
        }
    } else {
        if (low > 0x7FFFFFFFFFFFFFFFull) {
            return false;
        }
    }
    out = (int64_t)low;
    return true;
}

bool bytes_to_u64(const unsigned char* p, size_t n, uint64_t& out)
{
    if (n == 0) {
        return false;
    }
    return mag_fits_u64(p, n, out);
}

void mag_to_dec(const std::vector<unsigned char>& mag,
                std::vector<unsigned>& limbs)
{
    const unsigned long long kBase = 1000000000ull;
    limbs.assign(1, 0u);
    for (size_t i = mag.size(); i-- > 0; ) {
        unsigned long long carry = mag[i];
        for (size_t j = 0; j < limbs.size(); ++j) {
            unsigned long long v = (unsigned long long)limbs[j] * 256ull + carry;
            limbs[j] = (unsigned)(v % kBase);
            carry = v / kBase;
        }
        while (carry != 0) {
            limbs.push_back((unsigned)(carry % kBase));
            carry /= kBase;
        }
    }
    while (limbs.size() > 1 && limbs.back() == 0u) {
        limbs.pop_back();
    }
}

int dec_to_mag(const char* s, size_t n, int& sign,
               std::vector<unsigned char>& mag)
{
    sign = 0;
    mag.clear();
    if (n == 0) {
        return 0;
    }
    size_t i = (s[0] == '-') ? 1 : 0;
    sign = (i == 1) ? 1 : 0;
    if (i >= n) {
        return 0;
    }
    for (size_t k = i; k < n; ++k) {
        if (s[k] < '0' || s[k] > '9') {
            return 0;
        }
    }
    for (size_t k = i; k < n; ++k) {
        unsigned carry = (unsigned)(s[k] - '0');
        for (size_t j = 0; j < mag.size(); ++j) {
            unsigned v = (unsigned)mag[j] * 10u + carry;
            mag[j] = (unsigned char)(v & 0xFFu);
            carry = v >> 8;
        }
        while (carry != 0) {
            mag.push_back((unsigned char)(carry & 0xFFu));
            carry >>= 8;
        }
    }
    while (mag.size() > 1 && mag.back() == 0x00) {
        mag.pop_back();
    }
    return 1;
}

void int_2c_to_dec(const unsigned char* p, size_t n, char* out, size_t cap,
                   size_t* needed)
{
    size_t pos = 0;
    std::vector<unsigned char> mag;
    std::vector<unsigned> limbs;
    bool negative = (p[n - 1] & 0x80) != 0;
    mag_of_int(p, n, negative, mag);
    mag_to_dec(mag, limbs);
    char tmp[16];
    if (negative) {
        if (out && pos < cap) {
            out[pos] = '-';
        }
        ++pos;
    }
    int len = std::snprintf(tmp, sizeof(tmp), "%u", limbs.back());
    for (int i = 0; i < len; ++i) {
        if (out && pos + (size_t)i < cap) {
            out[pos + (size_t)i] = tmp[i];
        }
    }
    pos += (size_t)len;
    for (size_t j = limbs.size() - 1; j-- > 0; ) {
        int l = std::snprintf(tmp, sizeof(tmp), "%09u", limbs[j]);
        for (int i = 0; i < l; ++i) {
            if (out && pos + (size_t)i < cap) {
                out[pos + (size_t)i] = tmp[i];
            }
        }
        pos += (size_t)l;
    }
    if (out && cap > 0) {
        out[pos < cap ? pos : cap - 1] = '\0';
    }
    if (needed) {
        *needed = pos + 1;
    }
}

} 

struct Buf {
    std::atomic<long> refs;
    unsigned char*    ptr;
    size_t            size;
    int               owned;
};

struct bon {
    std::atomic<long> refs;
    Buf*          buf;
    size_t        off;
    size_t        len;
    unsigned char type;
    uint64_t      count;
    int           valid;
};

struct bon_writer {
    struct Frame {
        size_t   len_pos;
        uint64_t count;
        int      is_object;
        int      pending_key;
    };
    std::vector<unsigned char> buf;
    std::vector<Frame>         stack;
    int                        failed;
};

namespace {

Buf* buf_adopt(unsigned char* data, size_t size, int owned)
{
    Buf* b = (Buf*)std::malloc(sizeof(Buf));
    if (!b) {
        if (owned) {
            std::free(data);
        }
        set_err(BON_E_MEMORY, "out of memory");
        return nullptr;
    }
    new (&b->refs) std::atomic<long>(1);
    b->ptr = data;
    b->size = size;
    b->owned = owned;
    return b;
}

Buf* buf_copy(const void* data, size_t size)
{
    unsigned char* copy = nullptr;
    if (size > 0) {
        copy = (unsigned char*)std::malloc(size);
        if (!copy) {
            set_err(BON_E_MEMORY, "out of memory for %llu bytes",
                    (unsigned long long)size);
            return nullptr;
        }
        std::memcpy(copy, data, size);
    }
    return buf_adopt(copy, size, 1);
}

void buf_release(Buf* b)
{
    if (!b) {
        return;
    }
    if (b->refs.fetch_sub(1, std::memory_order_acq_rel) == 1) {
        if (b->owned) {
            std::free(b->ptr);
        }
        std::free(b);
    }
}

bon* node_new(Buf* buf, size_t off, size_t len, unsigned char type,
              uint64_t count)
{
    bon* n = (bon*)std::malloc(sizeof(bon));
    if (!n) {
        set_err(BON_E_MEMORY, "out of memory");
        return nullptr;
    }
    buf->refs.fetch_add(1, std::memory_order_relaxed);
    new (&n->refs) std::atomic<long>(1);
    n->buf = buf;
    n->off = off;
    n->len = len;
    n->type = type;
    n->count = count;
    n->valid = 1;
    return n;
}

void node_free(bon* n)
{
    if (!n) {
        return;
    }
    if (n->refs.fetch_sub(1, std::memory_order_acq_rel) != 1) {
        return;
    }
    Buf* b = n->buf;
    n->buf = nullptr;
    std::free(n);
    buf_release(b);
}

bool parse_tlv(const unsigned char* p, size_t avail, unsigned char& type,
               size_t& total, uint64_t& count)
{
    count = 0;
    total = 0;
    if (avail < 1) {
        return false;
    }
    unsigned char t = p[0];
    type = t;
    switch (t) {
        case BON_NONE:
            total = 1;
            return true;
        case BON_BOOL:
            if (avail < 2) {
                return false;
            }
            total = 2;
            return true;
        case BON_NUMBER: {
            if (avail < 2) {
                return false;
            }
            unsigned char m = p[1];
            size_t w = fixed_mode_size(m);
            if (w != 0) {
                total = 2 + w;
            } else if (m == BON_MODE_VARINT) {
                if (avail < 10) {
                    return false;
                }
                uint64_t n = rd_u64(p + 2);
                if (n > (uint64_t)(avail - 10)) {
                    return false;
                }
                total = 10 + (size_t)n;
            } else {
                return false;
            }
            return total <= avail;
        }
        case BON_FLOAT:
            total = 9;
            return total <= avail;
        case BON_DECIMAL:
            total = 17;
            return total <= avail;
        case BON_STRING:
        case BON_BLOB: {
            if (avail < 9) {
                return false;
            }
            uint64_t n = rd_u64(p + 1);
            if (n > (uint64_t)(avail - 9)) {
                return false;
            }
            total = 9 + (size_t)n;
            return true;
        }
        case BON_ARRAY:
        case BON_OBJECT: {
            if (avail < 17) {
                return false;
            }
            uint64_t n = rd_u64(p + 1);
            uint64_t c = rd_u64(p + 9);
            if (n > (uint64_t)(avail - 17)) {
                return false;
            }
            total = 17 + (size_t)n;
            count = c;
            return true;
        }
        default:
            return false;
    }
}

bon* node_from_tlv(Buf* buf, size_t off, size_t avail)
{
    unsigned char type = 0;
    size_t total = 0;
    uint64_t count = 0;
    if (!parse_tlv(buf->ptr + off, avail, type, total, count)) {
        if (g_err == BON_E_OK) {
            set_err(BON_E_TRUNCATED, "malformed TLV at offset %llu",
                    (unsigned long long)off);
        }
        return nullptr;
    }
    return node_new(buf, off, total, type, count);
}

bool node_check(const bon* n)
{
    if (!n) {
        if (g_err == BON_E_OK) {
            set_err(BON_E_INVALID, "null bon object");
        }
        return false;
    }
    if (!n->valid) {
        set_err(BON_E_INVALID, "bon object is not a valid TLV");
        return false;
    }
    return true;
}

bon* root_of(Buf* buf)
{
    if (buf->size < kMagicLen || std::memcmp(buf->ptr, kMagic, kMagicLen) != 0) {
        set_err(BON_E_MAGIC, "stream does not start with the 'bon' magic");
        return nullptr;
    }
    if (buf->size < kMagicLen + 17) {
        set_err(BON_E_TRUNCATED, "stream is too short to hold a root value");
        return nullptr;
    }
    bon* n = node_from_tlv(buf, kMagicLen, buf->size - kMagicLen);
    if (!n) {
        return nullptr;
    }
    if (n->type != BON_ARRAY && n->type != BON_OBJECT) {
        set_err(BON_E_ROOT, "root type 0x%02X must be ARRAY or OBJECT",
                (unsigned)n->type);
        node_free(n);
        return nullptr;
    }
    if (n->off + n->len != buf->size) {
        set_err(BON_E_LENGTH, "%llu trailing byte(s) after the root value",
                (unsigned long long)(buf->size - n->off - n->len));
        node_free(n);
        return nullptr;
    }
    return n;
}

bon* child_at(const bon* n, size_t index, int which)
{
    if (!node_check(n)) {
        return nullptr;
    }
    if (!is_container(n->type)) {
        set_err(BON_E_TYPE, "%s has no child nodes", bon_type_name(n));
        return nullptr;
    }
    if ((uint64_t)index >= n->count) {
        set_err(BON_E_RANGE, "index %llu out of range (count %llu)",
                (unsigned long long)index, (unsigned long long)n->count);
        return nullptr;
    }
    size_t step = (n->type == BON_ARRAY) ? 1u : 2u;
    if (which < 0 || (size_t)which >= step) {
        set_err(BON_E_TYPE, "%s has no child %d", bon_type_name(n), which);
        return nullptr;
    }
    size_t want = index * step + (size_t)which;
    size_t pos = n->off + 17;
    size_t end = n->off + n->len;
    for (size_t k = 0; k < want; ++k) {
        unsigned char type = 0;
        size_t total = 0;
        uint64_t count = 0;
        if (!parse_tlv(n->buf->ptr + pos, end - pos, type, total, count)) {
            set_err(BON_E_LENGTH, "child %llu of %s is malformed",
                    (unsigned long long)k, bon_type_name(n));
            return nullptr;
        }
        pos += total;
    }
    unsigned char type = 0;
    size_t total = 0;
    uint64_t count = 0;
    if (!parse_tlv(n->buf->ptr + pos, end - pos, type, total, count)) {
        set_err(BON_E_LENGTH, "child %llu of %s is malformed",
                (unsigned long long)index, bon_type_name(n));
        return nullptr;
    }
    if (pos + total > end) {
        set_err(BON_E_LENGTH, "child %llu of %s overruns the container",
                (unsigned long long)index, bon_type_name(n));
        return nullptr;
    }
    return node_new(n->buf, pos, total, type, count);
}

bool key_matches(const bon* key, const void* name, size_t name_size)
{
    if (key->type != BON_STRING) {
        return false;
    }
    size_t n = key->len - 9;
    if (n != name_size) {
        return false;
    }
    return n == 0 || std::memcmp(key->buf->ptr + key->off + 9, name, n) == 0;
}

/* Validates a single TLV in place: header, payload invariants and, for
 * containers, that total_len agrees with the header.  Containers are not
 * descended into here -- validate_tree() walks them with an explicit heap
 * stack, so one level of nesting costs one small cursor instead of one C stack
 * frame and the format itself imposes no depth limit.  *total_out receives the
 * node's byte length and *count_out its element / pair count. */
bool validate_tlv(const unsigned char* p, size_t avail, size_t* total_out,
                  uint64_t* count_out)
{
    unsigned char type = 0;
    size_t total = 0;
    uint64_t count = 0;
    if (!parse_tlv(p, avail, type, total, count)) {
        set_err(BON_E_TRUNCATED, "malformed TLV");
        return false;
    }
    if (total > avail) {
        set_err(BON_E_LENGTH, "value overruns its parent");
        return false;
    }
    *total_out = total;
    *count_out = count;
    switch (type) {
        case BON_NONE:
        case BON_FLOAT:
        case BON_DECIMAL:
            return true;
        case BON_BOOL:
            if (p[1] > 1) {
                set_err(BON_E_LENGTH, "BOOL payload is not 0 or 1");
                return false;
            }
            return true;
        case BON_NUMBER: {
            unsigned char m = p[1];
            if (m == BON_MODE_VARINT) {
                uint64_t n = rd_u64(p + 2);
                if (n == 0) {
                    set_err(BON_E_LENGTH, "varint payload is empty");
                    return false;
                }
                if (trim_2c(p + 10, (size_t)n) != (size_t)n) {
                    set_err(BON_E_MODE, "varint payload is not minimal");
                    return false;
                }
                return true;
            }
            if (fixed_mode_size(m) == 0) {
                set_err(BON_E_MODE, "reserved number mode 0x%02X", (unsigned)m);
                return false;
            }
            return true;
        }
        case BON_STRING:
        case BON_BLOB:
            if ((uint64_t)(total - 9) != rd_u64(p + 1)) {
                set_err(BON_E_LENGTH, "length field does not match the value");
                return false;
            }
            return true;
        case BON_ARRAY:
        case BON_OBJECT:
            if ((uint64_t)(total - 17) != rd_u64(p + 1)) {
                set_err(BON_E_LENGTH, "total_len does not match the container");
                return false;
            }
            return true;
        default:
            set_err(BON_E_TYPE, "unknown type 0x%02X", (unsigned)type);
            return false;
    }
}

bool validate_tree(const unsigned char* p, size_t avail)
{
    /* One cursor per open container.  An OBJECT is walked TLV by TLV like any
     * other container, which is why its budget is twice the pair count: a key
     * is validated and descended into exactly like a value. */
    struct Cursor {
        const unsigned char* base;   /* the container node */
        size_t               pos;    /* next byte to read inside it */
        size_t               end;    /* container length */
        uint64_t             left;   /* TLVs still expected */
    };
    std::vector<Cursor> stack;

    const unsigned char* node = p;   /* value waiting to be validated */
    size_t room = avail;
    size_t from = (size_t)-1;        /* cursor it was taken from, or none */

    for (;;) {
        size_t total = 0;
        uint64_t count = 0;
        if (!validate_tlv(node, room, &total, &count)) {
            return false;
        }
        if (from != (size_t)-1) {
            Cursor& owner = stack[from];
            owner.pos += total;
            --owner.left;
        }
        unsigned char type = node[0];
        if (type == BON_ARRAY || type == BON_OBJECT) {
            Cursor cur;
            cur.base = node;
            cur.pos = 17;
            cur.end = total;
            cur.left = (type == BON_OBJECT) ? ((count > (~(uint64_t)0) / 2)
                                               ? ~(uint64_t)0
                                               : count * 2)
                                            : count;
            try {
                stack.push_back(cur);
            } catch (...) {
                set_err(BON_E_MEMORY, "out of memory");
                return false;
            }
        }

        /* Pull the next value out of the innermost container that still owes
         * one, unwinding finished containers on the way. */
        for (;;) {
            if (stack.empty()) {
                return true;
            }
            Cursor& c = stack.back();
            if (c.left == 0) {
                if (c.pos != c.end) {
                    set_err(BON_E_LENGTH, "container holds more bytes than its "
                                          "count accounts for");
                    return false;
                }
                stack.pop_back();
                continue;
            }
            if (c.pos >= c.end) {
                set_err(BON_E_LENGTH, "container holds fewer values than its "
                                      "count says");
                return false;
            }
            node = c.base + c.pos;
            room = c.end - c.pos;
            from = stack.size() - 1;
            break;
        }
    }
}

void json_escape(const unsigned char* p, size_t n, std::vector<char>& out)
{
    static const char kHex[] = "0123456789ABCDEF";
    out.push_back('"');
    for (size_t i = 0; i < n; ++i) {
        unsigned char c = p[i];
        switch (c) {
            case '"':  out.push_back('\\'); out.push_back('"');  break;
            case '\\': out.push_back('\\'); out.push_back('\\'); break;
            case '\b': out.push_back('\\'); out.push_back('b');  break;
            case '\f': out.push_back('\\'); out.push_back('f');  break;
            case '\n': out.push_back('\\'); out.push_back('n');  break;
            case '\r': out.push_back('\\'); out.push_back('r');  break;
            case '\t': out.push_back('\\'); out.push_back('t');  break;
            default:
                if (c < 0x20) {
                    out.push_back('\\');
                    out.push_back('u');
                    out.push_back('0');
                    out.push_back('0');
                    out.push_back(kHex[(c >> 4) & 0x0F]);
                    out.push_back(kHex[c & 0x0F]);
                } else {
                    out.push_back((char)c);
                }
        }
    }
    out.push_back('"');
}

/* JSON debug rendering.  A single explicit stack drives the whole traversal:
 * every open container costs one frame, never a C stack frame, so a document
 * nested a million levels deep still prints.  Scalars go straight to the FILE*,
 * nothing is buffered whole.
 *
 * A non-STRING object key is wrapped as "#bon:<TYPE>:"<value>"; when such a key
 * is itself a container the closing quote has to wait for the whole container,
 * which is what the quote flag on the frame arranges -- the key's frames sit
 * above the object's frame, so they always finish first. */
enum { kJsonKey = 0, kJsonValue = 1 };

struct JsonFrame {
    bon*      node;    /* the container being walked */
    size_t    index;   /* next child (or pair) to emit */
    unsigned  state;   /* kJsonKey / kJsonValue, objects only */
    bool      owns;    /* free node when the frame pops */
    bool      quote;   /* close a "#bon:" key wrapper after this container */
};

bool json_scalar(FILE* f, const bon* n)
{
    const unsigned char* p = n->buf->ptr + n->off;
    switch (n->type) {
        case BON_NONE:
            return std::fputs("null", f) >= 0;
        case BON_BOOL:
            return std::fputs(p[1] ? "true" : "false", f) >= 0;
        case BON_NUMBER: {
            size_t need = 0;
            if (bon_get_int_str(n, nullptr, 0, &need) != 0) {
                return false;
            }
            std::vector<char> text(need);
            if (bon_get_int_str(n, &text[0], need, &need) != 0) {
                return false;
            }
            return std::fputs(text.data(), f) >= 0;
        }
        case BON_FLOAT: {
            uint64_t raw = rd_u64(p + 1);
            double d;
            std::memcpy(&d, &raw, sizeof(d));
            return std::fprintf(f, "%.17g", d) >= 0;
        }
        case BON_STRING:
            g_json.clear();
            json_escape(p + 9, n->len - 9, g_json);
            return std::fwrite(g_json.data(), 1, g_json.size(), f) == g_json.size();
        case BON_BLOB: {
            if (std::fputs("\"#bon:blob:", f) < 0) {
                return false;
            }
            std::fprintf(f, "%llu:", (unsigned long long)(n->len - 9));
            for (size_t i = 0; i < n->len - 9; ++i) {
                std::fprintf(f, "%02x", p[9 + i]);
            }
            return std::fputc('"', f) >= 0;
        }
        case BON_DECIMAL: {
            int64_t coeff = 0;
            uint64_t scale = 0;
            if (bon_get_decimal(n, &coeff, &scale) != 0) {
                return false;
            }
            return std::fprintf(f, "\"#bon:decimal:%lldE-%llu\"",
                                (long long)coeff,
                                (unsigned long long)scale) >= 0;
        }
        default:
            set_err(BON_E_TYPE, "unknown type 0x%02X", (unsigned)n->type);
            return false;
    }
}

bool json_push(FILE* f, bon* n, bool owns, bool quote,
               std::vector<JsonFrame>& stack)
{
    JsonFrame fr;
    fr.node = n;
    fr.index = 0;
    fr.state = kJsonKey;
    fr.owns = owns;
    fr.quote = quote;
    if (std::fputc(n->type == BON_OBJECT ? '{' : '[', f) < 0) {
        if (owns) {
            node_free(n);
        }
        return false;
    }
    try {
        stack.push_back(fr);
    } catch (...) {
        if (owns) {
            node_free(n);
        }
        set_err(BON_E_MEMORY, "out of memory");
        return false;
    }
    return true;
}

bool json_write(FILE* f, const bon* root)
{
    std::vector<JsonFrame> stack;
    bon* pending = const_cast<bon*>(root);   /* value waiting to be emitted */
    bool pending_owns = false;
    bool pending_quote = false;
    bool ok = true;
    bool done = false;

    while (ok && !done) {
        if (pending) {
            bon* n = pending;
            bool owns = pending_owns;
            bool quote = pending_quote;
            pending = NULL;
            if (!node_check(n)) {
                if (owns) {
                    node_free(n);
                }
                ok = false;
                break;
            }
            if (n->type == BON_ARRAY || n->type == BON_OBJECT) {
                if (!json_push(f, n, owns, quote, stack)) {
                    ok = false;
                    break;
                }
            } else {
                ok = json_scalar(f, n);
                if (ok && quote) {
                    ok = std::fputc('"', f) >= 0;
                }
                if (owns) {
                    node_free(n);
                }
                if (!ok) {
                    break;
                }
            }
        }

        /* Emit whatever the innermost open container owes us next. */
        while (ok && !done) {
            if (stack.empty()) {
                done = true;
                break;
            }
            JsonFrame& fr = stack.back();
            if (fr.index >= fr.node->count) {
                unsigned char t = fr.node->type;
                bon* n = fr.node;
                bool owns = fr.owns;
                bool quote = fr.quote;
                stack.pop_back();
                ok = std::fputc(t == BON_OBJECT ? '}' : ']', f) >= 0;
                if (ok && quote) {
                    ok = std::fputc('"', f) >= 0;
                }
                if (owns) {
                    node_free(n);
                }
                continue;
            }
            if (fr.node->type == BON_OBJECT) {
                if (fr.state == kJsonKey) {
                    if (fr.index != 0 && std::fputc(',', f) < 0) {
                        ok = false;
                        break;
                    }
                    bon* key = child_at(fr.node, fr.index, 0);
                    if (!key) {
                        ok = false;
                        break;
                    }
                    fr.state = kJsonValue;
                    if (key->type == BON_STRING) {
                        g_json.clear();
                        json_escape(key->buf->ptr + key->off + 9,
                                    key->len - 9, g_json);
                        ok = std::fwrite(g_json.data(), 1, g_json.size(), f)
                             == g_json.size();
                        node_free(key);
                        continue;
                    }
                    if (std::fputs("\"#bon:", f) < 0 ||
                        std::fputs(bon_type_name(key), f) < 0 ||
                        std::fputc(':', f) < 0) {
                        node_free(key);
                        ok = false;
                        break;
                    }
                    pending = key;            /* may be a container: quote closes late */
                    pending_owns = true;
                    pending_quote = true;
                    break;
                }
                if (std::fputc(':', f) < 0) {
                    ok = false;
                    break;
                }
                bon* val = child_at(fr.node, fr.index, 1);
                if (!val) {
                    ok = false;
                    break;
                }
                ++fr.index;
                fr.state = kJsonKey;
                pending = val;
                pending_owns = true;
                pending_quote = false;
                break;
            }
            if (fr.index != 0 && std::fputc(',', f) < 0) {
                ok = false;
                break;
            }
            bon* item = child_at(fr.node, fr.index, 0);
            if (!item) {
                ok = false;
                break;
            }
            ++fr.index;
            pending = item;
            pending_owns = true;
            pending_quote = false;
            break;
        }
    }

    for (size_t i = 0; i < stack.size(); ++i) {
        if (stack[i].owns) {
            node_free(stack[i].node);
        }
    }
    return ok;
}

bool writer_ok(bon_writer* w)
{
    if (!w) {
        if (g_err == BON_E_OK) {
            set_err(BON_E_INVALID, "null bon writer");
        }
        return false;
    }
    if (w->failed) {
        if (g_err == BON_E_OK) {
            set_err(BON_E_STATE, "bon writer is in a failed state");
        }
        return false;
    }
    return true;
}

void writer_fail(bon_writer* w, int code, const char* fmt, ...)
{
    w->failed = 1;
    g_err = code;
    va_list ap;
    va_start(ap, fmt);
    std::vsnprintf(g_msg, sizeof(g_msg), fmt, ap);
    va_end(ap);
}

/* A writer that can no longer produce a document stays alive so the caller can
 * bon_writer_free() it, but it can never be finished: the buffers are dropped
 * and every further write fails with BON_E_STATE. */
void writer_poison(bon_writer* w)
{
    w->failed = 1;
    w->stack.clear();
    std::vector<unsigned char>().swap(w->buf);
}

void writer_put(bon_writer* w, unsigned char c)
{
    try {
        w->buf.push_back(c);
    } catch (...) {
        writer_fail(w, BON_E_MEMORY, "out of memory");
    }
}

void writer_put_u64(bon_writer* w, uint64_t v)
{
    unsigned char tmp[8];
    wr_u64(tmp, v);
    try {
        w->buf.insert(w->buf.end(), tmp, tmp + 8);
    } catch (...) {
        writer_fail(w, BON_E_MEMORY, "out of memory");
    }
}

void writer_put_bytes(bon_writer* w, const void* p, size_t n)
{
    if (n == 0 || w->failed) {
        return;
    }
    try {
        const unsigned char* b = (const unsigned char*)p;
        w->buf.insert(w->buf.end(), b, b + n);
    } catch (...) {
        writer_fail(w, BON_E_MEMORY, "out of memory");
    }
}

void writer_begin(bon_writer* w, unsigned char type)
{
    if (!writer_ok(w)) {
        return;
    }
    size_t pos = w->buf.size();
    writer_put(w, type);
    writer_put_u64(w, 0);
    writer_put_u64(w, 0);
    if (w->failed) {
        return;
    }
    bon_writer::Frame f;
    f.len_pos = pos + 1;
    f.count = 0;
    f.is_object = (type == BON_OBJECT);
    f.pending_key = 0;
    try {
        w->stack.push_back(f);
    } catch (...) {
        writer_fail(w, BON_E_MEMORY, "out of memory");
    }
}

void writer_finish_value(bon_writer* w)
{
    if (w->stack.empty()) {
        return;
    }
    bon_writer::Frame& f = w->stack.back();
    if (f.is_object) {
        if (f.pending_key == 0) {
            ++f.count;
            f.pending_key = 1;
        } else {
            f.pending_key = 0;
        }
    } else {
        ++f.count;
    }
}

void writer_end(bon_writer* w, unsigned char type)
{
    if (!writer_ok(w)) {
        return;
    }
    if (w->stack.size() < 2) {
        writer_fail(w, BON_E_STATE, "unbalanced %s end",
                    type == BON_ARRAY ? "array" : "object");
        return;
    }
    bon_writer::Frame f = w->stack.back();
    if ((f.is_object ? BON_OBJECT : BON_ARRAY) != (int)type) {
        writer_fail(w, BON_E_STATE, "unbalanced container end");
        return;
    }
    w->stack.pop_back();
    if (f.is_object && f.pending_key != 0) {
        writer_fail(w, BON_E_STATE, "object key without a value");
        return;
    }
    wr_u64(&w->buf[f.len_pos], (uint64_t)(w->buf.size() - (f.len_pos + 16)));
    wr_u64(&w->buf[f.len_pos + 8], f.count);
    writer_finish_value(w);
}

void writer_atomic(bon_writer* w, unsigned char type)
{
    if (!writer_ok(w)) {
        return;
    }
    writer_put(w, type);
    writer_finish_value(w);
}

void writer_len_prefixed(bon_writer* w, unsigned char type, const void* data,
                         size_t size)
{
    if (!writer_ok(w)) {
        return;
    }
    writer_put(w, type);
    writer_put_u64(w, (uint64_t)size);
    writer_put_bytes(w, data, size);
    writer_finish_value(w);
}

void writer_fixed_int(bon_writer* w, unsigned char mode, uint64_t stored)
{
    writer_put(w, BON_NUMBER);
    writer_put(w, mode);
    unsigned char tmp[8];
    wr_u64(tmp, stored);
    writer_put_bytes(w, tmp, fixed_mode_width(mode));
    writer_finish_value(w);
}

void writer_int_bytes(bon_writer* w, const void* data, size_t size,
                      int force_varint)
{
    if (!writer_ok(w)) {
        return;
    }
    std::vector<unsigned char> p;
    try {
        if (size == 0) {
            p.assign(1, 0x00);
        } else {
            if (!data) {
                writer_fail(w, BON_E_USAGE, "null integer payload");
                return;
            }
            p.assign((const unsigned char*)data,
                     (const unsigned char*)data + size);
            shrink_2c(p);
        }
        if (!force_varint) {
            std::vector<unsigned char> mag;
            uint64_t m = 0;
            mag_of_int(p.data(), p.size(), (p[p.size() - 1] & 0x80) != 0, mag);
            if (mag_fits_u64(mag.data(), mag.size(), m)) {
                if ((p[p.size() - 1] & 0x80) != 0) {
                    writer_fixed_int(w, pick_neg_mode(m - 1), m - 1);
                } else {
                    writer_fixed_int(w, pick_pos_mode(m), m);
                }
                return;
            }
        }
    } catch (...) {
        writer_fail(w, BON_E_MEMORY, "out of memory");
        return;
    }
    writer_put(w, BON_NUMBER);
    writer_put(w, BON_MODE_VARINT);
    writer_put_u64(w, (uint64_t)p.size());
    writer_put_bytes(w, p.data(), p.size());
    writer_finish_value(w);
}

} 

extern "C" {

const char* bon_version(void)
{
    return BON_VERSION_STRING;
}

int bon_last_error_code(void)
{
    return g_err;
}

const char* bon_last_error(void)
{
    return g_err == BON_E_OK ? nullptr : g_msg;
}

void bon_clear_error(void)
{
    clear_err();
}

const char* bon_error_message(int code)
{
    switch (code) {
        case BON_E_OK:        return "ok";
        case BON_E_INVALID:   return "invalid bon object";
        case BON_E_MAGIC:     return "bad magic number";
        case BON_E_TRUNCATED: return "truncated data";
        case BON_E_TYPE:      return "type mismatch";
        case BON_E_MODE:      return "reserved or unknown number mode";
        case BON_E_LENGTH:    return "length field inconsistency";
        case BON_E_ROOT:      return "root type must be ARRAY or OBJECT";
        case BON_E_RANGE:     return "value out of range";
        case BON_E_NOKEY:     return "key not found";
        case BON_E_DEPTH:     return "maximum nesting depth exceeded";  /* reserved: the format has no depth limit */
        case BON_E_STATE:     return "invalid writer state";
        case BON_E_IO:        return "input/output error";
        case BON_E_MEMORY:    return "out of memory";
        case BON_E_USAGE:     return "invalid usage";
        default:              return "unknown error";
    }
}

bon* bon_from_stream(const void* bytes, size_t size)
{
    clear_err();
    if (!bytes) {
        set_err(BON_E_INVALID, "null stream");
        return nullptr;
    }
    Buf* b = buf_copy(bytes, size);
    if (!b) {
        return nullptr;
    }
    bon* n = root_of(b);
    buf_release(b);
    return n;
}

bon* bon_from_stream_ref(const void* bytes, size_t size)
{
    clear_err();
    if (!bytes) {
        set_err(BON_E_INVALID, "null stream");
        return nullptr;
    }
    Buf* b = buf_adopt((unsigned char*)bytes, size, 0);
    if (!b) {
        return nullptr;
    }
    bon* n = root_of(b);
    buf_release(b);
    return n;
}

bon* bon_from_file(const char* path)
{
    clear_err();
    if (!path) {
        set_err(BON_E_INVALID, "null path");
        return nullptr;
    }
    FILE* f = std::fopen(path, "rb");
    if (!f) {
        set_err(BON_E_IO, "cannot open '%s'", path);
        return nullptr;
    }
    std::vector<unsigned char> data;
    unsigned char chunk[65536];
    size_t got;
    while ((got = std::fread(chunk, 1, sizeof(chunk), f)) > 0) {
        data.insert(data.end(), chunk, chunk + got);
    }
    bool bad = std::ferror(f) != 0;
    std::fclose(f);
    if (bad) {
        set_err(BON_E_IO, "read error on '%s'", path);
        return nullptr;
    }
    static const char kEmpty[1] = { 0 };
    const void* bytes = data.empty() ? (const void*)kEmpty : (const void*)data.data();
    return bon_from_stream(bytes, data.size());
}

bon* bon_new_array(void)
{
    bon_writer* w = bon_writer_new(BON_ARRAY);
    if (!w) {
        return nullptr;
    }
    bon* n = bon_writer_finish(w);
    if (!n) {
        bon_writer_free(w);
    }
    return n;
}

bon* bon_new_object(void)
{
    bon_writer* w = bon_writer_new(BON_OBJECT);
    if (!w) {
        return nullptr;
    }
    bon* n = bon_writer_finish(w);
    if (!n) {
        bon_writer_free(w);
    }
    return n;
}

int bon_save_file(const bon* node, const char* path)
{
    clear_err();
    if (!node || !path) {
        set_err(BON_E_INVALID, "null argument");
        return -1;
    }
    if (node->off != kMagicLen) {
        set_err(BON_E_STATE, "only a root bon object can be saved as a file");
        return -1;
    }
    const void* bytes = nullptr;
    size_t size = 0;
    if (bon_stream(node, &bytes, &size) != 0) {
        return -1;
    }
    FILE* f = std::fopen(path, "wb");
    if (!f) {
        set_err(BON_E_IO, "cannot create '%s'", path);
        return -1;
    }
    size_t wrote = std::fwrite(bytes, 1, size, f);
    bool bad = (wrote != size) || std::fflush(f) != 0;
    if (std::fclose(f) != 0) {
        bad = true;
    }
    if (bad) {
        set_err(BON_E_IO, "write error on '%s'", path);
        return -1;
    }
    return 0;
}

bon* bon_ref(bon* node)
{
    if (!node) {
        clear_err();
        set_err(BON_E_INVALID, "null bon object");
        return nullptr;
    }
    node->refs.fetch_add(1, std::memory_order_relaxed);
    return node;
}

void bon_unref(bon* node)
{
    node_free(node);
}

void bon_free(void* node)
{
    node_free((bon*)node);
}

bon* bon_root(bon* node)
{
    clear_err();
    if (!node) {
        set_err(BON_E_INVALID, "null bon object");
        return nullptr;
    }
    if (node->off != kMagicLen) {
        set_err(BON_E_STATE, "bon object is not a root object");
        return nullptr;
    }
    return bon_ref(node);
}

int bon_is_root(const bon* node)
{
    return (node && node->off == kMagicLen) ? 1 : 0;
}

const void* bon_data(const bon* node)
{
    if (!node) {
        clear_err();
        set_err(BON_E_INVALID, "null bon object");
        return nullptr;
    }
    return node->buf->ptr + node->off;
}

size_t bon_size(const bon* node)
{
    if (!node) {
        clear_err();
        set_err(BON_E_INVALID, "null bon object");
        return 0;
    }
    return node->len;
}

int bon_stream(const bon* node, const void** bytes, size_t* size)
{
    clear_err();
    if (!node || !bytes || !size) {
        set_err(BON_E_INVALID, "null argument");
        return -1;
    }
    if (node->off != kMagicLen) {
        set_err(BON_E_STATE, "bon object is not a root object");
        return -1;
    }
    *bytes = node->buf->ptr;
    *size = node->buf->size;
    return 0;
}

const void* bon_magic(const bon* node, size_t* size)
{
    if (!node) {
        clear_err();
        set_err(BON_E_INVALID, "null bon object");
        return nullptr;
    }
    if (size) {
        *size = kMagicLen;
    }
    return kMagic;
}

int bon_type(const bon* node)
{
    if (!node) {
        clear_err();
        set_err(BON_E_INVALID, "null bon object");
        return -1;
    }
    return node->type;
}

const char* bon_type_name(const bon* node)
{
    if (!node) {
        return "null";
    }
    switch (node->type) {
        case BON_NONE:    return "NONE";
        case BON_BOOL:    return "BOOL";
        case BON_NUMBER:  return "NUMBER";
        case BON_FLOAT:   return "FLOAT";
        case BON_STRING:  return "STRING";
        case BON_ARRAY:   return "ARRAY";
        case BON_OBJECT:  return "OBJECT";
        case BON_BLOB:    return "BLOB";
        case BON_DECIMAL: return "DECIMAL";
        default:          return "UNKNOWN";
    }
}

int bon_valid(const bon* node)
{
    return (node && node->valid) ? 1 : 0;
}

size_t bon_count(const bon* node)
{
    if (!node) {
        clear_err();
        set_err(BON_E_INVALID, "null bon object");
        return 0;
    }
    if (!is_container(node->type)) {
        set_err(BON_E_TYPE, "%s is not a container", bon_type_name(node));
        return 0;
    }
    return (size_t)node->count;
}

int bon_validate(const bon* node)
{
    clear_err();
    if (!node) {
        set_err(BON_E_INVALID, "null bon object");
        return -1;
    }
    if (node->off == kMagicLen) {
        if (node->buf->size < kMagicLen
            || std::memcmp(node->buf->ptr, kMagic, kMagicLen) != 0) {
            set_err(BON_E_MAGIC, "bad magic");
            return -1;
        }
        if (node->type != BON_ARRAY && node->type != BON_OBJECT) {
            set_err(BON_E_ROOT, "root type must be ARRAY or OBJECT");
            return -1;
        }
        if (node->off + node->len != node->buf->size) {
            set_err(BON_E_LENGTH, "trailing bytes after the root value");
            return -1;
        }
    }
    if (!validate_tree(node->buf->ptr + node->off, node->len)) {
        return -1;
    }
    return 0;
}

bon* bon_at(const bon* node, size_t index)
{
    clear_err();
    if (!node) {
        set_err(BON_E_INVALID, "null bon object");
        return nullptr;
    }
    if (!is_container(node->type)) {
        set_err(BON_E_TYPE, "%s has no child nodes", bon_type_name(node));
        return nullptr;
    }
    return child_at(node, index, node->type == BON_OBJECT ? 1 : 0);
}

bon* bon_key_at(const bon* node, size_t index)
{
    clear_err();
    if (!node) {
        set_err(BON_E_INVALID, "null bon object");
        return nullptr;
    }
    return child_at(node, index, 0);
}

bon* bon_value_at(const bon* node, size_t index)
{
    clear_err();
    if (!node) {
        set_err(BON_E_INVALID, "null bon object");
        return nullptr;
    }
    return child_at(node, index, 1);
}

bon* bon_find(const bon* node, const void* key, size_t key_size)
{
    clear_err();
    if (!node) {
        set_err(BON_E_INVALID, "null bon object");
        return nullptr;
    }
    if (node->type != BON_OBJECT) {
        set_err(BON_E_TYPE, "%s is not an OBJECT", bon_type_name(node));
        return nullptr;
    }
    for (size_t i = 0; i < (size_t)node->count; ++i) {
        bon* k = child_at(node, i, 0);
        if (!k) {
            return nullptr;
        }
        bool hit = key_matches(k, key, key_size);
        node_free(k);
        if (hit) {
            return child_at(node, i, 1);
        }
    }
    set_err(BON_E_NOKEY, "key not found");
    return nullptr;
}

size_t bon_key_count(const bon* node, const void* key, size_t key_size)
{
    clear_err();
    if (!node) {
        set_err(BON_E_INVALID, "null bon object");
        return 0;
    }
    if (node->type != BON_OBJECT) {
        set_err(BON_E_TYPE, "%s is not an OBJECT", bon_type_name(node));
        return 0;
    }
    size_t n = 0;
    for (size_t i = 0; i < (size_t)node->count; ++i) {
        bon* k = child_at(node, i, 0);
        if (!k) {
            return n;
        }
        if (key_matches(k, key, key_size)) {
            ++n;
        }
        node_free(k);
    }
    return n;
}

int bon_key_is(const bon* node, size_t index, const void* key, size_t key_size)
{
    clear_err();
    if (!node) {
        set_err(BON_E_INVALID, "null bon object");
        return -1;
    }
    bon* k = child_at(node, index, 0);
    if (!k) {
        return -1;
    }
    int r = key_matches(k, key, key_size) ? 1 : 0;
    node_free(k);
    return r;
}

const char* bon_key_cstr(const bon* node, size_t index, size_t* size)
{
    clear_err();
    if (!node) {
        set_err(BON_E_INVALID, "null bon object");
        return nullptr;
    }
    bon* k = child_at(node, index, 0);
    if (!k) {
        return nullptr;
    }
    if (k->type != BON_STRING) {
        set_err(BON_E_TYPE, "key %llu is %s, not STRING",
                (unsigned long long)index, bon_type_name(k));
        node_free(k);
        return nullptr;
    }
    std::vector<char>& s = scratch_next();
    s.assign((const char*)k->buf->ptr + k->off + 9,
             (const char*)k->buf->ptr + k->off + 9 + k->len - 9);
    s.push_back('\0');
    node_free(k);
    if (size) {
        *size = s.size() - 1;
    }
    return s.data();
}

void bon_iter_init(bon_iter* it, bon* node, int which)
{
    clear_err();
    if (!it) {
        set_err(BON_E_INVALID, "null iterator");
        return;
    }
    it->owner = nullptr;
    it->index = 0;
    it->pos = 0;
    it->end = 0;
    it->step = 1;
    it->which = which;
    if (!node) {
        set_err(BON_E_INVALID, "null bon object");
        return;
    }
    if (!is_container(node->type)) {
        set_err(BON_E_TYPE, "%s is not a container", bon_type_name(node));
        return;
    }
    if (which != 0 && which != 1) {
        set_err(BON_E_USAGE, "which must be 0 or 1");
        return;
    }
    if (which == 1 && node->type != BON_OBJECT) {
        set_err(BON_E_TYPE, "value iteration requires an OBJECT");
        return;
    }
    it->owner = bon_ref(node);
    it->pos = node->off + 17;
    it->end = node->off + node->len;
    it->step = (node->type == BON_ARRAY) ? 1 : 2;
}

int iter_take(bon_iter* it, size_t start, size_t skip, bon** item)
{
    size_t pos = start;
    for (size_t k = 0; k < skip; ++k) {
        unsigned char type = 0;
        size_t total = 0;
        uint64_t count = 0;
        if (!parse_tlv(it->owner->buf->ptr + pos, it->end - pos, type, total,
                       count)) {
            set_err(BON_E_LENGTH, "iterator reached a malformed node");
            return -1;
        }
        pos += total;
    }
    bon* n = node_from_tlv(it->owner->buf, pos, it->end - pos);
    if (!n) {
        return -1;
    }
    it->pos = pos + n->len;
    *item = n;
    return 1;
}

int bon_iter_next(bon_iter* it, bon** item)
{
    clear_err();
    if (!it || !item || !it->owner) {
        set_err(BON_E_USAGE, "iterator is not initialized");
        return -1;
    }
    *item = nullptr;
    if (it->which == 1 && it->step == 1) {
        return 0;
    }
    if (it->index >= (size_t)it->owner->count) {
        return 0;
    }
    bon* n = nullptr;
    size_t start = it->pos;
    size_t skip = 0;
    if (it->which == 1) {
        start = it->owner->off + 17;
        skip = it->index * 2u + 1u;
    }
    int r = iter_take(it, start, skip, &n);
    if (r != 1) {
        return r;
    }
    it->index += 1;
    *item = n;
    return 1;
}

int bon_iter_key_next(bon_iter* it, bon** key, bon** value)
{
    clear_err();
    if (!it || !key || !value) {
        set_err(BON_E_USAGE, "null argument");
        return -1;
    }
    *key = nullptr;
    *value = nullptr;
    if (!it->owner) {
        set_err(BON_E_USAGE, "iterator is not initialized");
        return -1;
    }
    if (it->owner->type != BON_OBJECT) {
        set_err(BON_E_TYPE, "pair iteration requires an OBJECT");
        return -1;
    }
    if (it->index >= (size_t)it->owner->count) {
        return 0;
    }
    bon* k = nullptr;
    int r = iter_take(it, it->pos, 0, &k);
    if (r != 1) {
        return r;
    }
    bon* v = nullptr;
    r = iter_take(it, it->pos, 0, &v);
    if (r != 1) {
        node_free(k);
        set_err(BON_E_LENGTH, "object key without a value");
        return -1;
    }
    it->index += 1;
    *key = k;
    *value = v;
    return 1;
}

size_t bon_iter_index(const bon_iter* it)
{
    /* 0-based index of the item just returned, so it can be handed straight
     * back to bon_at()/bon_key_at()/bon_value_at().  0 before the first item
     * and the last index once the iteration is exhausted. */
    return (it && it->index) ? it->index - 1 : 0;
}

void bon_iter_done(bon_iter* it)
{
    if (it && it->owner) {
        bon* o = it->owner;
        it->owner = nullptr;
        node_free(o);
    }
}

int bon_get_bool(const bon* node)
{
    clear_err();
    if (!node) {
        set_err(BON_E_INVALID, "null bon object");
        return -1;
    }
    if (node->type != BON_BOOL) {
        set_err(BON_E_TYPE, "%s is not a BOOL", bon_type_name(node));
        return -1;
    }
    return node->buf->ptr[node->off + 1] ? 1 : 0;
}

int bon_get_int64(const bon* node, int64_t* out)
{
    clear_err();
    if (!node || !out) {
        set_err(BON_E_INVALID, "null argument");
        return -1;
    }
    if (node->type != BON_NUMBER) {
        set_err(BON_E_TYPE, "%s is not a NUMBER", bon_type_name(node));
        return -1;
    }
    const unsigned char* p = node->buf->ptr + node->off;
    unsigned char mode = p[1];
    if (mode == BON_MODE_VARINT) {
        uint64_t n = rd_u64(p + 2);
        if (!bytes_to_i64(p + 10, (size_t)n, *out)) {
            set_err(BON_E_RANGE, "integer does not fit in int64");
            return -1;
        }
        return 0;
    }
    size_t w = fixed_mode_size(mode);
    if (w == 0) {
        set_err(BON_E_MODE, "reserved number mode 0x%02X", (unsigned)mode);
        return -1;
    }
    uint64_t stored = 0;
    for (size_t i = w; i-- > 0; ) {
        stored = (stored << 8) | (uint64_t)p[2 + i];
    }
    if (mode <= BON_MODE_UINT64) {
        if (stored > (uint64_t)INT64_MAX) {
            set_err(BON_E_RANGE, "value does not fit in int64");
            return -1;
        }
        *out = (int64_t)stored;
    } else {
        if (stored > (uint64_t)INT64_MAX) {
            set_err(BON_E_RANGE, "value does not fit in int64");
            return -1;
        }
        *out = -(int64_t)stored - 1;
    }
    return 0;
}

int bon_get_uint64(const bon* node, uint64_t* out)
{
    clear_err();
    if (!node || !out) {
        set_err(BON_E_INVALID, "null argument");
        return -1;
    }
    if (node->type != BON_NUMBER) {
        set_err(BON_E_TYPE, "%s is not a NUMBER", bon_type_name(node));
        return -1;
    }
    const unsigned char* p = node->buf->ptr + node->off;
    unsigned char mode = p[1];
    if (mode == BON_MODE_VARINT) {
        uint64_t n = rd_u64(p + 2);
        if (n == 0) {
            set_err(BON_E_LENGTH, "varint payload is empty");
            return -1;
        }
        if ((p[10 + n - 1] & 0x80) != 0) {
            set_err(BON_E_RANGE, "value is negative");
            return -1;
        }
        if (!bytes_to_u64(p + 10, (size_t)n, *out)) {
            set_err(BON_E_RANGE, "value does not fit in uint64");
            return -1;
        }
        return 0;
    }
    size_t w = fixed_mode_size(mode);
    if (w == 0) {
        set_err(BON_E_MODE, "reserved number mode 0x%02X", (unsigned)mode);
        return -1;
    }
    if (mode > BON_MODE_UINT64) {
        set_err(BON_E_RANGE, "value is negative");
        return -1;
    }
    uint64_t stored = 0;
    for (size_t i = w; i-- > 0; ) {
        stored = (stored << 8) | (uint64_t)p[2 + i];
    }
    *out = stored;
    return 0;
}

int bon_get_double(const bon* node, double* out)
{
    clear_err();
    if (!node || !out) {
        set_err(BON_E_INVALID, "null argument");
        return -1;
    }
    if (node->type != BON_FLOAT) {
        set_err(BON_E_TYPE, "%s is not a FLOAT", bon_type_name(node));
        return -1;
    }
    uint64_t raw = rd_u64(node->buf->ptr + node->off + 1);
    double d;
    std::memcpy(&d, &raw, sizeof(d));
    *out = d;
    return 0;
}

int bon_get_decimal(const bon* node, int64_t* coefficient, uint64_t* scale)
{
    clear_err();
    if (!node || !coefficient || !scale) {
        set_err(BON_E_INVALID, "null argument");
        return -1;
    }
    if (node->type != BON_DECIMAL) {
        set_err(BON_E_TYPE, "%s is not a DECIMAL", bon_type_name(node));
        return -1;
    }
    const unsigned char* p = node->buf->ptr + node->off;
    *coefficient = (int64_t)rd_u64(p + 1);
    *scale = rd_u64(p + 9);
    return 0;
}

const void* bon_get_bytes(const bon* node, size_t* size)
{
    clear_err();
    if (!node) {
        set_err(BON_E_INVALID, "null bon object");
        return nullptr;
    }
    if (node->type != BON_STRING && node->type != BON_BLOB) {
        set_err(BON_E_TYPE, "%s has no byte payload", bon_type_name(node));
        return nullptr;
    }
    if (size) {
        *size = node->len - 9;
    }
    return node->buf->ptr + node->off + 9;
}

int bon_get_string(const bon* node, char* out, size_t capacity, size_t* needed)
{
    clear_err();
    if (!node) {
        set_err(BON_E_INVALID, "null bon object");
        return -1;
    }
    if (node->type != BON_STRING) {
        set_err(BON_E_TYPE, "%s is not a STRING", bon_type_name(node));
        return -1;
    }
    size_t n = node->len - 9;
    if (needed) {
        *needed = n + 1;
    }
    if (!out || capacity == 0) {
        return 0;
    }
    if (capacity < n + 1) {
        out[0] = '\0';
        return -2;
    }
    std::memcpy(out, node->buf->ptr + node->off + 9, n);
    out[n] = '\0';
    return 0;
}

const char* bon_get_cstr(const bon* node)
{
    clear_err();
    if (!node) {
        set_err(BON_E_INVALID, "null bon object");
        return nullptr;
    }
    if (node->type != BON_STRING) {
        set_err(BON_E_TYPE, "%s is not a STRING", bon_type_name(node));
        return nullptr;
    }
    const char* p = (const char*)node->buf->ptr + node->off + 9;
    size_t n = node->len - 9;
    std::vector<char>& s = scratch_next();
    s.assign(p, p + n);
    s.push_back('\0');
    return s.data();
}

int bon_get_number_mode(const bon* node, unsigned char* mode)
{
    clear_err();
    if (!node || !mode) {
        set_err(BON_E_INVALID, "null argument");
        return -1;
    }
    if (node->type != BON_NUMBER) {
        set_err(BON_E_TYPE, "%s is not a NUMBER", bon_type_name(node));
        return -1;
    }
    *mode = node->buf->ptr[node->off + 1];
    return 0;
}

int bon_get_int_bytes(const bon* node, const void** bytes, size_t* size,
                      int* negative)
{
    clear_err();
    if (!node || !bytes || !size) {
        set_err(BON_E_INVALID, "null argument");
        return -1;
    }
    if (node->type != BON_NUMBER) {
        set_err(BON_E_TYPE, "%s is not a NUMBER", bon_type_name(node));
        return -1;
    }
    const unsigned char* p = node->buf->ptr + node->off;
    unsigned char mode = p[1];
    if (mode == BON_MODE_VARINT) {
        uint64_t n = rd_u64(p + 2);
        if (n == 0) {
            set_err(BON_E_LENGTH, "varint payload is empty");
            return -1;
        }
        *bytes = p + 10;
        *size = (size_t)n;
        if (negative) {
            *negative = (p[10 + n - 1] & 0x80) ? 1 : 0;
        }
        return 0;
    }
    size_t w = fixed_mode_size(mode);
    if (w == 0) {
        set_err(BON_E_MODE, "reserved number mode 0x%02X", (unsigned)mode);
        return -1;
    }
    uint64_t stored = 0;
    for (size_t i = w; i-- > 0; ) {
        stored = (stored << 8) | (uint64_t)p[2 + i];
    }
    if (negative) {
        *negative = (mode > BON_MODE_UINT64) ? 1 : 0;
    }
    try {
        std::vector<unsigned char> bytes2c;
        if (mode <= BON_MODE_UINT64) {
            u64_to_2c(stored, w, bytes2c);
        } else {
            stored_neg_to_2c(stored, w, bytes2c);
        }
        std::vector<char>& s = scratch_next();
        s.assign((const char*)bytes2c.data(),
                 (const char*)bytes2c.data() + bytes2c.size());
        *bytes = s.data();
        *size = s.size();
        return 0;
    } catch (...) {
        set_err(BON_E_MEMORY, "out of memory");
        return -1;
    }
}

int bon_get_int_str(const bon* node, char* out, size_t capacity, size_t* needed)
{
    clear_err();
    if (!node) {
        set_err(BON_E_INVALID, "null bon object");
        return -1;
    }
    if (node->type != BON_NUMBER) {
        set_err(BON_E_TYPE, "%s is not a NUMBER", bon_type_name(node));
        return -1;
    }
    const unsigned char* p = node->buf->ptr + node->off;
    unsigned char mode = p[1];
    size_t n = 0;
    size_t need = 0;
    std::vector<unsigned char> bytes2c;
    if (mode == BON_MODE_VARINT) {
        n = (size_t)rd_u64(p + 2);
        if (n == 0) {
            set_err(BON_E_LENGTH, "varint payload is empty");
            return -1;
        }
        int_2c_to_dec(p + 10, n, nullptr, 0, &need);
        if (needed) {
            *needed = need;
        }
        if (!out || capacity == 0) {
            return 0;
        }
        if (capacity < need) {
            out[0] = '\0';
            return -2;
        }
        int_2c_to_dec(p + 10, n, out, capacity, &need);
        return 0;
    }
    size_t w = fixed_mode_size(mode);
    if (w == 0) {
        set_err(BON_E_MODE, "reserved number mode 0x%02X", (unsigned)mode);
        return -1;
    }
    uint64_t stored = 0;
    for (size_t i = w; i-- > 0; ) {
        stored = (stored << 8) | (uint64_t)p[2 + i];
    }
    try {
        if (mode <= BON_MODE_UINT64) {
            u64_to_2c(stored, w, bytes2c);
        } else {
            stored_neg_to_2c(stored, w, bytes2c);
        }
    } catch (...) {
        set_err(BON_E_MEMORY, "out of memory");
        return -1;
    }
    int_2c_to_dec(bytes2c.data(), bytes2c.size(), out, capacity, &need);
    if (needed) {
        *needed = need;
    }
    if (!out || capacity == 0) {
        return 0;
    }
    if (capacity < need) {
        return -2;
    }
    return 0;
}

bon_writer* bon_writer_new(int root_type)
{
    clear_err();
    if (root_type != BON_ARRAY && root_type != BON_OBJECT) {
        set_err(BON_E_ROOT, "root type 0x%02X must be ARRAY or OBJECT",
                (unsigned)root_type);
        return nullptr;
    }
    bon_writer* w = new (std::nothrow) bon_writer();
    if (!w) {
        set_err(BON_E_MEMORY, "out of memory");
        return nullptr;
    }
    w->failed = 0;
    try {
        w->buf.reserve(64);
        w->buf.insert(w->buf.end(), kMagic, kMagic + kMagicLen);
        w->buf.push_back((unsigned char)root_type);
        w->buf.insert(w->buf.end(), 16, 0);
        w->stack.reserve(8);
        bon_writer::Frame f;
        f.len_pos = 4;
        f.count = 0;
        f.is_object = (root_type == BON_OBJECT);
        f.pending_key = 0;
        w->stack.push_back(f);
    } catch (...) {
        delete w;
        set_err(BON_E_MEMORY, "out of memory");
        return nullptr;
    }
    return w;
}

int bon_w_none(bon_writer* w)
{
    clear_err();
    if (!writer_ok(w)) {
        return -1;
    }
    writer_atomic(w, BON_NONE);
    return w->failed ? -1 : 0;
}

int bon_w_bool(bon_writer* w, int value)
{
    clear_err();
    if (!writer_ok(w)) {
        return -1;
    }
    writer_put(w, BON_BOOL);
    writer_put(w, value ? 0x01 : 0x00);
    writer_finish_value(w);
    return w->failed ? -1 : 0;
}

int bon_w_int64(bon_writer* w, int64_t value)
{
    clear_err();
    if (!writer_ok(w)) {
        return -1;
    }
    if (value < 0) {
        uint64_t stored = (uint64_t)(-(value + 1));
        writer_fixed_int(w, pick_neg_mode(stored), stored);
    } else {
        writer_fixed_int(w, pick_pos_mode((uint64_t)value), (uint64_t)value);
    }
    return w->failed ? -1 : 0;
}

int bon_w_uint64(bon_writer* w, uint64_t value)
{
    clear_err();
    if (!writer_ok(w)) {
        return -1;
    }
    writer_fixed_int(w, pick_pos_mode(value), value);
    return w->failed ? -1 : 0;
}

int bon_w_double(bon_writer* w, double value)
{
    clear_err();
    if (!writer_ok(w)) {
        return -1;
    }
    uint64_t raw;
    std::memcpy(&raw, &value, sizeof(raw));
    writer_put(w, BON_FLOAT);
    unsigned char tmp[8];
    wr_u64(tmp, raw);
    writer_put_bytes(w, tmp, 8);
    writer_finish_value(w);
    return w->failed ? -1 : 0;
}

int bon_w_decimal(bon_writer* w, int64_t coefficient, uint64_t scale)
{
    clear_err();
    if (!writer_ok(w)) {
        return -1;
    }
    writer_put(w, BON_DECIMAL);
    unsigned char tmp[8];
    wr_u64(tmp, (uint64_t)coefficient);
    writer_put_bytes(w, tmp, 8);
    wr_u64(tmp, scale);
    writer_put_bytes(w, tmp, 8);
    writer_finish_value(w);
    return w->failed ? -1 : 0;
}

int bon_w_string(bon_writer* w, const void* utf8, size_t size)
{
    clear_err();
    if (!writer_ok(w)) {
        return -1;
    }
    if (size > 0 && !utf8) {
        writer_fail(w, BON_E_USAGE, "null string payload");
        return -1;
    }
    writer_len_prefixed(w, BON_STRING, utf8, size);
    return w->failed ? -1 : 0;
}

int bon_w_blob(bon_writer* w, const void* data, size_t size)
{
    clear_err();
    if (!writer_ok(w)) {
        return -1;
    }
    if (size > 0 && !data) {
        writer_fail(w, BON_E_USAGE, "null blob payload");
        return -1;
    }
    writer_len_prefixed(w, BON_BLOB, data, size);
    return w->failed ? -1 : 0;
}

int bon_w_int_bytes(bon_writer* w, const void* little_endian_2c, size_t size)
{
    clear_err();
    if (!writer_ok(w)) {
        return -1;
    }
    writer_int_bytes(w, little_endian_2c, size, 0);
    return w->failed ? -1 : 0;
}

int bon_w_int_bytes_varint(bon_writer* w, const void* little_endian_2c,
                           size_t size)
{
    clear_err();
    if (!writer_ok(w)) {
        return -1;
    }
    writer_int_bytes(w, little_endian_2c, size, 1);
    return w->failed ? -1 : 0;
}

int bon_w_int_dec(bon_writer* w, const char* digits, size_t size)
{
    clear_err();
    if (!writer_ok(w)) {
        return -1;
    }
    if (!digits) {
        writer_fail(w, BON_E_USAGE, "null decimal text");
        return -1;
    }
    int sign = 0;
    std::vector<unsigned char> mag;
    std::vector<unsigned char> bytes2c;
    try {
        if (!dec_to_mag(digits, size, sign, mag)) {
            writer_fail(w, BON_E_USAGE, "'%.*s' is not a decimal integer",
                        (int)(size > 24 ? 24 : size), digits);
            return -1;
        }
        if (sign) {
            negate_2c(mag, bytes2c);
        } else {
            bytes2c = mag;
            if (bytes2c.empty()) {
                bytes2c.push_back(0x00);
            } else if ((bytes2c.back() & 0x80) != 0) {
                bytes2c.push_back(0x00);
            }
        }
    } catch (...) {
        writer_fail(w, BON_E_MEMORY, "out of memory");
        return -1;
    }
    writer_int_bytes(w, bytes2c.data(), bytes2c.size(), 0);
    return w->failed ? -1 : 0;
}

int bon_w_array_begin(bon_writer* w)
{
    clear_err();
    if (!writer_ok(w)) {
        return -1;
    }
    writer_begin(w, BON_ARRAY);
    return w->failed ? -1 : 0;
}

int bon_w_array_end(bon_writer* w)
{
    clear_err();
    if (!writer_ok(w)) {
        return -1;
    }
    writer_end(w, BON_ARRAY);
    return w->failed ? -1 : 0;
}

int bon_w_object_begin(bon_writer* w)
{
    clear_err();
    if (!writer_ok(w)) {
        return -1;
    }
    writer_begin(w, BON_OBJECT);
    return w->failed ? -1 : 0;
}

int bon_w_object_end(bon_writer* w)
{
    clear_err();
    if (!writer_ok(w)) {
        return -1;
    }
    writer_end(w, BON_OBJECT);
    return w->failed ? -1 : 0;
}

const void* bon_writer_data(const bon_writer* w, size_t* size)
{
    clear_err();
    if (!w) {
        set_err(BON_E_INVALID, "null bon writer");
        return nullptr;
    }
    if (size) {
        *size = w->buf.size();
    }
    return w->buf.empty() ? nullptr : w->buf.data();
}

size_t bon_writer_size(const bon_writer* w)
{
    return w ? w->buf.size() : 0;
}

int bon_writer_depth(const bon_writer* w)
{
    return w ? (int)w->stack.size() : 0;
}

bon* bon_writer_finish(bon_writer* w)
{
    clear_err();
    if (!w) {
        set_err(BON_E_INVALID, "null bon writer");
        return nullptr;
    }
    /* The writer is consumed if and only if this call returns non-NULL.  On
     * every failure the caller still owns w and must bon_writer_free() it. */
    if (w->failed) {
        writer_poison(w);
        set_err(BON_E_STATE, "bon writer is in a failed state");
        return nullptr;
    }
    if (w->stack.size() != 1) {
        size_t depth = w->stack.size() - 1;
        writer_poison(w);
        set_err(BON_E_STATE, "%llu container(s) left open",
                (unsigned long long)depth);
        return nullptr;
    }
    bon_writer::Frame f = w->stack.back();
    if (f.is_object && f.pending_key != 0) {
        writer_poison(w);
        set_err(BON_E_STATE, "root object key without a value");
        return nullptr;
    }
    wr_u64(&w->buf[f.len_pos], (uint64_t)(w->buf.size() - (f.len_pos + 16)));
    wr_u64(&w->buf[f.len_pos + 8], f.count);
    bon* n = bon_from_stream(w->buf.data(), w->buf.size());
    if (!n) {
        writer_poison(w); /* bon_from_stream already set the error */
        return nullptr;
    }
    delete w;
    return n;
}

void bon_writer_free(bon_writer* w)
{
    delete w;
}

int bon_write_json(FILE* out, const bon* node)
{
    clear_err();
    if (!out || !node) {
        set_err(BON_E_INVALID, "null argument");
        return -1;
    }
    try {
        if (!json_write(out, node)) {
            return -1;
        }
        std::fputc('\n', out);
    } catch (...) {
        set_err(BON_E_MEMORY, "out of memory");
        return -1;
    }
    return std::ferror(out) ? -1 : 0;
}

int bon_write_hex(FILE* out, const void* bytes, size_t size)
{
    clear_err();
    if (!out) {
        set_err(BON_E_INVALID, "null argument");
        return -1;
    }
    if (!bytes && size > 0) {
        set_err(BON_E_INVALID, "null buffer with size %llu",
                (unsigned long long)size);
        return -1;
    }
    const unsigned char* p = (const unsigned char*)bytes;
    if (size == 0) {
        if (std::fprintf(out, "(empty)\n") < 0) {
            set_err(BON_E_IO, "write error");
            return -1;
        }
        return 0;
    }
    for (size_t off = 0; off < size; off += 16) {
        std::fprintf(out, "%08llX  ", (unsigned long long)off);
        for (size_t i = 0; i < 16; ++i) {
            if (off + i < size) {
                std::fprintf(out, "%02X ", p[off + i]);
            } else {
                std::fputs("   ", out);
            }
            if (i == 7) {
                std::fputc(' ', out);
            }
        }
        std::fputc(' ', out);
        for (size_t i = 0; i < 16 && off + i < size; ++i) {
            unsigned char c = p[off + i];
            std::fputc((c >= 0x20 && c < 0x7F) ? (int)c : '.', out);
        }
        std::fputc('\n', out);
    }
    if (std::ferror(out)) {
        set_err(BON_E_IO, "write error");
        return -1;
    }
    return 0;
}

}
