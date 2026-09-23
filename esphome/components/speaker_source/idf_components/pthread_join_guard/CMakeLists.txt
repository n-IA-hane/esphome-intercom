# ESPHome wakes the main loop using the notification slot also used by IDF
# pthread_join. A notification is not proof that the child has exited.
# Preserve IDF's implementation, changing only the completion predicate.
idf_component_register(REQUIRES pthread)
if(CMAKE_BUILD_EARLY_EXPANSION)
    return()
endif()
idf_component_get_property(pthread_dir pthread COMPONENT_DIR)
idf_component_get_property(pthread_lib pthread COMPONENT_LIB)
file(READ "${pthread_dir}/pthread.c" source)
set(old_wait [=[        if (wait) {
            xTaskNotifyWait(0, 0, NULL, portMAX_DELAY);
            _lock_acquire(&s_threads_lock);
            child_task_retval = pthread->retval;]=])
set(new_wait [=[        if (wait) {
            for (;;) {
                xTaskNotifyWait(0, 0, NULL, portMAX_DELAY);
                _lock_acquire(&s_threads_lock);
                if (pthread->state == PTHREAD_TASK_STATE_EXIT) {
                    break;
                }
                _lock_release(&s_threads_lock);
            }
            child_task_retval = pthread->retval;]=])
set(old_exit [=[        // Remove from list, it indicates that task has exited
        if (pthread->join_task) {
            // notify join
            xTaskNotify(pthread->join_task, 0, eNoAction);
        } else {
            pthread->state = PTHREAD_TASK_STATE_EXIT;
        }]=])
set(new_exit [=[        // Publish completion under the same lock checked by pthread_join.
        pthread->state = PTHREAD_TASK_STATE_EXIT;
        if (pthread->join_task) {
            // notify join
            xTaskNotify(pthread->join_task, 0, eNoAction);
        }]=])
# Skip the adapter once the installed dependency has this guarded wait.
string(FIND "${source}" "${new_wait}" guarded_wait)
string(FIND "${source}" "${new_exit}" guarded_exit)
if(NOT guarded_wait EQUAL -1 AND NOT guarded_exit EQUAL -1)
    return()
endif()
foreach(part wait exit)
    string(FIND "${source}" "${old_${part}}" location)
    if(location EQUAL -1)
        message(FATAL_ERROR "ESP-IDF pthread implementation changed; review the join completion compatibility adapter (${part})")
    endif()
    string(REPLACE "${old_${part}}" "${new_${part}}" source "${source}")
endforeach()
set(patched "${CMAKE_CURRENT_BINARY_DIR}/pthread_join_guard.c")
file(WRITE "${patched}" "${source}")
get_target_property(sources ${pthread_lib} SOURCES)
list(FILTER sources EXCLUDE REGEX "(^|/)pthread\\.c$")
list(APPEND sources "${patched}")
set_property(TARGET ${pthread_lib} PROPERTY SOURCES "${sources}")
target_include_directories(${pthread_lib} PRIVATE "${pthread_dir}")
