"""Run native Select restoration and maintained boot lambdas before DSP setup."""
from pathlib import Path
import os
import re
import shutil
import subprocess

import esphome
from esphome import yaml_util
import pytest


ROOT = Path(__file__).resolve().parents[1]
ESPHOME = Path(esphome.__file__).resolve().parent
AUDIO_STACK = Path(os.environ.get("AUDIO_STACK_REPO", ROOT.parent / "esphome-audio-stack"))


def source_function(source, signature):
    """Retain a complete, unmodified C++ function, including its real body."""
    start = source.index(signature)
    opening = source.index("{", start)
    depth = 0
    for position in range(opening, len(source)):
        depth += (source[position] == "{") - (source[position] == "}")
        if depth == 0:
            return source[start:position + 1]
    raise AssertionError(f"Unclosed function: {signature}")


def boot_action(backend):
    document = yaml_util.load_yaml(ROOT / f"packages/features/audio_controls/{backend}_mode.yaml")
    actions = document["esphome"]["on_boot"]
    assert len(actions) == 1
    boot = actions[0]
    assert len(boot["then"]) == 1 and set(boot["then"][0]) == {"lambda"}
    value = boot["then"][0]["lambda"]
    return float(boot["priority"]), getattr(value, "value", str(value))


def native_priorities(backend):
    component = (ESPHOME / "core/component.h").read_text()
    template = (ESPHOME / "components/template/select/template_select.h").read_text()
    processor = (AUDIO_STACK / f"esphome/components/esp_{backend}/esp_{backend}.h").read_text()
    select_priority = source_function(template, "float get_setup_priority() const override")
    processor_priority = source_function(processor, "float get_setup_priority() const override")
    assert "setup_priority::HARDWARE" in select_priority
    assert "setup_priority::PROCESSOR" in processor_priority
    values = []
    for name in ("HARDWARE", "PROCESSOR"):
        match = re.search(r"inline constexpr float " + name + r" = ([\d.]+)f;", component)
        assert match is not None, name
        values.append(float(match[1]))
    return values


@pytest.mark.parametrize("backend", ["aec", "afe"])
def test_select_restore_precedes_boot_configuration_and_dsp_setup(backend):
    select, processor = native_priorities(backend)
    boot, _ = boot_action(backend)
    assert (select, boot, processor) == (800, 500, 400)
    assert select > boot > processor


PUBLIC_SELECT = r'''
#pragma once
#include <algorithm>
#include <cassert>
#include <cstddef>
#include <optional>
#include <string>
#include <type_traits>
#include <vector>
namespace esphome {
template<typename T> using optional = std::optional<T>;
struct PreferenceStorage {
  bool available{true};
  size_t index{0};
  unsigned loads{0}, saves{0};
};
inline PreferenceStorage stored;
class ESPPreferenceObject {
 public:
  bool load(size_t *value) {
    ++stored.loads;
    if (!stored.available) return false;
    *value = stored.index;
    return true;
  }
  bool save(const size_t *value) { ++stored.saves; stored.index = *value; return true; }
};
namespace template_ {
class BaseTemplateSelect {
 public:
  virtual ~BaseTemplateSelect() = default;
  virtual void setup() {}
  bool has_index(size_t index) const { return index < options.size(); }
  bool has_option(const std::string &option) const {
    return std::find(options.begin(), options.end(), option) != options.end();
  }
  const std::string &option_at(size_t index) const { assert(has_index(index)); return options[index]; }
  const std::string &current_option() const { return option_at(selected); }
  void publish_state(size_t index) { assert(has_index(index)); selected = index; }
  void publish_state(const std::string &option) {
    assert(has_option(option));
    selected = std::find(options.begin(), options.end(), option) - options.begin();
  }
  template<typename T> ESPPreferenceObject make_entity_preference() {
    static_assert(std::is_same_v<T, size_t>);
    return {};
  }
  std::vector<std::string> options{"sr_low_cost", "sr_high_perf"};
  size_t selected{99};
};
void setup_initial(BaseTemplateSelect *, size_t);
void setup_with_restore(BaseTemplateSelect *, ESPPreferenceObject &, size_t);
template<bool HAS_LAMBDA, bool RESTORE_VALUE, size_t INITIAL_OPTION_INDEX>
class SelectProbe : public BaseTemplateSelect {
 public:
  // The exact upstream setup implementation is inserted below.
  NATIVE_SETUP
  ESPPreferenceObject pref_;
};
}
}
'''


HARNESS = r'''
#include "template_select.h"
#include <array>
#include <functional>
#include <iostream>
using namespace esphome;
struct Processor {
  // The configuration setters below are copied verbatim from Audio Stack.
  CONFIG_SETTERS
  std::string get_mode_name() const {
    return std::string(afe_type_ == 0 ? "sr_" : "fd_") + (MODE_FIELD == 0 ? "low_cost" : "high_perf");
  }
  void setup() { mode_at_setup = MODE_FIELD; type_at_setup = afe_type_; ++setups; }
  int mode_{1}, afe_mode_{1}, afe_type_{3}, mode_at_setup{-1}, type_at_setup{-1};
  unsigned setups{0};
};

template<bool Restore, size_t Initial = 1>
void scenario(const std::string &name, int expected) {
  template_::SelectProbe<false, Restore, Initial> aec_mode_select;
  Processor PROCESSOR_ID;
  if (name == "unknown_option") {
    aec_mode_select.options[0] = "unrecognized_mode";
    PROCESSOR_ID.afe_type_ = 0;
  }
  const auto boot = [&]() {
#define id(value) value
    BOOT_LAMBDA
#undef id
  };
  struct Step { float priority; std::function<void()> action; };
  std::array<Step, 3> steps{{
      {SELECT_PRIORITY, [&] { aec_mode_select.setup(); }},
      {BOOT_PRIORITY, boot},
      {PROCESSOR_PRIORITY, [&] { PROCESSOR_ID.setup(); }},
  }};
  std::sort(steps.begin(), steps.end(), [](const auto &a, const auto &b) { return a.priority > b.priority; });
  for (auto &step : steps) step.action();
  assert(PROCESSOR_ID.setups == 1);
  assert(PROCESSOR_ID.mode_at_setup == expected);
  assert(aec_mode_select.current_option() == (expected == 0 ? "sr_low_cost" : "sr_high_perf"));
  AFE_TYPE_CHECK
  assert(stored.loads == (Restore ? 1U : 0U));
  assert(stored.saves == 0 && "boot must not rewrite the user's preference");
}

int main(int argc, char **argv) {
  assert(argc == 2);
  const std::string name(argv[1]);
  stored = {};
  if (name == "saved_low") scenario<true>(name, 0);
  else if (name == "saved_high") { stored.index = 1; scenario<true>(name, 1); }
  else if (name == "preference_missing") { stored.available = false; scenario<true>(name, 1); }
  else if (name == "preference_invalid") { stored.index = 99; scenario<true>(name, 1); }
  else if (name == "restore_disabled") scenario<false>(name, 1);
  else if (name == "explicit_initial_low") { stored.index = 1; scenario<false, 0>(name, 0); }
  else if (name == "unknown_option") scenario<true>(name, 1);
  else return 2;
  std::cout << "PASS " << name << '\n';
}
'''


@pytest.fixture(scope="module", params=["aec", "afe"])
def restore_binary(tmp_path_factory, request):
    compiler = shutil.which("g++")
    assert compiler, "Native restoration gate requires g++"
    backend = request.param
    build = tmp_path_factory.mktemp(f"full-{backend}-restore")
    native = ESPHOME / "components/template/select"
    header = (native / "template_select.h").read_text()
    setup = source_function(header, "void setup() override")
    (build / "template_select.h").write_text(PUBLIC_SELECT.replace("NATIVE_SETUP", setup))
    # Compile the complete, byte-identical upstream helper implementation.
    shutil.copyfile(native / "template_select.cpp", build / "template_select.cpp")
    log = build / "esphome/core/log.h"
    log.parent.mkdir(parents=True)
    log.write_text(
        "#pragma once\n"
        "template<typename... Args> inline void test_log(Args &&...) {}\n"
        "#define ESP_LOGD(...) test_log(__VA_ARGS__)\n"
        "#define ESP_LOGE(...) test_log(__VA_ARGS__)\n"
        "#define ESP_LOGCONFIG(...) test_log(__VA_ARGS__)\n"
        "#define LOG_SELECT(prefix, label, value) (void)(value)\n"
        "#define LOG_UPDATE_INTERVAL(value) (void)(value)\n"
        '#define YESNO(value) ((value) ? "YES" : "NO")\n'
    )
    processor = (AUDIO_STACK / f"esphome/components/esp_{backend}/esp_{backend}.h").read_text()
    signatures = ["void set_mode(int mode)"] if backend == "aec" else ["void set_afe_type(int type)", "void set_afe_mode(int mode)"]
    setters = "\n".join(source_function(processor, signature) for signature in signatures)
    select_priority, processor_priority = native_priorities(backend)
    boot_priority, boot = boot_action(backend)
    replacements = {
        "CONFIG_SETTERS": setters,
        "MODE_FIELD": "mode_" if backend == "aec" else "afe_mode_",
        "PROCESSOR_ID": "aec_processor" if backend == "aec" else "afe_processor",
        "BOOT_LAMBDA": boot,
        "SELECT_PRIORITY": str(select_priority),
        "BOOT_PRIORITY": str(boot_priority),
        "PROCESSOR_PRIORITY": str(processor_priority),
        "AFE_TYPE_CHECK": "assert(afe_processor.type_at_setup == 0);" if backend == "afe" else "",
    }
    harness = HARNESS
    for name, replacement in replacements.items():
        harness = harness.replace(name, replacement)
    source = build / "restore.cpp"
    source.write_text(harness)
    binary = build / "restore"
    result = subprocess.run(
        [compiler, "-std=c++17", "-Wall", "-Wextra", "-Werror", "-fsanitize=address,undefined",
         "-fno-omit-frame-pointer", "-I", str(build), str(source),
         str(build / "template_select.cpp"), "-o", str(binary)],
        capture_output=True, text=True, timeout=45,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return binary


@pytest.mark.parametrize("scenario", [
    "saved_low", "saved_high", "preference_missing", "preference_invalid",
    "restore_disabled", "explicit_initial_low", "unknown_option",
])
def test_restored_choice_reaches_dsp_before_setup(restore_binary, scenario):
    result = subprocess.run(
        [str(restore_binary), scenario], capture_output=True, text=True, timeout=5,
        env={**os.environ, "UBSAN_OPTIONS": "halt_on_error=1:print_stacktrace=1"},
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == f"PASS {scenario}"
