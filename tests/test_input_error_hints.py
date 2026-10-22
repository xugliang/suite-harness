import json
import pytest
from jsonschema import Draft202012Validator
from suiteharness.execution.input_errors import input_error_hint


@pytest.mark.parametrize('schema,value,rule,expected', [
    ({'type':'integer','maximum':100},200,'maximum',100),
    ({'type':'integer','minimum':0},-1,'minimum',0),
    ({'type':'integer'},'private-secret','type','integer'),
    ({'type':'string','pattern':'^safe$'},'private-secret','pattern',None),
])
def test_safe_field_hint(schema, value, rule, expected):
    validator = Draft202012Validator({'type':'object','properties':{'limit':schema}})
    error = next(validator.iter_errors({'limit':value}))
    message, details = input_error_hint(error)
    assert details['validation_path'] == 'limit'
    assert details['validation_rule'] == rule
    assert details.get('validation_constraint') == expected
    assert 'private-secret' not in message
    assert 'not executed' in message


def test_unknown_keys_and_values_are_not_logged():
    validator = Draft202012Validator({'type':'object','additionalProperties':{'type':'integer'}})
    message, details = input_error_hint(next(validator.iter_errors({'private-secret':'secret-value'})))
    assert details['validation_path'] == 'root'
    assert 'private-secret' not in message and 'secret-value' not in message


def test_required_and_nested_hint():
    schema = {'type':'object','properties':{'request':{'type':'object','required':['version_id']}}}
    message, details = input_error_hint(next(Draft202012Validator(schema).iter_errors({'request':{}})))
    assert details == {'validation_path':'request','validation_rule':'required','validation_constraint':'version_id'}


def test_diagnostic_accepts_only_schema_metadata():
    from suiteharness.diagnostics import _safe
    assert _safe({'validation_path':'limit','validation_rule':'maximum','validation_constraint':100,
                  'arguments':{'password':'secret'}}) == {
                      'validation_path':'limit','validation_rule':'maximum','validation_constraint':100}
