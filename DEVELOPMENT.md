# Amazon Deadline Cloud for Houdini Development

## Submitter Development Workflow

WARNING: This workflow installs additional Python packages into your Houdini's python distribution.

1. Create a development location within which to do your git checkouts. For example `~/deadline-clients`. Clone packages from this directory with commands like `git clone git@github.com:casillas2/deadline-cloud-for-houdini.git`. You'll also want the `deadline-cloud` and `openjd-adaptor-runtime` repos.
2. Switch to your Houdini directory, like `cd "C:\Program Files\hfs19.5"`.
3. Run `.\bin\hython -m pip install -e C:\Users\<username>\deadline-clients\deadline-cloud` to install the Amazon Deadline Cloud Client Library in edit mode.
4. Run `.\bin\hython -m pip install -e C:\Users\<username>\deadline-clients\openjd-adaptor-runtime-for-python` to install the Open Job Description Adaptor Runtime Library in edit mode.
5. Run `.\bin\hython -m pip install -e C:\Users\<username>\deadline-clients\deadline-cloud-for-houdini` to install the Houdini submitter in edit mode.
6. Run Houdini. Go to File > Import > Houdini Digital Asset Select the `src\deadline\houdini_submitter\otls\deadline_cloud.hda` directory. Press "Install and Create", which should create a "deadline_cloud1" node in the output context.
7. To edit the deadline_cloud hda, go to Assets > Asset Manager. Under Operator Type Libraries > Current HIP File, you will find "Driver/deadline_cloud". Right click, select Type Properties. From the Parameter tab you can modify the parameter interface, as you hit Apply you will see that the "DialogScript" file in the hda source files has been updated.

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
