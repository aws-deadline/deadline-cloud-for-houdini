# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

from __future__ import annotations

import os
from types import FrameType
from typing import Optional

# The Houdini Adaptor adds the `openjd` namespace directory to PYTHONPATH,
# so that importing just the adaptor_runtime_client should work.
try:
    from adaptor_runtime_client import (  # type: ignore[import]
        HTTPClientInterface,
    )
    from houdini_adaptor.HoudiniClient.houdini_handler import HoudiniHandler  # type: ignore[import]
except ImportError:
    from openjd.adaptor_runtime_client import (
        HTTPClientInterface,
    )
    from deadline.houdini_adaptor.HoudiniClient.houdini_handler import HoudiniHandler

try:
    import hou  # type: ignore
except ImportError:  # pragma: no cover
    raise OSError("Could not find the Houdini module. Are you running this inside of Houdini?")


class HoudiniClient(HTTPClientInterface):
    """
    Client that runs in Houdini for the Houdini Adaptor
    """

    def __init__(self, socket_path: str) -> None:
        super().__init__(socket_path=socket_path)
        self.actions.update(HoudiniHandler().action_dict)

    def close(self, args: Optional[dict] = None) -> None:
        hou.exit()

    def graceful_shutdown(self, signum: int, frame: FrameType | None):
        hou.exit()


def main():
    socket_path = os.environ.get("HOUDINI_ADAPTOR_SOCKET_PATH")
    if not socket_path:
        raise OSError(
            "HoudiniClient cannot connect to the Adaptor because the environment variable "
            "HOUDINI_ADAPTOR_SOCKET_PATH does not exist"
        )

    if not os.path.exists(socket_path):
        raise OSError(
            "HoudiniClient cannot connect to the Adaptor because the socket at the path defined by "
            "the environment variable HOUDINI_ADAPTOR_SOCKET_PATH does not exist. Got: "
            f"{os.environ['HOUDINI_ADAPTOR_SOCKET_PATH']}"
        )

    client = HoudiniClient(socket_path)
    client.poll()


if __name__ == "__main__":  # pragma: no cover
    main()
