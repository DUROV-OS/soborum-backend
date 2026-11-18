"""Stable fingerprints of the exact input used by an automated analysis."""
import hashlib
import json


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(',', ':'), default=str).encode()).hexdigest()


ANALYSIS_VERSION = 'section-evidence-v1'
