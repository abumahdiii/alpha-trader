from __future__ import annotations

import hashlib
import math

import pytest
from pydantic import ValidationError

from alpha_engine.strategy.params import ParamSchema, ParamSpec, canonical_params_json, params_hash


def _schema() -> ParamSchema:
    return ParamSchema([
        ParamSpec(name="channel_period", type="int", default=100, min=20, max=500, step=1, label_fa="دوره کانال"),
        ParamSpec(name="k", type="float", default=2.0, min=0.5, max=4.0, step=0.1, label_fa="ضریب انحراف معیار"),
        ParamSpec(name="use_engulfing", type="bool", default=True, label_fa="اینگالف"),
        ParamSpec(name="sigma_mode", type="choice", default="population", choices=("population", "sample"),
                  label_fa="روش سیگما"),
    ])


def test_defaults_filled_in_schema_order() -> None:
    clean, errors = _schema().validate({})
    assert errors == []
    assert list(clean) == ["channel_period", "k", "use_engulfing", "sigma_mode"]
    assert clean == {"channel_period": 100, "k": 2.0, "use_engulfing": True, "sigma_mode": "population"}
    assert _schema().validate(None) == (clean, [])


def test_valid_values_and_safe_coercion() -> None:
    clean, errors = _schema().validate(
        {"channel_period": 150.0, "k": 3, "use_engulfing": "false", "sigma_mode": "sample"}
    )
    assert errors == []
    assert clean["channel_period"] == 150 and type(clean["channel_period"]) is int
    assert clean["k"] == 3.0 and type(clean["k"]) is float
    assert clean["use_engulfing"] is False
    assert clean["sigma_mode"] == "sample"
    # numeric strings are accepted
    clean, errors = _schema().validate({"channel_period": " 120 ", "k": "2.5"})
    assert errors == [] and clean["channel_period"] == 120 and clean["k"] == 2.5


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("channel_period", True),      # bool is not an int
        ("channel_period", 100.5),     # non-integral float
        ("channel_period", "1e2"),
        ("channel_period", "abc"),
        ("k", False),                  # bool is not a float
        ("k", float("nan")),
        ("k", float("inf")),
        ("k", "nan"),
        ("use_engulfing", 1),          # int is not a bool
        ("use_engulfing", 0),
        ("use_engulfing", "yes"),
        ("sigma_mode", "Sample"),
        ("sigma_mode", 1),
        ("channel_period", None),
        ("k", [2.0]),
    ],
)
def test_bad_types_rejected(key: str, value: object) -> None:
    clean, errors = _schema().validate({key: value})
    assert clean == {}
    assert len(errors) == 1
    assert key in errors[0]


def test_bounds_inclusive() -> None:
    schema = _schema()
    assert schema.validate({"channel_period": 20, "k": 0.5})[1] == []
    assert schema.validate({"channel_period": 500, "k": 4.0})[1] == []
    for values in ({"channel_period": 19}, {"channel_period": 501}, {"k": 0.49}, {"k": 4.01}):
        clean, errors = schema.validate(values)
        assert clean == {} and len(errors) == 1
    _, errors = schema.validate({"channel_period": 19})
    assert "نباید کمتر از 20" in errors[0]
    _, errors = schema.validate({"k": 4.01})
    assert "نباید بیشتر از 4" in errors[0]


def test_unknown_keys_and_multiple_errors_reported() -> None:
    clean, errors = _schema().validate({"foo": 1, "channel_period": 5, 3: "x"})
    assert clean == {}
    assert len(errors) == 3
    assert any("پارامتر ناشناخته" in e and "'foo'" in e for e in errors)


def test_non_mapping_rejected() -> None:
    clean, errors = _schema().validate([("k", 2.0)])  # type: ignore[arg-type]
    assert clean == {} and len(errors) == 1


def test_choice_true_vs_one_are_distinct() -> None:
    schema = ParamSchema([ParamSpec(name="mode", type="choice", default=1, choices=(1, 2), label_fa="حالت")])
    assert schema.validate({"mode": True})[0] == {}
    assert schema.validate({"mode": 2})[0] == {"mode": 2}
    assert schema.validate({"mode": 2.0})[0] == {"mode": 2}


@pytest.mark.parametrize(
    "kwargs",
    [
        dict(name="p", type="int", default=5, min=10, label_fa="x"),              # default out of bounds
        dict(name="p", type="float", default=2, label_fa="x"),                    # default not float
        dict(name="p", type="int", default=True, label_fa="x"),                   # bool default for int
        dict(name="p", type="int", default=1, min=5, max=1, label_fa="x"),        # min > max
        dict(name="p", type="bool", default=True, min=0, label_fa="x"),           # bounds on bool
        dict(name="p", type="choice", default="a", label_fa="x"),                 # no choices
        dict(name="p", type="choice", default="c", choices=("a", "b"), label_fa="x"),
        dict(name="p", type="int", default=1, choices=(1, 2), label_fa="x"),      # choices on int
        dict(name="P", type="int", default=1, label_fa="x"),                      # bad name
        dict(name="p", type="int", default=1, label_fa=""),                       # empty label
        dict(name="p", type="float", default=1.0, step=0, label_fa="x"),          # step must be > 0
    ],
)
def test_bad_specs_rejected(kwargs: dict) -> None:
    with pytest.raises(ValidationError):
        ParamSpec(**kwargs)


def test_duplicate_names_rejected() -> None:
    spec = ParamSpec(name="p", type="int", default=1, label_fa="x")
    with pytest.raises(ValueError):
        ParamSchema([spec, spec])


def test_schema_container_helpers() -> None:
    schema = _schema()
    assert len(schema) == 4 and "k" in schema and "zz" not in schema
    assert schema.names == ("channel_period", "k", "use_engulfing", "sigma_mode")
    assert schema.get("k").label_fa == "ضریب انحراف معیار"
    listed = schema.to_list()
    assert listed[0]["name"] == "channel_period" and listed[3]["choices"] == ["population", "sample"]


def test_params_hash_stable_and_order_independent() -> None:
    a = {"k": 2.0, "channel_period": 100, "use_engulfing": True, "sigma_mode": "population"}
    b = {"sigma_mode": "population", "use_engulfing": True, "channel_period": 100, "k": 2.0}
    assert params_hash(a) == params_hash(b)
    # Worked example: exact canonical form and digest.
    canonical = '{"channel_period":100,"k":2.0,"sigma_mode":"population","use_engulfing":true}'
    assert canonical_params_json(a) == canonical
    assert params_hash(a) == hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    assert len(params_hash(a)) == 64
    assert params_hash({**a, "k": 2.1}) != params_hash(a)


def test_params_hash_after_validation_normalises_types() -> None:
    schema = _schema()
    h1 = params_hash(schema.validate({"k": 2})[0])
    h2 = params_hash(schema.validate({"k": 2.0})[0])
    h3 = params_hash(schema.validate({})[0])
    assert h1 == h2 == h3


def test_params_hash_rejects_nan() -> None:
    with pytest.raises(ValueError):
        params_hash({"k": math.nan})
