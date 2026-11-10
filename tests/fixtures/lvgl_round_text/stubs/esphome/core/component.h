#pragma once
namespace esphome {
namespace setup_priority { constexpr float PROCESSOR=400; }
class Component {
 public:
  virtual ~Component()=default;
  virtual void setup() {}
  virtual void dump_config() {}
  virtual float get_setup_priority() const { return 0; }
  void disable_loop() { this->loop_enabled=false; }
  void mark_failed() { this->failed=true; }
  bool loop_enabled=true, failed=false;
};
}
