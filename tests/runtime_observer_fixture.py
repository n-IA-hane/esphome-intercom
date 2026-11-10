"""Read the coordinated component's actual observer decision body for host probes."""

import os
from pathlib import Path


def runtime_root():
    return Path(os.environ.get(
        "RUNTIME_CONTROLLER_REPO", Path(__file__).resolve().parents[2] / "esphome-runtime-controller"
    ))


def source_event_body():
    root = runtime_root()
    source = (root / "esphome/components/runtime_controller/runtime_network_observers.cpp").read_text()
    bodies = []
    for signature, guard in (("static bool is_presence_client(", "USE_RUNTIME_CONTROLLER_API"),
                             ("void RuntimeNetworkObservers::source_event(", None),
                             ("void RuntimeNetworkObservers::publish_api_presence_()", "USE_RUNTIME_CONTROLLER_API")):
        start = source.index(signature)
        body = source[start:source.index("\n}\n", start) + 2]
        bodies.append(f"#ifdef {guard}\n{body}\n#endif\n" if guard else body)
    return "\n".join(bodies)
