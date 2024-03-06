# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

import logging as _logging
import sys as _sys

from openjd.adaptor_runtime import EntryPoint as _EntryPoint

from .adaptor import HoudiniAdaptor

__all__ = ["main"]
_logger = _logging.getLogger(__name__)


def main(reentry_exe=None) -> int:
    """
    Entry point for the Houdini Adaptor
    """
    _logger.info("About to start the HoudiniAdaptor")

    package_name = vars(_sys.modules[__name__])["__package__"]
    if not package_name:
        raise RuntimeError(f"Must be run as a module. Do not run {__file__} directly")

    try:
        _EntryPoint(HoudiniAdaptor).start(reentry_exe=reentry_exe)
    except Exception as e:
        _logger.error(f"Entrypoint failed: {e}")
        return 1

    _logger.info("Done HoudiniAdaptor main")
    return 0


if __name__ == "__main__":
    _sys.exit(main())
