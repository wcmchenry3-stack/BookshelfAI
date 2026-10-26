"""Labelled scan evaluation (#536): one model on the owner's shelf photos.

See ``backend/eval/README.md`` for the dataset layout and owner setup, and
``docs/testing.md`` ("AI scan evaluation") for how and when to run it.
"""


class EvalError(Exception):
    """A refusal or failure the CLI reports with its message.

    Every message is written to be safe to print: it never holds a bucket
    name, endpoint, object key, credential, title or author."""
