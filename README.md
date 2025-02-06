# AWS Deadline Cloud for Houdini

[![pypi](https://img.shields.io/pypi/v/deadline-cloud-for-houdini.svg?style=flat)](https://pypi.python.org/pypi/deadline-cloud-for-houdini)
[![python](https://img.shields.io/pypi/pyversions/deadline-cloud-for-houdini.svg?style=flat)](https://pypi.python.org/pypi/deadline-cloud-for-houdini)
[![license](https://img.shields.io/pypi/l/deadline-cloud-for-houdini.svg?style=flat)](https://github.com/aws-deadline/deadline-cloud-for-houdini/blob/mainline/LICENSE)

AWS Deadline Cloud for Houdini is a Python package that supports creating and running SideFX Houdini jobs within [AWS Deadline Cloud][deadline-cloud].
It provides both the implementation of a Houdini plug-in for your workstation that helps you offload the computation for your rendering workloads
to [AWS Deadline Cloud][deadline-cloud] to free up your workstation's compute for other tasks, and the implementation of a command-line
adaptor application based on the [Open Job Description (OpenJD) Adaptor Runtime][openjd-adaptor-runtime] that improves AWS Deadline Cloud's
ability to run Houdini efficiently on your render farm.

[deadline-cloud]: https://docs.aws.amazon.com/deadline-cloud/latest/userguide/what-is-deadline-cloud.html
[deadline-cloud-client]: https://github.com/aws-deadline/deadline-cloud
[openjd]: https://github.com/OpenJobDescription/openjd-specifications/wiki
[openjd-adaptor-runtime]: https://github.com/OpenJobDescription/openjd-adaptor-runtime-for-python
[openjd-adaptor-runtime-lifecycle]: https://github.com/OpenJobDescription/openjd-adaptor-runtime-for-python/blob/release/README.md#adaptor-lifecycle
[service-managed-fleets]: https://docs.aws.amazon.com/deadline-cloud/latest/userguide/smf-manage.html
[default-queue-environment]: https://docs.aws.amazon.com/deadline-cloud/latest/userguide/create-queue-environment.html#conda-queue-environment
[deadline-cloud-submitter]: https://docs.aws.amazon.com/deadline-cloud/latest/userguide/submitter.html
[deadline-cloud-monitor]: https://docs.aws.amazon.com/deadline-cloud/latest/userguide/monitor-onboarding.html

## Compatibility

This library requires:

1. Houdini 19.5, 20.0, or 20.5
1. Python 3.9 or higher; and
1. Linux, Windows, or a macOS operating system.

## Getting Started

This Houdini integration for AWS Deadline Cloud has two components that you will need to install:

1. The Houdini submitter plug-in must be installed on the workstation that you will use to submit jobs; and
2. The Houdini adaptor must be installed on all of your AWS Deadline Cloud worker hosts that will be running the Houdini jobs that you submit.

Before submitting any large, complex, or otherwise compute-heavy Houdini render jobs to your farm using the submitter and adaptor that you
set up, we strongly recommend that you construct a simple test scene that can be rendered quickly and submit renders of that
scene to your farm to ensure that your setup is correctly functioning.

## Submitter

This package provides a Houdini render output node (ROP) that creates jobs for AWS Deadline Cloud using the [AWS Deadline Cloud client library][deadline-cloud-client]. Based on the loaded scene it determines the files required, allows the user to specify render options, and builds an [OpenJD template][openjd] that defines the workflow.

There are two installation options:
1. Windows or Linux: the [official Deadline Cloud submitter installer][deadline-cloud-submitter]
2. Windows, Mac, or Linux: manual installation following the `Submitter Development Workflow` in [DEVELOPMENT.md](DEVELOPMENT.md).

For most setups, you will also want to install the [Deadline Cloud monitor][deadline-cloud-monitor].

### Using the Submitter

To use the Houdini submitter:
1. Open Houdini.
2. In the Network Editor, usually in the lower right side of Houdini, select the `/out` network.
3. Press tab, and enter `deadline`.
4. Select the Deadline Cloud option and place it within the `/out` network to create the node.
5. Connect the output of the last render output node (ROP) (for example, Karma, Mantra, or compositing) in your existing `/out` network to the input of the Deadline Cloud node.
6. Choose the Deadline Cloud node.
7. Enter any job settings in the node editor, usually in the upper right side of Houdini.
8. In the bottom right of the node editor, choose "Submit".

The Deadline Cloud submission will automatically parse the connected `/out` network tree and submit each node as a step in the job maintaining the dependency tree. Using non-default render networks other than `/out` is also supported.

### Adaptor

The Houdini Adaptor implements the [OpenJD][openjd-adaptor-runtime] interface that allows render workloads to launch Houdini and feed it commands. This gives the following benefits:
* a standardized render application interface,
* sticky rendering, where the application stays open between tasks,
* path mapping, that enables cross-platform rendering

To install, test and use a custom Houdini Adaptor, refer to [DEVELOPMENT.md](DEVELOPMENT.md) section `Application Interface Adaptor Development Workflow`

Jobs on Deadline Cloud created by the submitter use a released version of this adaptor by default, and require that both the installed adaptor and the Hython executable be available on the PATH of the user that will be running your jobs.

Or you can set the `HYTHON_EXECUTABLE` to point to the Hython executable.

If you are using the [default Queue Environment][default-queue-environment], or an equivalent, to run your jobs, then the released build of this adaptor will be
automatically made available to your job. Otherwise, you will need to install the adaptor.

The release build of the Houdini adaptor can be installed by the standard python packaging mechanisms:
```sh
$ pip install deadline-cloud-for-houdini
```

After installation, test that it has been installed properly by running the following as the same user that runs your jobs and
that `houdini` can be run as the same user:
```sh
$ houdini-openjd --help
```

For more information on the commands the OpenJD adaptor runtime provides, see [here][openjd-adaptor-runtime-lifecycle].

### Houdini Software Availability in AWS Deadline Cloud Service Managed Fleets

You will need to ensure that the version of Houdini that you want to run is available on the worker host when you are using
AWS Deadline Cloud's [Service Managed Fleets][service-managed-fleets] to run jobs;
hosts do not have any rendering applications pre-installed. The standard way of accomplishing this is described
[in the service documentation](https://docs.aws.amazon.com/deadline-cloud/latest/developerguide/provide-applications.html).
You can find a list of the versions of Houdini that are available by default
[in the user guide](https://docs.aws.amazon.com/deadline-cloud/latest/userguide/create-queue-environment.html#conda-queue-environment)
if you are using the default Conda queue enivonment in your setup.

## Viewing the Job Bundle that will be submitted

To submit a job, the submitter first generates a [Job Bundle][job-bundle], and then uses functionality from the
[Deadline][deadline-cloud-client] package to submit the Job Bundle to your render farm to run. If you would like to see
the job that will be submitted to your farm, then you can use the "Export Bundle" button in the submitter to export the
Job Bundle to a location of your choice. If you want to submit the job from the export, rather than through the
submitter plug-in then you can use the [Deadline Cloud application][deadline-cloud-client] to submit that bundle to your farm.

[job-bundle]: https://docs.aws.amazon.com/deadline-cloud/latest/developerguide/build-job-bundle.html

## Versioning

This package's version follows [Semantic Versioning 2.0](https://semver.org/), but is still considered to be in its
initial development, thus backwards incompatible versions are denoted by minor version bumps. To help illustrate how
versions will increment during this initial development stage, they are described below:

1. The MAJOR version is currently 0, indicating initial development.
2. The MINOR version is currently incremented when backwards incompatible changes are introduced to the public API.
3. The PATCH version is currently incremented when bug fixes or backwards compatible changes are introduced to the public API.

## Security

We take all security reports seriously. When we receive such reports, we will
investigate and subsequently address any potential vulnerabilities as quickly
as possible. If you discover a potential security issue in this project, please
notify AWS/Amazon Security via our [vulnerability reporting page](http://aws.amazon.com/security/vulnerability-reporting/)
or directly via email to [AWS Security](mailto:aws-security@amazon.com). Please do not
create a public GitHub issue in this project.

## Telemetry

See [telemetry](https://github.com/aws-deadline/deadline-cloud-for-houdini/blob/release/docs/telemetry.md) for more information.

## License

This project is licensed under the Apache-2.0 License.
