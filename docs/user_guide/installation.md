# Installation

To install the AWS Deadline Cloud for Houdini submitter, you will need:

* A Windows, macOS, or Linux workstation
* Houdini 19.5 or later

**To install the submitter**

1. Download the [Deadline Cloud submitter installer](https://docs.aws.amazon.com/deadline-cloud/latest/userguide/submitter.html).
1. Run the installer.
    - When prompted, select each version of Houini you want to use the submitter with.
1. Launch Houdini.

The Deadline Cloud submitter should be automatically available as a render operator (ROP) node.

> The submitter installer is available for Windows, macOS, and Linux. See the [developer README](https://github.com/aws-deadline/deadline-cloud-for-houdini/blob/mainline/README.md) for manual installation instructions.

**To verify the submitter is installed correctly**

1. Open Houdini.
1. In the **Network Editor**, select the `/out` network.
1. Open the context menu (right-click or press **Tab**) and search for `deadline`.
1. Choose **Deadline Cloud** to create a new node.

![Adding a Deadline Cloud node in the `/out` network](images/add-submitter-node.png)