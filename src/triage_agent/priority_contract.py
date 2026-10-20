"""Provisional bug priority output contract; no severity inference or model calls."""
from __future__ import annotations

from .review_local import strict_json

CONTRACT_VERSION = 'priority-bug-v0-draft'
GRADES = ('high', 'medium', 'low')
SCHEMA = {
    'type': 'object',
    'properties': {
        'priority': {'type': ['string', 'null'], 'enum': [*GRADES, None]},
        'rationale': {'type': 'string', 'minLength': 1},
        'needs_review': {'type': 'boolean'},
        'missing_information': {'type': 'array', 'maxItems': 3,
                                'items': {'type': 'string', 'minLength': 1}},
    },
    'required': ['priority', 'rationale', 'needs_review', 'missing_information'],
    'additionalProperties': False,
}


def valid_prediction(value):
    """Check field types AND consistency; JSON Schema alone is insufficient."""
    if not isinstance(value, dict) or set(value) != set(SCHEMA['required']):
        return False
    if not isinstance(value['rationale'], str) or not value['rationale'].strip():
        return False
    if type(value['needs_review']) is not bool:
        return False
    questions = value['missing_information']
    if not isinstance(questions, list) or not all(isinstance(q, str) and q.strip() for q in questions):
        return False
    if value['priority'] is None:
        return value['needs_review'] and 1 <= len(questions) <= 3
    return (isinstance(value['priority'], str) and value['priority'] in GRADES
            and not value['needs_review'] and not questions)
