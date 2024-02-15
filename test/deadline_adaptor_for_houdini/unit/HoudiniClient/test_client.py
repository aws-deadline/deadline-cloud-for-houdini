# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

from __future__ import annotations

import os
from unittest.mock import Mock, patch

import pytest
from .mock_hou import hou_module as hou  # noqa:F401

from deadline.houdini_adaptor.HoudiniClient.houdini_client import HoudiniClient, main


class TestHoudiniClient:
    @patch("deadline.houdini_adaptor.HoudiniClient.houdini_client.HTTPClientInterface")
    def test_houdiniclient(self, mock_httpclient: Mock) -> None:
        """Tests that the houdini client can initialize, set a renderer and close"""
        client = HoudiniClient(server_path=str(9999))
        client.close()

    @patch("deadline.houdini_adaptor.HoudiniClient.houdini_client.os.path.exists")
    @patch.dict(os.environ, {"HOUDINI_ADAPTOR_SERVER_PATH": "server_path"})
    @patch("deadline.houdini_adaptor.HoudiniClient.HoudiniClient.poll")
    @patch("deadline.houdini_adaptor.HoudiniClient.houdini_client.HTTPClientInterface")
    def test_main(self, mock_httpclient: Mock, mock_poll: Mock, mock_exists: Mock) -> None:
        """Tests that the main method starts the houdini client polling method"""
        # GIVEN
        mock_exists.return_value = True

        # WHEN
        main()

        # THEN
        mock_exists.assert_called_once_with("server_path")
        mock_poll.assert_called_once()

    @patch.dict(os.environ, {}, clear=True)
    @patch("deadline.houdini_adaptor.HoudiniClient.HoudiniClient.poll")
    def test_main_no_server_socket(self, mock_poll: Mock) -> None:
        """Tests that the main method raises an OSError if no server socket is found"""
        # WHEN
        with pytest.raises(OSError) as exc_info:
            main()

        # THEN
        assert str(exc_info.value) == (
            "HoudiniClient cannot connect to the Adaptor because the environment variable "
            "HOUDINI_ADAPTOR_SERVER_PATH does not exist"
        )
        mock_poll.assert_not_called()

    @patch.dict(os.environ, {"HOUDINI_ADAPTOR_SERVER_PATH": "/a/path/that/does/not/exist"})
    @patch("deadline.houdini_adaptor.HoudiniClient.houdini_client.os.path.exists")
    @patch("deadline.houdini_adaptor.HoudiniClient.HoudiniClient.poll")
    def test_main_server_socket_not_exists(self, mock_poll: Mock, mock_exists: Mock) -> None:
        """Tests that the main method raises an OSError if the server socket does not exist"""
        # GIVEN
        mock_exists.return_value = False

        # WHEN
        with pytest.raises(OSError) as exc_info:
            main()

        # THEN
        mock_exists.assert_called_once_with(os.environ["HOUDINI_ADAPTOR_SERVER_PATH"])
        assert str(exc_info.value) == (
            "HoudiniClient cannot connect to the Adaptor because the socket at the path defined by "
            "the environment variable HOUDINI_ADAPTOR_SERVER_PATH does not exist. Got: "
            f"{os.environ['HOUDINI_ADAPTOR_SERVER_PATH']}"
        )
        mock_poll.assert_not_called()
