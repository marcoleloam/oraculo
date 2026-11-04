"""Read capability choices without rewriting the literal host catalog."""

from oraculo.state import StateError


def option_values(option: dict) -> list[str | bool]:
    """Validate current values/default or legacy type/options/currentValue."""
    if "values" in option:
        # Do not silently prefer one of two potentially conflicting schemas.
        if "type" in option or "options" in option:
            raise StateError("native_bridge_model_options_incoherent")
        choices = option["values"]
        selections = ("default", "currentValue")
    elif option.get("type") == "boolean":
        choices = [False, True]
        selections = ("currentValue",)
    elif option.get("type") == "select":
        rows = option.get("options")
        if not isinstance(rows, list) or any(
            not isinstance(row, dict) or "id" not in row for row in rows
        ):
            raise StateError("native_bridge_result_incomplete")
        choices = [row["id"] for row in rows]
        selections = ("currentValue",)
    else:
        raise StateError("native_bridge_result_incomplete")
    if (
        not isinstance(choices, list)
        or not choices
        or any(type(c) not in (str, bool) or c == "" for c in choices)
    ):
        raise StateError("native_bridge_model_options_unproven")
    typed = [(type(c), c) for c in choices]
    if len(set(typed)) != len(typed) or any(
        option.get(key) is not None and (type(option[key]), option[key]) not in typed
        for key in selections
    ):
        raise StateError("native_bridge_model_options_incoherent")
    return choices
