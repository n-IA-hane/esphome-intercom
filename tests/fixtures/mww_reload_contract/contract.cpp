#include "streaming_model.h"
#include <iostream>
#include <stdexcept>

namespace esphome::micro_wake_word {
const uint8_t *ModelData::get_model_pointer() const { return this->valid_ ? this->data_ : nullptr; }
}
using namespace esphome::micro_wake_word;
using test_backend::invocations;

void check(bool condition,const std::string &message) {
  if (!condition) throw std::runtime_error(message);
}

class TestedModel : public StreamingModel {
 public:
  TestedModel() {
    static constexpr uint8_t MODEL[1] = {0};
    this->model_start_ = MODEL;
    this->default_probability_cutoff_ = this->probability_cutoff_ = 229;
    this->sliding_window_size_ = 5;
    this->recent_streaming_probabilities_.resize(5,0);
    this->tensor_arena_size_ = 1024;
    this->tensor_arena_size_probed_ = true;  // Arena sizing is a separate existing contract.
  }
  void log_model_config() override {}
  DetectionEvent determine_detected() override { return {}; }
  bool load_now() { return this->load_model_(); }
  unsigned phase() const { return this->current_stride_step_; }
  bool loaded() const { return this->loaded_; }
  void remove_model_data() { this->model_start_ = nullptr; }
};

void feed(TestedModel &model,int8_t value) {
  std::array<int8_t,40> feature;
  feature.fill(value);
  check(model.perform_streaming_inference(feature.data()),"unexpected inference/load failure");
}

void verify_input(const std::array<int8_t,120> &input,std::array<int8_t,3> values) {
  for (unsigned row=0;row<3;++row)
    for (unsigned bin=0;bin<40;++bin)
      check(input[row*40+bin]==values[row],"Invoke input does not contain three expected new features");
}

void initial_stream(TestedModel &model,unsigned partial) {
  feed(model,-10); feed(model,-11); feed(model,-12);
  check(model.get_unprocessed_probability_status(),"fixture must leave a real probability pending");
  for (unsigned i=0;i<partial;++i) feed(model,-20-i);
  check(model.phase()==partial,"fixture phase mismatch");
}

void three_new_features(TestedModel &model) {
  const size_t before=invocations.size();
  std::array<size_t,3> counts;
  std::array<bool,3> pending;
  for (unsigned i=0;i<3;++i) {
    feed(model,static_cast<int8_t>((i+1)*17));
    counts[i]=invocations.size()-before;
    pending[i]=model.get_unprocessed_probability_status();
  }
  std::cout<<"new-slice Invoke counts "<<counts[0]<<","<<counts[1]<<","<<counts[2];
  if (invocations.size()>before) {
    const auto &first=invocations[before];
    std::cout<<"; first input row heads "<<int(first[0])<<","<<int(first[40])<<","<<int(first[80]);
  }
  std::cout<<"\n";
  check(counts==std::array<size_t,3>{0,0,1},"Invoke occurred before three NEW features after reload");
  check(pending==std::array<bool,3>{false,false,true},"pending probability leaked across interpreter reload");
  verify_input(invocations.back(),{17,34,51});
}

void reload(unsigned partial) {
  TestedModel model;
  initial_stream(model,partial);
  const size_t created=test_backend::constructions;
  model.unload_model();
  check(!model.loaded(),"unload did not retire interpreter");
  three_new_features(model);
  check(test_backend::constructions==created+1,"reload must construct exactly one new interpreter");
}

void normal_reset(unsigned partial) {
  TestedModel model;
  initial_stream(model,partial);
  const size_t before=invocations.size(), created=test_backend::constructions;
  model.reset_probabilities();
  check(model.phase()==partial && model.get_unprocessed_probability_status(),
        "ordinary probability reset changed stride or pending state");
  for (unsigned i=partial;i<3;++i) feed(model,-20-i);
  check(invocations.size()==before+1 && test_backend::constructions==created,
        "ordinary probability reset broke the current interpreter stream");
  verify_input(invocations.back(),{-20,-21,-22});
}

void pending_cleared_only_for_new_interpreter() {
  TestedModel model;
  initial_stream(model,1);
  check(model.load_now(),"existing interpreter load failed");
  check(model.phase()==1 && model.get_unprocessed_probability_status(),
        "existing interpreter unexpectedly reset stride/pending");
  model.unload_model();
  check(model.load_now(),"new interpreter load failed");
  check(model.phase()==0 && !model.get_unprocessed_probability_status(),
        "new interpreter did not reset stride/pending after successful validation");
}

void failed_load(unsigned mode) {
  TestedModel model;
  initial_stream(model,2);
  model.unload_model();
  test_backend::fail_heap=mode==0;
  test_backend::fail_tensors=mode==1;
  test_backend::bad_input=mode==2;
  test_backend::bad_output=mode==3;
  check(!model.load_now(),"invalid load was unexpectedly accepted");
  check(!model.loaded() && model.phase()==2 && model.get_unprocessed_probability_status(),
        "failed load reset the prior logical state before a successful new interpreter");
  // Match inference_task's existing error cleanup before any later start.
  model.unload_model();
  test_backend::fail_heap=test_backend::fail_tensors=test_backend::bad_input=test_backend::bad_output=false;
  three_new_features(model);
}

void disabled_model() {
  TestedModel model;
  model.disable();
  const size_t allocations=test_backend::allocations;
  feed(model,12);
  check(invocations.empty() && test_backend::allocations==allocations,"disabled model allocated or invoked");
  model.enable();
  initial_stream(model,2);
  model.disable();
  const size_t calls=invocations.size();
  feed(model,13);
  check(!model.loaded() && invocations.size()==calls,"disable must unload without consuming this feature");
  feed(model,14);
  check(invocations.size()==calls,"disabled unloaded model invoked");
  model.enable();
  three_new_features(model);
}

void missing_model() {
  TestedModel model;
  model.remove_model_data();
  feed(model,11);
  check(!model.is_enabled() && !model.loaded() && invocations.empty(),"missing model must remain disabled");
}

int main() {
  unsigned failures=0,passed=0;
  auto run=[&](const char *name,auto test) {
    test_backend::reset();
    try { test(); check(test_backend::allocations==test_backend::frees,"heap ownership leak");
          ++passed; std::cout<<"PASS "<<name<<"\n"; }
    catch (const std::exception &error) { ++failures; std::cout<<"FAIL "<<name<<": "<<error.what()<<"\n"; }
  };
  run("reload_after_one_slice",[]{reload(1);});
  run("reload_after_two_slices",[]{reload(2);});
  run("normal_reset_after_one_slice",[]{normal_reset(1);});
  run("normal_reset_after_two_slices",[]{normal_reset(2);});
  run("pending_new_vs_existing_interpreter",pending_cleared_only_for_new_interpreter);
  run("failed_heap_then_cleanup_reload",[]{failed_load(0);});
  run("failed_allocate_tensors_then_cleanup_reload",[]{failed_load(1);});
  run("invalid_input_then_cleanup_reload",[]{failed_load(2);});
  run("invalid_output_then_cleanup_reload",[]{failed_load(3);});
  run("disabled_and_reenabled_model",disabled_model);
  run("missing_model",missing_model);
  std::cout<<"SUMMARY passed="<<passed<<" failed="<<failures<<"\n";
  return failures ? 1 : 0;
}
