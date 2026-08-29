"""Import-only red tests for the 002 scheduler surface.

These tests fail until T003–T018 land the real module content; they exist
to pin the import paths and top-level names the rest of the 002 tasks
rely on. The scheduler package skeleton (T001) makes the imports pass;
the specific attribute imports below are red until each task's
implementation lands.
"""

import digital_twins.scheduler as scheduler  # noqa: F401  (package import)
import digital_twins.scheduler.presets  # noqa: F401  (module import)
import digital_twins.scheduler.schedules  # noqa: F401  (module import)
import digital_twins.scheduler.loop  # noqa: F401  (module import)
import digital_twins.scheduler.status  # noqa: F401  (module import)


def test_presets_module_exposes_expand_next_and_preset_values():
    from digital_twins.scheduler import presets
    assert hasattr(presets, "expand_next")
    assert hasattr(presets, "preset_values")


def test_schedules_module_exposes_crud_functions():
    from digital_twins.scheduler import schedules
    for name in (
        "create_schedule",
        "list_schedules",
        "update_schedule",
        "delete_schedule",
        "due_schedules",
        "claim_and_advance",
    ):
        assert hasattr(schedules, name), f"schedules.{name} missing"


def test_loop_module_exposes_serve_once_tick_and_run_serve():
    from digital_twins.scheduler import loop
    assert hasattr(loop, "serve_once_tick")
    assert hasattr(loop, "run_serve")


def test_status_module_exposes_status_payload():
    from digital_twins.scheduler import status
    assert hasattr(status, "status_payload")
