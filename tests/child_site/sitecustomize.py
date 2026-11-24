import os
import sys

_DEPLOYED = os.path.realpath(os.path.join(os.path.dirname(__file__), "..", "..", "src", "pricing.json"))


def _refuse(event, args):
    if event == "open" and str(args[0]).endswith("pricing.json") and os.path.realpath(args[0]) == _DEPLOYED:
        raise RuntimeError("a test child opened the deployed rate document: import tests.bounded_pricing in it")


sys.addaudithook(_refuse)
