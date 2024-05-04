# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

from __future__ import annotations

import os
from types import FrameType
from typing import Optional

# The Houdini Adaptor adds the `openjd` namespace directory to PYTHONPATH,
# so that importing just the adaptor_runtime_client should work.
try:
    from adaptor_runtime_client import (  # type: ignore[import]
        ClientInterface,
    )
    from houdini_adaptor.HoudiniClient.houdini_handler import HoudiniHandler  # type: ignore[import]
except ImportError:
    from openjd.adaptor_runtime_client import (
        ClientInterface,
    )
    from deadline.houdini_adaptor.HoudiniClient.houdini_handler import HoudiniHandler

try:
    import hou  # type: ignore
except ImportError:  # pragma: no cover
    raise OSError("Could not find the Houdini module. Are you running this inside of Houdini?")


class HoudiniClient(ClientInterface):
    """
    Client that runs in Houdini for the Houdini Adaptor
    """

    def __init__(self, server_path: str) -> None:
        super().__init__(server_path=server_path)
        print(f"HoudiniClient: Houdini Version {hou.applicationVersionString()}")
        self.actions.update(HoudiniHandler().action_dict)

    def close(self, args: Optional[dict] = None) -> None:
        hou.exit()

    def graceful_shutdown(self, signum: int, frame: FrameType | None):
        hou.exit()


def main():
    server_path = os.environ.get("HOUDINI_ADAPTOR_SERVER_PATH")
    if not server_path:
        raise OSError(
            "HoudiniClient cannot connect to the Adaptor because the environment variable "
            "HOUDINI_ADAPTOR_SERVER_PATH does not exist"
        )

    if not os.path.exists(server_path):
        raise OSError(
            "HoudiniClient cannot connect to the Adaptor because the socket at the path defined by "
            "the environment variable HOUDINI_ADAPTOR_SERVER_PATH does not exist. Got: "
            f"{os.environ['HOUDINI_ADAPTOR_SERVER_PATH']}"
        )

    client = HoudiniClient(server_path)
    client.poll()


if __name__ == "__main__":  # pragma: no cover
    main()
