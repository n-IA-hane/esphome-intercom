#pragma once
#include <cstddef>
namespace allocation_probe {
struct Sample { size_t calls; size_t frees; size_t bytes; size_t reallocations; size_t watched_reallocations; };
void begin();
void watch_realloc(const void *pointer);
Sample end();
}
