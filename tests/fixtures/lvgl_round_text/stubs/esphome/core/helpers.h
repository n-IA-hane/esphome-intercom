#pragma once
#include <cstdlib>
#include <cstdint>
namespace esphome {
template<typename T> class RAMAllocator {
 public:
  enum Flags { NONE=0, ALLOC_INTERNAL=2 };
  explicit RAMAllocator(uint8_t=0) {}
  T *allocate(size_t bytes) { return static_cast<T *>(std::malloc(bytes)); }
  void deallocate(T *pointer, size_t) { std::free(pointer); }
};
}
