# Development documentation

This package has two active branches:

- `mainline` -- For active development. This branch is not intended to be consumed by other packages. Any commit to this branch may break APIs, dependencies, and so on, and thus break any consumer without notice.
- `release` -- The official release of the package intended for consumers. Any breaking releases will be accompanied with an increase to this package's interface version.

## Build / Test / Release

### Build the package

```bash
hatch build
```

### Run tests

```bash
hatch run test
```

### Run linting

```bash
hatch run lint
```

### Run formatting

```bash
hatch run fmt
```

## Run tests for all supported Python versions

```bash
hatch run all:test
```

## Use development Submitter in Houdini

```bash
hatch run install
```
A development version of the Deadline Cloud node is then available in `/out` by pressing TAB, typing `deadline`, and adding it to the network.

## Submitter Development Workflow

This workflow creates a "houdini package", a JSON file which tells Houdini where to find the plugin files. This workflow is preferred because it does not install any files directly into your Houdini installation, and it uses the same functionality to load the plugin as is used by the submitter installer. Because we use the paths of our clone of the repository, we only need to run this script once after creating a new development environment, or if the dependencies change, and then changes to the code will be present the next time you launch Houdini.

1. Clone this repository somewhere on the machine you have Houdini installed on:

   ```sh
   git clone git@github.com:aws-deadline/deadline-cloud-for-houdini.git
   cd deadline-cloud-for-houdini
   ```

2. Create a Houdini package using the provided script, specifying the full houdini version:

   ```sh
   hatch run install --houdini-version X.Y
   ```

4. (Optional) If you need to make changes to the Houdini submitter and deadline-cloud at the same time, you can do the following to do an in-place install of deadline-cloud from a clone of the deadline-cloud repository. Note that this will print an error message if the current version of deadline-cloud is greater than specified in deadline-cloud-for-houdini's dependencies, but in most cases this can be ignored:

   ```sh
   cd ..
   git clone git@github.com:aws-deadline/deadline-cloud.git
   cd deadline-cloud-for-houdini
   hatch run install --houdini-version X.Y --local-dep ../deadline-cloud
   ```

5. (Optional) To edit the deadline_cloud hda, go to Assets > Asset Manager. Under Operator Type Libraries > Current HIP File, you will find "Driver/deadline_cloud". Right click, select Type Properties. From the Parameter tab you can modify the parameter interface, as you hit Apply you will see that the "DialogScript" file in the hda source files has been updated.

## Application Interface Adaptor Development Workflow

You can work on the adaptor alongside your submitter development workflow using a Deadline Cloud farm that uses a service-managed fleet. You'll need to perform the following steps to substitute your build of the adaptor for the one in the service.

1. Use the development location from the Submitter Development Workflow.
2. Build wheels for `openjd_adaptor_runtime`, `deadline` and `deadline_cloud_for_houdini`, place them in a "wheels" folder in `deadline-cloud-for-houdini`. A script is provided to do this, just execute from `deadline-cloud-for-houdini`:

   ```bash
   # If you don't have the build package installed already
   $ pip install build
   ...
   $ ./scripts/build_wheels.sh
   ```

   Wheels should have been generated in the "wheels" folder:

   ```bash
   $ ls ./wheels
   deadline_cloud_for_houdini-<version>-py3-none-any.whl
   deadline-<version>-py3-none-any.whl
   openjd_adaptor_runtime-<version>-py3-none-any.whl
   ```

3. Open the Houdini integrated submitter, and in the Job-Specific Settings tab, enable the option 'Include Adaptor Wheels'. Add the "wheels" folder. Then submit your test job.
