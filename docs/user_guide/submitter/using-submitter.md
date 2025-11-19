# Submitting a job to Deadline Cloud from Houdini

To use the Deadline Cloud for Houdini submitter, you will need:

- A profile to submit to Deadline Cloud with
- A Deadline Cloud farm and queue to submit to

## Submit a job

**To submit a job from Houdini to Deadline Cloud**

1. In the Network Editor, select the **/out** network.
1. Open the context menu (right-click or press **Tab**) and search for `deadline` to create a Deadline Cloud node.
1. Connect the output of a ROP to the input of the Deadline Cloud node.
    - When you connect a node to the Deadline Cloud node, the submitted job will render the input ROP and all ROPs in its graph.
1. Select the Deadline Cloud node.
1. Use the options in the node editor to configure your job. See [Houdini-specific Settings](#houdini-specific-settings) for information about what each option does.
1. (Optional) To export a job's associated files to your job history directory without submitting it, choose **Export Bundle**.
    - A _job bundle_ is a group of files that defines a job. For more information, see [Open Job Description templates for Deadline Cloud](https://docs.aws.amazon.com/deadline-cloud/latest/developerguide/build-job-bundle.html).
1. Choose **Submit** to send your job to Deadline Cloud.

## Houdini-specific Settings

The **Job-specific settings** tab of the Deadline Cloud node provides options specific to Houdini jobs.

* _Submit Dependencies as Separate Steps_ - Split the ROP graph into separate rendering steps for easier monitoring and debugging. When enabled, each connected render node becomes its own step in the job.
* _Include Adaptor Wheels_ - Enable custom builds of the adaptor (called _wheels_) that change rendering behavior. When enabled, you can specify a directory containing adaptor wheels. You can build adaptor wheels by running the [`build_wheel.sh`](https://github.com/aws-deadline/deadline-cloud-for-houdini/blob/mainline/scripts/build_wheels.sh) script.
* _Adaptor Wheels_ - The directory path containing custom adaptor wheels (only available when **Include Adaptor Wheels** is enabled).
* _Automatically unlock ROPs_ - Automatically unlock dependency ROPs during submission. Locked ROPs use existing outputs and won't re-render, which can block dependencies from re-rendering.
* _Automatically parse scene (.hip) references_ - Automatically discover and attach the job's input and output file names and directories based on the ROP graph during job submission.
* _Automatically save scene (.hip) file_ - Automatically save the scene (`.hip`) file to `$HIP` when submitting a job.

For information about the other submitter options, see the [AWS Deadline Cloud guide for using a submitter](https://docs.aws.amazon.com/deadline-cloud/latest/userguide/jobs-using-submitter.html).

## Monitoring Your Jobs

You can monitor job progress using the Deadline Cloud monitor. For more information, see the [AWS Deadline Cloud guide for using the monitor](https://docs.aws.amazon.com/deadline-cloud/latest/userguide/working-with-deadline-monitor.html).

## Getting Help

- Contact AWS Support
- (Requires a GitHub account) [Open an issue in `deadline-cloud-for-houdini` on GitHub](https://github.com/aws-deadline/deadline-cloud-for-houdini/issues)
