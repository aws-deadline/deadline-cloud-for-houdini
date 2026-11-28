# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
"""Tests for the Skip output folders, keep loaded caches checkbox and the Excluded input paths
box, on fake Qt classes and a fake of the Job attachments tab's layout."""
import contextlib
import importlib
import importlib.util
import logging
import pathlib
import re
import sys
import types
from typing import Any, Callable, Dict, List
from unittest import mock

import pytest

from .mock_hou import hou_module as hou
from .test_assets_auto_inputs import FakeNode, FakeParm, make_rop
from deadline.client.job_bundle.submission import AssetReferences
from deadline.houdini_submitter.python.deadline_cloud_for_houdini import excluded_input_paths_box
from deadline.houdini_submitter.python.deadline_cloud_for_houdini.excluded_input_paths_box import (
    EXCLUDED_INPUT_PATHS_HELP,
    EXCLUDED_INPUT_PATHS_PLACEHOLDER,
    EXCLUDED_INPUT_PATHS_TITLE,
    SKIP_OUTPUTS_KEEP_CACHES_HELP,
    SKIP_OUTPUTS_KEEP_CACHES_TITLE,
    _save_and_refresh,
    add_auto_input_controls,
    add_to_dialog_top,
)


class _Signal(list):
    connect = list.append

    def emit(self, *args: Any) -> None:
        for slot in self:
            slot(*args)


class _Widget:
    """Records each setter call by name, and adds itself to a parent widget given last."""

    def __init__(self, *args: Any) -> None:
        self.args = args
        self.calls: Dict[str, tuple] = {}
        self.children: List[Any] = []
        self.text, self.checked, self.started = "", False, 0
        self.textChanged, self.toggled, self.timeout = _Signal(), _Signal(), _Signal()
        if args and isinstance(args[-1], _Widget):
            args[-1].children.append(self)

    def __getattr__(self, name: str) -> Callable:
        return lambda *a: self.calls.__setitem__(name, a)

    def title(self) -> str:
        return self.args[0]

    def fontMetrics(self) -> Any:
        return types.SimpleNamespace(lineSpacing=lambda: 16)

    def setPlainText(self, text: str) -> None:
        self.text = text
        self.textChanged.emit()

    def toPlainText(self) -> str:
        return self.text

    def setChecked(self, checked: bool) -> None:
        self.checked = checked

    def click(self) -> None:
        self.checked = not self.checked
        self.toggled.emit(self.checked)

    def start(self) -> None:
        self.started += 1


@pytest.fixture
def fake_qt():
    core = types.SimpleNamespace(QTimer=_Widget)
    widgets = types.SimpleNamespace(
        QCheckBox=_Widget,
        QGroupBox=_Widget,
        QPlainTextEdit=_Widget,
        QVBoxLayout=lambda parent: mock.Mock(),
        QSizePolicy=types.SimpleNamespace(Preferred="Preferred", Maximum="Maximum"),
    )
    qtpy = types.SimpleNamespace(QtCore=core, QtWidgets=widgets)
    with mock.patch.dict(
        sys.modules, {"qtpy": qtpy, "qtpy.QtCore": core, "qtpy.QtWidgets": widgets}
    ):
        yield


class _Layout(list):
    """The QVBoxLayout that JobAttachmentsWidget._build_ui fills with its groups."""

    insertWidget = list.insert

    def indexOf(self, widget: Any) -> int:
        return self.index(widget) if widget in self else -1


# The tab's groups in German, so placement can't depend on the English titles.
_GERMAN = [
    "Allgemeine Übermittlungseinstellungen",
    "Eingabedateien anhängen",
    "Eingabeverzeichnisse anhängen",
    "Ausgabeverzeichnisse angeben",
]


def _dialog(input_directories: int = 2) -> Any:
    """The tab's groups, and in General submission settings its row of general settings."""
    groups = [_Widget(t) for t in _GERMAN]
    general_settings = _Widget("Erforderlich: alle Eingabepfade vorhanden")
    general_group = mock.Mock(**{"layout.return_value": _Layout([general_settings])})
    ja = mock.Mock(general_group=general_group, general_settings=general_settings)
    ja.attachments = AssetReferences()
    ja.input_directories_group = groups[input_directories]
    ja.layout.return_value = _Layout(groups)
    return mock.Mock(job_attachments=ja)


def _titles(dialog: Any) -> List[str]:
    return [w.title() for w in dialog.job_attachments.layout()]


def _child(box: Any, name: str) -> Any:
    return next(c for c in box.children if name in c.calls)


@pytest.mark.parametrize(("input_directories", "box_at"), [(2, 3), (0, 1)])
def test_each_control_lands_by_its_anchor_in_any_language(
    fake_qt, caplog, input_directories, box_at
):
    """The checkbox goes right under Require all input paths exist and the box right after
    Attach input directories, found by the widget's attributes, not their translated titles."""
    dialog = _dialog(input_directories)
    ja = dialog.job_attachments
    fallback = mock.Mock()

    with caplog.at_level(logging.WARNING):
        checkbox, box = add_auto_input_controls(dialog, make_rop(0), fallback)

    assert ja.general_group.layout() == [ja.general_settings, checkbox]
    assert ja.layout().index(box) == box_at
    assert [w.title() for w in ja.layout() if w is not box] == _GERMAN
    fallback.assert_not_called()
    assert caplog.records == []


@pytest.mark.parametrize(
    ("problem", "titles", "fallback_used", "warnings"),
    [
        *(
            pytest.param(
                missing,
                [SKIP_OUTPUTS_KEEP_CACHES_TITLE, EXCLUDED_INPUT_PATHS_TITLE],
                True,
                [
                    "Couldn't find Require all input paths exist in the Job attachments tab",
                    "Couldn't find Attach input directories in the Job attachments tab",
                ],
                id=missing,
            )
            for missing in ("attribute", "not in layout", "no layout", "no tab")
        ),
        pytest.param(
            "the checkbox can't be built",
            [EXCLUDED_INPUT_PATHS_TITLE],
            False,
            ["Couldn't add the Skip output folders, keep loaded caches checkbox"],
            id="the checkbox can't be built",
        ),
        pytest.param(
            "placing fails",
            [],
            False,
            [
                "Couldn't add the Skip output folders, keep loaded caches checkbox: no Qt",
                "Couldn't add the Excluded input paths box: no Qt",
            ],
            id="placing fails",
        ),
    ],
)
def test_a_control_without_its_group_uses_the_fallback_and_an_error_skips_only_that_control(
    fake_qt, caplog, problem, titles, fallback_used, warnings
):
    """Each control that can't find its group warns once and goes to the fallback. An error
    while adding one control is logged, and the other control is still added."""
    dialog = _dialog()
    ja = dialog.job_attachments
    patch: Any = contextlib.nullcontext()
    if problem == "attribute":
        del ja.general_settings, ja.input_directories_group
    elif problem == "not in layout":
        ja.general_settings = ja.input_directories_group = _Widget("other")
    elif problem == "no layout":
        ja.layout.return_value = None
        ja.general_group.layout.return_value = None
    elif problem == "no tab":
        dialog = mock.Mock(spec=[])
    elif problem == "the checkbox can't be built":
        # A QCheckBox that can't be built stands for any error while adding the checkbox.
        patch = mock.patch.object(sys.modules["qtpy.QtWidgets"], "QCheckBox", None)
    else:
        patch = mock.patch.object(
            excluded_input_paths_box, "_insert_after", side_effect=RuntimeError("no Qt")
        )
    fallback = mock.Mock()

    with patch, caplog.at_level(logging.WARNING):
        controls = add_auto_input_controls(dialog, make_rop(0), fallback)

    assert [c.args for c in controls] == [(title,) for title in titles]
    assert fallback.call_args_list == ([mock.call(c) for c in controls] if fallback_used else [])
    messages = [r.getMessage() for r in caplog.records]
    assert len(messages) == len(warnings)
    assert all(m.startswith(w) for m, w in zip(messages, warnings))


@pytest.mark.parametrize(
    ("missing_parm", "titles", "warnings"),
    [
        pytest.param(None, [], 2, id="parms can't be read"),
        pytest.param(
            "skip_outputs_keep_caches", [EXCLUDED_INPUT_PATHS_TITLE], 0, id="no toggle parm"
        ),
        pytest.param(
            "excluded_input_paths", [SKIP_OUTPUTS_KEEP_CACHES_TITLE], 0, id="no excluded paths parm"
        ),
    ],
)
def test_a_node_gets_a_control_only_for_each_parm_it_has(
    fake_qt, caplog, missing_parm, titles, warnings
):
    """A node without a parm gets no control for it. A node whose parms can't be read, such as a
    deleted one, gets no controls and one warning for each."""
    node = make_rop(1)
    if missing_parm is None:
        node.parm.side_effect = RuntimeError("deleted")
    else:
        del node.parms[missing_parm]

    with caplog.at_level(logging.WARNING):
        controls = add_auto_input_controls(_dialog(), node, mock.Mock())

    assert [c.args for c in controls] == [(title,) for title in titles]
    assert len(caplog.records) == warnings


def _scene_with_cache(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("DEADLINE_HOUDINI_EXCLUDED_INPUT_PATHS", raising=False)
    monkeypatch.setattr(
        hou.text.expandString, "side_effect", lambda s: s.replace("$HIP", str(tmp_path))
    )
    (tmp_path / "cache").mkdir()
    hou.hipFile.path.return_value = str(tmp_path / "scene.hip")
    cache = FakeNode("/obj/a").add("dir", FakeParm("dir", str(tmp_path / "cache")))
    monkeypatch.setattr(hou.fileReferences, "side_effect", lambda: [(cache, cache.raw)])


def test_the_box_filters_with_the_toggle_off_and_saves_each_edit_before_the_pause(
    fake_qt, tmp_path, monkeypatch
):
    """An edit is saved to the node at once, so closing the pane keeps it. The pause, or a press
    on Submit or Save bundle as during it, refreshes the lists with a scan that leaves the cache
    out."""
    _scene_with_cache(tmp_path, monkeypatch)
    node = make_rop(0, patterns=["$HIP/old"])
    dialog = _dialog()

    _, box = add_auto_input_controls(dialog, node, mock.Mock())
    edit, timer = _child(box, "setPlaceholderText"), _child(box, "setSingleShot")

    assert edit.toPlainText() == "$HIP/old"
    assert box.children == [edit, timer]
    assert edit.calls["setPlaceholderText"] == (EXCLUDED_INPUT_PATHS_PLACEHOLDER,)
    assert box.calls["setToolTip"] == edit.calls["setToolTip"] == (EXCLUDED_INPUT_PATHS_HELP,)
    assert box.calls["setSizePolicy"] == ("Preferred", "Maximum")
    assert edit.calls["setFixedHeight"] == (4 * 16 + 12,)
    assert timer.calls["setSingleShot"] == (True,) and timer.calls["setInterval"] == (700,)
    started = timer.started
    edit.setPlainText("$HIP/cache\n\n$HIP/render/**")
    assert timer.started == started + 1
    assert node.parm("excluded_input_paths").values() == ["$HIP/cache", "$HIP/render/**"]
    dialog.job_attachments.refresh_ui.assert_not_called()

    (refresh_before_submit,) = dialog.submit_button.pressed.connect.call_args.args
    (refresh_before_save,) = dialog.export_bundle_button.pressed.connect.call_args.args
    assert refresh_before_save is refresh_before_submit
    timer.isActive = lambda: False
    refresh_before_submit()
    dialog.job_attachments.refresh_ui.assert_not_called()
    timer.isActive = lambda: True
    refresh_before_submit()

    assert timer.calls["stop"] == ()
    (refs, attachments), _ = dialog.job_attachments.refresh_ui.call_args
    assert refs.input_directories == set() and attachments is None
    timer.timeout.emit()
    assert dialog.job_attachments.refresh_ui.call_count == 2


@pytest.mark.parametrize("skip_outputs_keep_caches", [0, 1])
def test_a_click_on_the_checkbox_saves_the_toggle_and_refreshes(fake_qt, skip_outputs_keep_caches):
    node = make_rop(skip_outputs_keep_caches)
    dialog = _dialog()
    scan = AssetReferences(input_directories={"/p/geo"})

    checkbox, _ = add_auto_input_controls(dialog, node, mock.Mock())
    assert checkbox.args == (SKIP_OUTPUTS_KEEP_CACHES_TITLE,)
    assert checkbox.calls["setToolTip"] == (SKIP_OUTPUTS_KEEP_CACHES_HELP,)
    assert checkbox.checked is bool(skip_outputs_keep_caches)
    with mock.patch.object(
        excluded_input_paths_box, "_get_scene_asset_references", return_value=scan
    ):
        checkbox.click()

    assert node.parm("skip_outputs_keep_caches").eval() == 1 - skip_outputs_keep_caches
    dialog.job_attachments.refresh_ui.assert_called_once_with(scan, None)


def test_a_path_added_by_hand_is_put_back_when_a_refresh_no_longer_lists_it() -> None:
    """deadline-cloud's refresh drops an added path that the scan also lists. The path comes back
    as added once a later scan doesn't list it, and other added paths are untouched."""
    added = AssetReferences(input_filenames={"/p/cache/sim.bgeo", "/p/notes.txt"})
    ja = mock.Mock(attachments=added)
    ja.refresh_ui.side_effect = lambda auto, _: added.input_filenames.difference_update(
        auto.input_filenames
    )
    absorbed: dict = {"input_filenames": set(), "input_directories": set()}
    scans = [
        AssetReferences(input_filenames={"/p/cache/sim.bgeo"}),
        AssetReferences(input_filenames={"/p/cache/sim.bgeo", "/p/geo.bgeo"}),
        AssetReferences(input_filenames={"/p/geo.bgeo"}),
        AssetReferences(input_filenames={"/p/geo.bgeo"}),
    ]
    seen = []
    with mock.patch.object(
        excluded_input_paths_box, "_get_scene_asset_references", side_effect=scans
    ):
        for _ in scans:
            _save_and_refresh(make_rop(0), lambda: None, ja, absorbed)
            seen.append(set(added.input_filenames))
            if len(seen) == 3:
                # The user removes the restored path by hand, so it must not come back.
                added.input_filenames.discard("/p/cache/sim.bgeo")

    assert seen == [
        {"/p/notes.txt"},
        {"/p/notes.txt"},
        {"/p/notes.txt", "/p/cache/sim.bgeo"},
        {"/p/notes.txt"},
    ]


@pytest.mark.parametrize("job_attachments", [None, "a failing tab"])
def test_a_change_is_saved_and_a_failed_refresh_logs_one_warning(caplog, job_attachments):
    save = mock.Mock()
    ja = mock.Mock(attachments=AssetReferences(), **{"refresh_ui.side_effect": RuntimeError("x")})
    with (
        mock.patch.object(
            excluded_input_paths_box, "_get_scene_asset_references", return_value=AssetReferences()
        ),
        caplog.at_level(logging.WARNING),
    ):
        _save_and_refresh(make_rop(0), save, ja if job_attachments else None, {})

    save.assert_called_once_with()
    expected = ["Couldn't update the job attachments after the change: x"]
    assert [r.getMessage() for r in caplog.records] == (expected if job_attachments else [])
    assert all(r.exc_info is None for r in caplog.records)


def test_the_dialog_fallback_puts_widgets_at_the_top_in_the_order_given():
    dialog, first, second = mock.Mock(), 1, 2

    to_top = add_to_dialog_top(dialog)
    to_top(first)
    to_top(second)

    assert dialog.layout().insertWidget.call_args_list == [
        mock.call(0, first),
        mock.call(1, second),
    ]


@pytest.mark.parametrize(
    ("parm_name", "help_text", "parm_type"),
    [
        ("excluded_input_path#", EXCLUDED_INPUT_PATHS_HELP, "string"),
        ("skip_outputs_keep_caches", SKIP_OUTPUTS_KEEP_CACHES_HELP, "toggle"),
    ],
)
def test_the_hda_parms_have_the_tooltip_text_as_help_and_the_toggle_is_off(
    parm_name, help_text, parm_type
):
    """String rows aren't file parms, so hou.fileReferences() doesn't report them as inputs."""
    dialog_script = (
        pathlib.Path(__file__).parents[3]
        / "src/deadline/houdini_submitter/otls/deadline_cloud.hda/Driver_1deadline__cloud/DialogScript"
    ).read_text(encoding="utf-8")
    block = dialog_script[dialog_script.index(f'name    "{parm_name}"') :]
    block = block[: block.index("\n        }")]
    found = re.search(r'help    "((?:[^"\\]|\\.)*)"', block)
    assert found is not None
    decoded = re.sub(r"\\(.)", lambda m: "\n" if m.group(1) == "n" else m.group(1), found.group(1))
    assert decoded == help_text
    assert f"type    {parm_type}" in block
    assert 'default { "0" }' in block or parm_type == "string"


def test_the_submit_dialog_gets_both_controls_with_the_dialog_top_fallback(monkeypatch):
    from deadline.houdini_submitter.python.deadline_cloud_for_houdini import submitter

    dialog = mock.Mock()
    dialogs = types.SimpleNamespace(SubmitJobToDeadlineDialog=mock.Mock(return_value=dialog))
    monkeypatch.setitem(
        sys.modules, "deadline.client.ui.dialogs.submit_job_to_deadline_dialog", dialogs
    )
    monkeypatch.setattr(submitter, "get_setting", lambda name: "id")
    monkeypatch.setattr(submitter, "read_ui_settings_from_node", mock.Mock())
    monkeypatch.setattr(submitter, "_get_scene_asset_references", mock.Mock())
    monkeypatch.setattr(submitter, "_install_pre_submission_gate", mock.Mock())
    for name in ("get_default_conda_packages", "get_default_rez_packages"):
        monkeypatch.setattr(submitter, name, mock.Mock())
    add = mock.Mock()
    monkeypatch.setattr(excluded_input_paths_box, "add_auto_input_controls", add)
    node = mock.Mock(**{"inputAncestors.return_value": [mock.Mock()]})

    submitter.submit_callback({"node": node})

    (added_to, added_for, fallback), _ = add.call_args
    assert (added_to, added_for) == (dialog, node)
    fallback("widget")
    dialog.layout().insertWidget.assert_called_once_with(0, "widget")
    dialog.exec_.assert_called_once_with()


def test_the_rop_panel_gets_both_controls_with_the_grid_fallback(monkeypatch):
    """The panel's module is loaded from its file, as Houdini's Python panel loads it."""
    from deadline.houdini_submitter.python import deadline_cloud_for_houdini

    panel_file = (
        pathlib.Path(__file__).parents[3]
        / "src/deadline/houdini_submitter/panel/submitter_panel.py"
    )
    monkeypatch.setitem(sys.modules, "deadline_cloud_for_houdini", deadline_cloud_for_houdini)
    for name in ("_version", "_assets", "constants", "excluded_input_paths_box"):
        module = importlib.import_module(f"{deadline_cloud_for_houdini.__name__}.{name}")
        monkeypatch.setitem(sys.modules, f"deadline_cloud_for_houdini.{name}", module)
    for name in ("houdini_submitter_widget", "queue_parameters", "submitter"):
        monkeypatch.setitem(sys.modules, f"deadline_cloud_for_houdini.{name}", mock.MagicMock())
    monkeypatch.setitem(
        sys.modules, "deadline.client.ui.dialogs.submit_job_to_deadline_dialog", mock.MagicMock()
    )
    monkeypatch.setitem(sys.modules, "deadline.client.ui.pre_gui_hooks", mock.MagicMock())
    widgets: dict[str, Any] = {
        n: mock.MagicMock() for n in ("QComboBox", "QLabel", "QLineEdit", "QVBoxLayout")
    }
    widgets["QGridLayout"] = mock.MagicMock()
    widgets["QWidget"] = type("QWidget", (), {"__init__": lambda self, parent=None: None})
    monkeypatch.setitem(sys.modules, "qtpy.QtWidgets", types.SimpleNamespace(**widgets))
    spec = importlib.util.spec_from_file_location("submitter_panel_under_test", panel_file)
    assert spec is not None and spec.loader is not None
    panel_module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(panel_module)
    add = mock.Mock()
    monkeypatch.setattr(panel_module, "add_auto_input_controls", add)
    panel = mock.Mock()

    panel_module.SubmitterPanel._build_ui(panel)

    (added_to, added_for, fallback), _ = add.call_args
    assert (added_to, added_for) == (panel.deadline_cloud_widget, panel.node)
    grid = panel_module.QGridLayout.return_value
    grid.rowCount.return_value = 3
    fallback("widget")
    grid.addWidget.assert_called_with("widget", 3, 0, 1, -1)
