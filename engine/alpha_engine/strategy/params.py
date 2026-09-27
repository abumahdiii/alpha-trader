"""Strategy parameter schema: typed specs, safe validation, and a stable provenance hash.

A strategy declares an ordered :class:`ParamSchema` of :class:`ParamSpec` entries. User-supplied values
(from the UI / API, i.e. JSON) go through :meth:`ParamSchema.validate`, which

* fills defaults for missing keys,
* rejects unknown keys,
* coerces types safely -- never ``True -> 1`` or ``1 -> True``; a float is accepted for an ``int`` only
  when it is integral (``14.0`` -> ``14``); numeric strings are accepted (``"14"``); NaN/inf are rejected,
* enforces inclusive ``min``/``max`` bounds and ``choices``.

``step`` is a UI increment hint only; it is NOT enforced (a float grid check would reject legitimate
values such as ``0.30000000000000004``).

:func:`params_hash` is the sha256 of the canonical JSON of the *validated* values (sorted keys, no
whitespace, NaN forbidden). Hash only validated values: validation normalises types, so ``2`` and
``2.0`` for a float param both hash as ``2.0``.

Error messages are Persian because they are shown to the user as-is.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Iterator, Mapping
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

ParamType = Literal["int", "float", "bool", "choice"]
ParamValue = bool | int | float | str

_NAME_RE = r"^[a-z][a-z0-9_]*$"
_INT_STR_RE = re.compile(r"^[+-]?\d+$")
_TRUE_STRINGS = frozenset({"true"})
_FALSE_STRINGS = frozenset({"false"})


def _is_number(value: Any) -> bool:
    """int or float, but never bool (bool is an int subclass in Python)."""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _fmt(value: Any) -> str:
    return repr(value) if isinstance(value, str) else str(value)


class ParamSpec(BaseModel):
    """One tunable strategy parameter."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(pattern=_NAME_RE)
    type: ParamType
    default: ParamValue
    min: float | None = None
    max: float | None = None
    choices: tuple[ParamValue, ...] | None = None
    step: float | None = Field(default=None, gt=0)
    label_fa: str = Field(min_length=1)
    description_fa: str = ""

    @model_validator(mode="after")
    def _check_consistency(self) -> ParamSpec:
        numeric = self.type in ("int", "float")
        if not numeric and (self.min is not None or self.max is not None or self.step is not None):
            raise ValueError(f"param {self.name!r}: min/max/step are only allowed for int/float")
        if self.type == "choice":
            if not self.choices:
                raise ValueError(f"param {self.name!r}: a choice param needs non-empty choices")
            if len(set(map(_choice_key, self.choices))) != len(self.choices):
                raise ValueError(f"param {self.name!r}: duplicate choices")
        elif self.choices is not None:
            raise ValueError(f"param {self.name!r}: choices are only allowed for type 'choice'")
        for bound in (self.min, self.max):
            if bound is not None and not math.isfinite(bound):
                raise ValueError(f"param {self.name!r}: bounds must be finite")
        if self.min is not None and self.max is not None and self.min > self.max:
            raise ValueError(f"param {self.name!r}: min > max")
        coerced, error = self.coerce(self.default)
        if error is not None:
            raise ValueError(f"param {self.name!r}: invalid default ({self.default!r})")
        if coerced != self.default or type(coerced) is not type(self.default):
            raise ValueError(f"param {self.name!r}: default must already have the declared type")
        return self

    def coerce(self, value: Any) -> tuple[ParamValue | None, str | None]:
        """Return ``(clean_value, None)`` or ``(None, persian_error)``. Never raises."""
        label = f"«{self.label_fa}» ({self.name})"
        if value is None:
            return None, f"مقدار پارامتر {label} نمی‌تواند خالی باشد."

        if self.type == "bool":
            if isinstance(value, bool):
                return value, None
            if isinstance(value, str) and value.strip().lower() in _TRUE_STRINGS | _FALSE_STRINGS:
                return value.strip().lower() in _TRUE_STRINGS, None
            return None, f"پارامتر {label} باید true یا false باشد (مقدار داده‌شده: {_fmt(value)})."

        if self.type == "choice":
            assert self.choices is not None
            for choice in self.choices:
                if _choice_key(choice) == _choice_key(value):
                    return choice, None
            allowed = "، ".join(_fmt(c) for c in self.choices)
            return None, f"پارامتر {label} باید یکی از این مقادیر باشد: {allowed} (مقدار داده‌شده: {_fmt(value)})."

        number = self._coerce_number(value)
        if number is None:
            kind = "عدد صحیح" if self.type == "int" else "عدد"
            return None, f"پارامتر {label} باید {kind} باشد (مقدار داده‌شده: {_fmt(value)})."
        if self.min is not None and number < self.min:
            return None, f"پارامتر {label} نباید کمتر از {_num(self.min)} باشد (مقدار داده‌شده: {_num(number)})."
        if self.max is not None and number > self.max:
            return None, f"پارامتر {label} نباید بیشتر از {_num(self.max)} باشد (مقدار داده‌شده: {_num(number)})."
        return number, None

    def _coerce_number(self, value: Any) -> int | float | None:
        if isinstance(value, bool):
            return None  # no bool -> number surprises
        if isinstance(value, str):
            text = value.strip()
            if self.type == "int":
                return int(text) if _INT_STR_RE.match(text) else None
            try:
                value = float(text)
            except ValueError:
                return None
        if not _is_number(value):
            return None
        if isinstance(value, float) and not math.isfinite(value):
            return None
        if self.type == "int":
            if isinstance(value, float):
                return int(value) if value.is_integer() else None
            return int(value)
        return float(value)


def _choice_key(value: Any) -> tuple[str, Any]:
    """Equality key that keeps ``True`` and ``1`` apart (``True == 1`` in Python)."""
    if isinstance(value, bool):
        return ("bool", value)
    if _is_number(value):
        return ("num", float(value))
    return (type(value).__name__, value)


def _num(value: float | int) -> str:
    if isinstance(value, float) and value.is_integer():
        return str(int(value)) if abs(value) < 1e15 else str(value)
    return str(value)


class ParamSchema:
    """Ordered, name-unique collection of :class:`ParamSpec`."""

    __slots__ = ("_specs", "_by_name")

    def __init__(self, specs: list[ParamSpec] | tuple[ParamSpec, ...] = ()) -> None:
        specs = tuple(specs)
        names = [s.name for s in specs]
        duplicates = sorted({n for n in names if names.count(n) > 1})
        if duplicates:
            raise ValueError(f"duplicate parameter names: {duplicates}")
        self._specs: tuple[ParamSpec, ...] = specs
        self._by_name: dict[str, ParamSpec] = {s.name: s for s in specs}

    def __iter__(self) -> Iterator[ParamSpec]:
        return iter(self._specs)

    def __len__(self) -> int:
        return len(self._specs)

    def __contains__(self, name: object) -> bool:
        return name in self._by_name

    def __repr__(self) -> str:
        return f"ParamSchema({[s.name for s in self._specs]})"

    @property
    def specs(self) -> tuple[ParamSpec, ...]:
        return self._specs

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(s.name for s in self._specs)

    def get(self, name: str) -> ParamSpec:
        return self._by_name[name]

    def defaults(self) -> dict[str, ParamValue]:
        return {s.name: s.default for s in self._specs}

    def to_list(self) -> list[dict[str, Any]]:
        """JSON-ready schema for the UI."""
        return [s.model_dump(mode="json") for s in self._specs]

    def validate(self, values: Mapping[str, Any] | None) -> tuple[dict[str, ParamValue], list[str]]:
        """Validate user values against this schema.

        Returns ``(clean_values, errors_fa)``. On success ``errors_fa`` is empty and ``clean_values``
        holds every schema param in schema order. On any error ``clean_values`` is ``{}`` so a
        half-valid parameter set can never be used by accident.
        """
        if values is None:
            values = {}
        if not isinstance(values, Mapping):
            return {}, ["پارامترها باید به‌صورت یک شیء کلید-مقدار ارسال شوند."]

        errors: list[str] = []
        for key in values:
            if not isinstance(key, str) or key not in self._by_name:
                errors.append(f"پارامتر ناشناخته: {_fmt(key)}.")

        clean: dict[str, ParamValue] = {}
        for spec in self._specs:
            if spec.name not in values:
                clean[spec.name] = spec.default
                continue
            value, error = spec.coerce(values[spec.name])
            if error is not None:
                errors.append(error)
            else:
                assert value is not None
                clean[spec.name] = value

        if errors:
            return {}, errors
        return clean, []


def canonical_params_json(values: Mapping[str, Any]) -> str:
    """Canonical JSON: sorted keys, compact separators, UTF-8 kept, NaN/inf rejected."""
    return json.dumps(dict(values), sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def params_hash(values: Mapping[str, Any]) -> str:
    """Stable sha256 hex digest of the canonical JSON of (validated) params, for provenance."""
    return hashlib.sha256(canonical_params_json(values).encode("utf-8")).hexdigest()
