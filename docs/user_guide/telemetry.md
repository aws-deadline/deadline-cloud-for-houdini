# Telemetry

This library collects telemetry data by default. Telemetry events contain non-personally identifiable information that helps us understand how users interact with our software. We use this to know which features our customers use and/or what existing pain points are.

There are three ways to opt out of telemetry:

- Use the submitter dialog (recommended)
- Use the command-line tool
- Set a terminal environment variable

**To opt out of telemetry (submitter)**

1. Create a submitter node by opening the `out` network, opening its context menu (right-click or press **Tab**), and searching for `deadline`.
1. Choose **Settings...** at the bottom of the submitter panel.
1. Under **General settings**, check **Telemetry opt out**.
1. Choose **Apply** to save the setting, or **OK** to save and close the settings dialog.

**To opt out of telemetry (command line)**

1. Run the `deadline config` command:
```sh
deadline config set telemetry.opt_out true
```

This is equivalent to checking **Telemetry opt out** in the submitter GUI as described above.

**To opt out of telemetry (terminal)**

1. Set the environment variable from a terminal:
```sh
DEADLINE_CLOUD_TELEMETRY_OPT_OUT=true
```
1. Launch Houdini from the same terminal.


An environment variable value will always be used instead of the configuration setting, if it exists.