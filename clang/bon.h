/*
 * BON (Binary Object Notation) v1.0 - C ABI
 *
 * The bon object IS the file byte stream itself.  A bon object owns (or
 * borrows) one byte buffer, and every access to the data is expressed as a
 * position + length inside that buffer.  Nothing in this interface belongs to
 * a language: no std::string, no dictionaries, no boxed variants, no GC roots.
 * Only byte buffers, offsets, lengths and fixed-width scalars cross the ABI,
 * so the same DLL is used unchanged from C, C++, Python, Rust, Go, C#, etc.
 * via a plain C FFI.
 *
 * Child nodes are zero-copy views into the parent buffer: bon_data(child)
 * points inside bon_data(parent).  Children are kept alive by the buffer's
 * reference count, so a child pointer is only valid while any ancestor of that
 * child is alive.
 *
 * Ownership: every bon* handed to the caller owns one reference.
 * bon_ref() hands out an extra reference (release it with bon_unref() or
 * bon_free()), bon_unref()/bon_free() drop one reference and destroy the node
 * once the last reference is gone.  A child returned by bon_at(), bon_find(),
 * an iterator, ... is a new reference, so the parent may be freed immediately
 * and the child stays usable.
 *
 * Writing: bon_writer_new(BON_ARRAY|BON_OBJECT) emits the root container
 * header straight away, so scalars written next belong to the root and
 * bon_w_array_begin()/bon_w_object_begin() only nest further.  Container
 * lengths and counts are back-patched, so bon_writer_data() is only final
 * after bon_writer_finish(); use the finished root for byte-exact output.
 * bon_writer_finish() consumes the writer only when it succeeds, so on failure
 * the writer must be released with bon_writer_free().
 *
 * Threading: every entry point is re-entrant and free of shared mutable state
 * except the thread-local error slot.  The same bon object may be read from
 * several threads concurrently.
 */

#ifndef BON_H_INCLUDED
#define BON_H_INCLUDED

#include <stddef.h>
#include <stdint.h>
#include <stdio.h>

#if defined(_WIN32)
#  if defined(BON_BUILD_DLL)
#    define BON_API __declspec(dllexport)
#  elif defined(BON_USE_DLL)
#    define BON_API __declspec(dllimport)
#  else
#    define BON_API
#  endif
#else
#  if defined(BON_BUILD_DLL) && defined(__GNUC__)
#    define BON_API __attribute__((visibility("default")))
#  else
#    define BON_API
#  endif
#endif

#if defined(__cplusplus)
extern "C" {
#endif

#define BON_VERSION_MAJOR 1
#define BON_VERSION_MINOR 0
#define BON_VERSION_PATCH 0
#define BON_VERSION_STRING "1.0.0"

enum {
    BON_NONE    = 0x00,
    BON_BOOL    = 0x01,
    BON_NUMBER  = 0x02,
    BON_FLOAT   = 0x03,
    BON_STRING  = 0x04,
    BON_ARRAY   = 0x05,
    BON_OBJECT  = 0x06,
    BON_BLOB    = 0x07,
    BON_DECIMAL = 0x08
};

enum {
    BON_MODE_UINT8  = 0x00,
    BON_MODE_UINT16 = 0x01,
    BON_MODE_UINT32 = 0x02,
    BON_MODE_UINT64 = 0x03,
    BON_MODE_VARINT = 0x04,
    BON_MODE_NUINT8  = 0x05,
    BON_MODE_NUINT16 = 0x06,
    BON_MODE_NUINT32 = 0x07,
    BON_MODE_NUINT64 = 0x08
};

enum {
    BON_E_OK        =  0,
    BON_E_INVALID   =  1,
    BON_E_MAGIC     =  2,
    BON_E_TRUNCATED =  3,
    BON_E_TYPE      =  4,
    BON_E_MODE      =  5,
    BON_E_LENGTH    =  6,
    BON_E_ROOT      =  7,
    BON_E_RANGE     =  8,
    BON_E_NOKEY     =  9,
    BON_E_DEPTH     = 10,   /* reserved: the format imposes no nesting limit */
    BON_E_STATE     = 11,
    BON_E_IO        = 12,
    BON_E_MEMORY    = 13,
    BON_E_USAGE     = 14
};

typedef struct bon        bon;
typedef struct bon_writer bon_writer;

typedef struct bon_iter {
    struct bon* owner;
    size_t      index;
    size_t      pos;
    size_t      end;
    int         step;
    int         which;
} bon_iter;

BON_API const char* bon_version(void);

BON_API int         bon_last_error_code(void);
BON_API const char* bon_last_error(void);
BON_API void        bon_clear_error(void);
BON_API const char* bon_error_message(int code);

/* Parsing.  bon_from_stream()/bon_from_file() copy the bytes, so the result
 * stands on its own.  bon_from_stream_ref() BORROWS the caller's buffer: it
 * parses in place with no copy, so that buffer must stay alive and unchanged
 * for as long as the returned node and every node reached from it.  Freeing the
 * buffer early leaves dangling pointers that the library cannot detect. */
BON_API bon* bon_from_stream(const void* bytes, size_t size);
BON_API bon* bon_from_stream_ref(const void* bytes, size_t size);
BON_API bon* bon_from_file(const char* path);
BON_API bon* bon_new_array(void);
BON_API bon* bon_new_object(void);
BON_API int  bon_save_file(const bon* node, const char* path);

BON_API bon* bon_ref(bon* node);
BON_API void bon_unref(bon* node);
BON_API void bon_free(void* node);
BON_API bon* bon_root(bon* node);
BON_API int  bon_is_root(const bon* node);

/* Raw bytes.  bon_data()/bon_size() describe the node itself, i.e. its type
 * byte, header and payload as they appear in the file.  For the root node that
 * is the TLV only: the 3 magic bytes are NOT part of bon_data()/bon_size(), so
 * a file is 3 + bon_size(root) bytes.  bon_stream() returns the whole file
 * (magic included) but only for a root; bon_magic() exposes the magic alone. */
BON_API const void* bon_data(const bon* node);
BON_API size_t      bon_size(const bon* node);
BON_API int         bon_stream(const bon* node, const void** bytes, size_t* size);
BON_API const void* bon_magic(const bon* node, size_t* size);
BON_API int         bon_type(const bon* node);
BON_API const char* bon_type_name(const bon* node);
BON_API int         bon_valid(const bon* node);
BON_API size_t      bon_count(const bon* node);
BON_API int         bon_validate(const bon* node);

/* Navigation.  Every bon_at/bon_key_at/bon_value_at/bon_find/bon_iter_* result
 * is a new reference the caller owns.  bon_at() indexes ARRAY elements and
 * OBJECT values, so it is the "give me child i" entry point; bon_key_at() and
 * bon_value_at() address the two halves of an OBJECT pair explicitly.  Only
 * STRING keys are matched by bon_find()/bon_key_count()/bon_key_is(). */
BON_API bon* bon_at(const bon* node, size_t index);
BON_API bon* bon_key_at(const bon* node, size_t index);
BON_API bon* bon_value_at(const bon* node, size_t index);
BON_API bon* bon_find(const bon* node, const void* key, size_t key_size);
BON_API size_t bon_key_count(const bon* node, const void* key, size_t key_size);
BON_API int   bon_key_is(const bon* node, size_t index, const void* key, size_t key_size);
BON_API const char* bon_key_cstr(const bon* node, size_t index, size_t* size);

/* Iterators keep the container alive on their own, so the caller may free it
 * while iterating; the iterator still has to be closed with bon_iter_done().
 * which: 0 = keys, 1 = values (OBJECT only, 0 for ARRAY).  The next calls
 * return 1 while an item was produced, 0 at the end and -1 on error.
 * bon_iter_index() is the 0-based index of the item just returned (0 before
 * the first one), so it can be passed back to bon_at()/bon_value_at().  The
 * "index" field is the raw item counter and should not be read directly. */
BON_API void bon_iter_init(bon_iter* it, bon* node, int which);
BON_API int  bon_iter_next(bon_iter* it, bon** item);
BON_API int  bon_iter_key_next(bon_iter* it, bon** key, bon** value);
BON_API size_t bon_iter_index(const bon_iter* it);
BON_API void bon_iter_done(bon_iter* it);

/* Scalars.  Payloads come back in two different ways, so keep them apart:
 *
 *  - zero-copy borrow, valid while the node lives: bon_get_bytes() and
 *    bon_data() point straight into the document buffer; the VARINT branch of
 *    bon_get_int_bytes() does the same.
 *  - thread-local scratch slot: bon_get_cstr() and bon_key_cstr() return a
 *    NUL-terminated copy in one of 4 rotating slots, so several results can be
 *    alive at once; a result survives the next 3 such calls, then that slot is
 *    reused.  They return NULL (with an error set) on a non-STRING node.
 *    bon_get_int_bytes() also borrows a slot on its fixed-width branch, but
 *    that copy is raw little-endian two's complement -- binary bytes with no
 *    terminator, not a C string.
 *
 * To keep a value beyond that, copy it or use the caller-owned buffers
 * bon_get_string() / bon_get_int_str().  bon_get_int_bytes() hands back the
 * minimal little-endian two's complement form and reports the sign through
 * *negative; bon_w_int_bytes() takes the same form back and re-picks the
 * smallest lossless wire mode. */
BON_API int   bon_get_bool(const bon* node);
BON_API int   bon_get_int64(const bon* node, int64_t* out);
BON_API int   bon_get_uint64(const bon* node, uint64_t* out);
BON_API int   bon_get_double(const bon* node, double* out);
BON_API int   bon_get_decimal(const bon* node, int64_t* coefficient, uint64_t* scale);
BON_API const void* bon_get_bytes(const bon* node, size_t* size);
BON_API int   bon_get_string(const bon* node, char* out, size_t capacity, size_t* needed);
BON_API const char* bon_get_cstr(const bon* node);
BON_API int   bon_get_int_bytes(const bon* node, const void** bytes, size_t* size, int* negative);
BON_API int   bon_get_int_str(const bon* node, char* out, size_t capacity, size_t* needed);
BON_API int   bon_get_number_mode(const bon* node, unsigned char* mode);

/* Streaming writer.  bon_writer_new() writes magic + root header, so the first
 * value written belongs to the root; begin/end only nest deeper.  A failed
 * call latches the writer into a failed state that bon_writer_finish() reports.
 * bon_writer_data()/bon_writer_size() expose the bytes so far, but the
 * back-patched container lengths and counts are only correct after
 * bon_writer_finish(), which then returns the root. */
BON_API bon_writer* bon_writer_new(int root_type);
BON_API int   bon_w_none(bon_writer* w);
BON_API int   bon_w_bool(bon_writer* w, int value);
BON_API int   bon_w_int64(bon_writer* w, int64_t value);
BON_API int   bon_w_uint64(bon_writer* w, uint64_t value);
BON_API int   bon_w_double(bon_writer* w, double value);
BON_API int   bon_w_decimal(bon_writer* w, int64_t coefficient, uint64_t scale);
BON_API int   bon_w_string(bon_writer* w, const void* utf8, size_t size);
BON_API int   bon_w_blob(bon_writer* w, const void* data, size_t size);
BON_API int   bon_w_int_bytes(bon_writer* w, const void* little_endian_2c, size_t size);
BON_API int   bon_w_int_bytes_varint(bon_writer* w, const void* little_endian_2c, size_t size);
BON_API int   bon_w_int_dec(bon_writer* w, const char* digits, size_t size);
BON_API int   bon_w_array_begin(bon_writer* w);
BON_API int   bon_w_array_end(bon_writer* w);
BON_API int   bon_w_object_begin(bon_writer* w);
BON_API int   bon_w_object_end(bon_writer* w);
BON_API const void* bon_writer_data(const bon_writer* w, size_t* size);
BON_API size_t    bon_writer_size(const bon_writer* w);
BON_API int       bon_writer_depth(const bon_writer* w);
/* Ownership: bon_writer_finish() consumes the writer if and only if it returns
 * non-NULL.  When it returns NULL the writer is still alive but poisoned -- it
 * can no longer be finished or written to, and the caller must release it with
 * bon_writer_free().  bon_writer_free() is the only function that destroys a
 * writer, so it is always safe on the handle you still hold. */
BON_API bon*      bon_writer_finish(bon_writer* w);
BON_API void      bon_writer_free(bon_writer* w);

/* JSON debug output.  NONE/BOOL/NUMBER/FLOAT/STRING/BLOB/ARRAY/OBJECT map onto
 * JSON directly.  Values that JSON cannot hold losslessly get a readable tag
 * prefix (DECIMAL "#bon:decimal:<coeff>E-<scale>", a non-STRING object key
 * "#bon:<TYPE>:<json>", BLOB "#bon:blob:<size>:<lowercase hex>").  This is a
 * one-way debug rendering, not a reversible encoding. */
BON_API int bon_write_json(FILE* out, const bon* node);
BON_API int bon_write_hex(FILE* out, const void* bytes, size_t size);

#if defined(__cplusplus)
}
#endif

#endif
