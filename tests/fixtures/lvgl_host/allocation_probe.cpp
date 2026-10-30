#include "allocation_probe.h"
#include <cstdlib>
namespace {
bool enabled;
const void *watched;
allocation_probe::Sample sample{};
void counted(size_t size) { if (enabled) { ++sample.calls; sample.bytes += size; } }
}
namespace allocation_probe {
void watch_realloc(const void *p) { watched = p; }
void begin() { sample = {}; enabled = true; }
Sample end() { enabled = false; return sample; }
}
extern "C" {
void *__real_malloc(size_t);
void *__real_calloc(size_t, size_t);
void *__real_realloc(void *, size_t);
void __real_free(void *);
void *__wrap_malloc(size_t size) { counted(size); return __real_malloc(size); }
void *__wrap_calloc(size_t count, size_t size) { counted(count * size); return __real_calloc(count, size); }
void *__wrap_realloc(void *p, size_t size) { counted(size); if (enabled) { ++sample.reallocations; if(p == watched) ++sample.watched_reallocations; } return __real_realloc(p, size); }
void __wrap_free(void *p) { if (enabled && p != nullptr) ++sample.frees; __real_free(p); }
}
