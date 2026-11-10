#pragma once
#include <algorithm>
#include <array>
#include <cstdint>
#include <cstdlib>
#include <cstring>
#include <memory>
#include <string>
#include <vector>

using TfLiteStatus = int;
static constexpr int kTfLiteOk = 0, kTfLiteError = 1;
static constexpr int kTfLiteInt8 = 9, kTfLiteUInt8 = 3, TFLITE_SCHEMA_VERSION = 3;
struct TfLiteIntArray { int size; int data[3]; };
union TfLitePtrUnion { int8_t *int8; uint8_t *uint8; };
struct TfLiteTensor { TfLiteIntArray *dims; int type; TfLitePtrUnion data; };

namespace test_backend {
inline bool fail_heap = false, fail_tensors = false, bad_input = false, bad_output = false;
inline size_t allocations = 0, frees = 0, constructions = 0;
inline std::vector<std::array<int8_t,120>> invocations;
inline void reset() {
  fail_heap = fail_tensors = bad_input = bad_output = false;
  allocations = frees = constructions = 0;
  invocations.clear();
}
}

namespace tflite {
struct Model { int version() const { return TFLITE_SCHEMA_VERSION; } };
inline const Model *GetModel(const uint8_t *) { static Model model; return &model; }
class MicroAllocator {
 public:
  static MicroAllocator *Create(uint8_t *, size_t) { static MicroAllocator allocator; return &allocator; }
};
class MicroResourceVariables {
 public:
  static MicroResourceVariables *Create(MicroAllocator *, size_t) { static MicroResourceVariables variables; return &variables; }
};
template<int N> class MicroMutableOpResolver {
 public:
#define TEST_ADD(name) int name() { return kTfLiteOk; }
  TEST_ADD(AddCallOnce) TEST_ADD(AddVarHandle) TEST_ADD(AddReshape) TEST_ADD(AddReadVariable)
  TEST_ADD(AddStridedSlice) TEST_ADD(AddConcatenation) TEST_ADD(AddAssignVariable) TEST_ADD(AddConv2D)
  TEST_ADD(AddMul) TEST_ADD(AddAdd) TEST_ADD(AddMean) TEST_ADD(AddFullyConnected) TEST_ADD(AddLogistic)
  TEST_ADD(AddQuantize) TEST_ADD(AddDepthwiseConv2D) TEST_ADD(AddAveragePool2D) TEST_ADD(AddMaxPool2D)
  TEST_ADD(AddPad) TEST_ADD(AddPack) TEST_ADD(AddSplitV)
#undef TEST_ADD
};
class MicroInterpreter {
 public:
  template<int N> MicroInterpreter(const Model *, MicroMutableOpResolver<N> &, uint8_t *, size_t,
                                   MicroResourceVariables *) {
    ++test_backend::constructions;
    input_bytes.fill(0x55);
    input_tensor = {&input_dims,kTfLiteInt8,{}};
    input_tensor.data.int8 = input_bytes.data();
    output_tensor = {&output_dims,kTfLiteUInt8,{}};
    output_tensor.data.uint8 = &output_byte;
  }
  int AllocateTensors() {
    if (test_backend::bad_input) input_tensor.type = kTfLiteUInt8;
    if (test_backend::bad_output) output_tensor.type = kTfLiteInt8;
    return test_backend::fail_tensors ? kTfLiteError : kTfLiteOk;
  }
  int Invoke() { test_backend::invocations.push_back(input_bytes); return kTfLiteOk; }
  size_t arena_used_bytes() const { return 128; }
  TfLiteTensor *input(int) { return &input_tensor; }
  TfLiteTensor *output(int) { return &output_tensor; }
  std::array<int8_t,120> input_bytes;
  TfLiteIntArray input_dims{3,{1,3,40}}, output_dims{2,{1,1,0}};
  TfLiteTensor input_tensor{}, output_tensor{};
  uint8_t output_byte{0};
};
template<typename T> T *GetTensorData(TfLiteTensor *tensor) {
  return reinterpret_cast<T *>(tensor->data.int8);
}
}

namespace esphome {
using std::make_unique;
template<typename T> class RAMAllocator {
 public:
  enum Flags { NONE=0, ALLOC_EXTERNAL=1, ALLOC_INTERNAL=2, PREFER_INTERNAL=8 };
  RAMAllocator() = default;
  explicit RAMAllocator(int) {}
  T *allocate(size_t count) {
    if (test_backend::fail_heap) return nullptr;
    auto *result = static_cast<T *>(malloc(count*sizeof(T)));
    if (result != nullptr) { memset(result,0x55,count*sizeof(T)); ++test_backend::allocations; }
    return result;
  }
  void deallocate(T *ptr,size_t) { if (ptr != nullptr) ++test_backend::frees; free(ptr); }
};
inline uint32_t fnv1_hash(const std::string &) { return 1; }
class ESPPreferenceObject {
 public:
  template<class T> bool load(T *) { return false; }
  template<class T> bool save(T *) { return true; }
};
class TestPreferences {
 public:
  template<class T> ESPPreferenceObject make_preference(uint32_t) { return {}; }
};
inline TestPreferences preferences;
inline TestPreferences *global_preferences = &preferences;
template<typename... Args> void test_log(Args...) {}
}
#define ESPHOME_LOG_TAG(name, text) static constexpr const char *name = text
#define ESP_LOGE(...) esphome::test_log(__VA_ARGS__)
#define ESP_LOGW(...) esphome::test_log(__VA_ARGS__)
#define ESP_LOGD(...) esphome::test_log(__VA_ARGS__)
#define ESP_LOGCONFIG(...) esphome::test_log(__VA_ARGS__)
