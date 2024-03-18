# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
import sys
import types
from unittest.mock import MagicMock, Mock

module_name = "hou"
hou_module = types.ModuleType(module_name)
sys.modules[module_name] = hou_module
this_module = sys.modules[module_name]
# Usage set mocked names here, set mocked return values/properties in unit tests.
# Mocked names
setattr(this_module, "exit", Mock(name=module_name + ".exit"))
setattr(this_module, "logging", Mock(name=module_name + ".logging"))
setattr(this_module, "renderMethod", Mock(name=module_name + ".renderMethod"))
setattr(this_module, "node", MagicMock(name=module_name + ".node"))
setattr(this_module, "hipFile", Mock(name=module_name + ".hipFile"))
setattr(this_module, "LoadWarning", Mock(name=module_name + ".LoadWarning"))
setattr(this_module, "Node", Mock(name=module_name + ".Node"))
setattr(
    this_module, "applicationVersionString", Mock(name=module_name + ".applicationVersionString")
)
