"""Exercise the compiled join guard with unrelated and completion notifications."""

from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def test_join_ignores_unrelated_notifications(tmp_path):
    adapter = (
        ROOT
        / "esphome/components/speaker_source/idf_components/pthread_join_guard/CMakeLists.txt"
    ).read_text()
    wait = re.search(r"set\(new_wait \[=\[(.*?)\]=\]\)", adapter, re.S).group(1)
    source = r"""
#include <cassert>
#include <cstddef>
constexpr int PTHREAD_TASK_STATE_RUN = 0, PTHREAD_TASK_STATE_EXIT = 1;
constexpr int portMAX_DELAY = -1;
struct Worker { int state = PTHREAD_TASK_STATE_RUN; void *retval = nullptr; };
int s_threads_lock, remaining_noise, wake_count;
bool locked;
Worker *child;
void _lock_acquire(int *) { assert(!locked); locked = true; }
void _lock_release(int *) { assert(locked); locked = false; }
void xTaskNotifyWait(int, int, void *, int) {
    assert(!locked);
    ++wake_count;
    if (remaining_noise-- == 0) child->state = PTHREAD_TASK_STATE_EXIT;
}
void join_worker(Worker *pthread) {
    bool wait = true;
    void *child_task_retval = nullptr;
""" + wait + r"""
        assert(locked);
        assert(pthread->state == PTHREAD_TASK_STATE_EXIT);
        assert(child_task_retval == pthread->retval);
        _lock_release(&s_threads_lock);
    }
}
int main() {
    for (int noise = 0; noise < 5; ++noise) {
        Worker worker;
        child = &worker;
        remaining_noise = noise;
        wake_count = 0;
        locked = false;
        join_worker(&worker);
        assert(wake_count == noise + 1);
        assert(!locked);
    }
}
"""
    cpp = tmp_path / "join_guard.cpp"
    cpp.write_text(source)
    binary = tmp_path / "join_guard"
    subprocess.run(
        ["g++", "-std=c++20", str(cpp), "-o", str(binary)],
        check=True,
        capture_output=True,
    )
    subprocess.run([str(binary)], check=True, capture_output=True)
